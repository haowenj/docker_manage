from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from docker_package_app.command import CommandRunner
from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import PackageError
from docker_package_app.models import ImageAction, PackagePlan, Stage

EXPORT_RESERVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ImageMetadata:
    reference: str
    image_id: str
    repo_digests: tuple[str, ...]
    os: str
    architecture: str
    variant: str | None
    size: int

    @property
    def platform(self) -> str:
        value = f"{self.os}/{self.architecture}"
        return f"{value}/{self.variant}" if self.variant else value


class DockerEngine:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def build(
        self,
        compose_path: Path,
        services: Sequence[str],
        platform: str,
    ) -> tuple[str, ...]:
        if not services:
            return ()
        argv = [
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "build",
            *services,
        ]
        self._run_mutation(
            argv,
            operation="build",
            env={"DOCKER_DEFAULT_PLATFORM": platform},
        )
        return tuple(services)

    def pull(self, image: str, platform: str) -> None:
        self._run_mutation(
            ["docker", "pull", "--platform", platform, image],
            operation="pull",
        )

    def inspect(
        self,
        images: Sequence[str],
        expected_platform: str,
    ) -> tuple[ImageMetadata, ...]:
        if not images:
            return ()
        result = self.runner.run(
            ["docker", "image", "inspect", "--format", "{{json .}}", *images]
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != len(images):
            raise PackageError(
                f"Docker returned metadata for {len(lines)} of {len(images)} images"
            )
        metadata: list[ImageMetadata] = []
        for reference, line in zip(images, lines, strict=True):
            try:
                raw = json.loads(line)
                item = ImageMetadata(
                    reference=reference,
                    image_id=str(raw["Id"]),
                    repo_digests=tuple(raw.get("RepoDigests") or ()),
                    os=str(raw["Os"]),
                    architecture=str(raw["Architecture"]),
                    variant=str(raw["Variant"]) if raw.get("Variant") else None,
                    size=int(raw["Size"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PackageError(f"invalid Docker image metadata for {reference}") from exc
            _verify_platform(item, expected_platform)
            metadata.append(item)
        return tuple(metadata)

    def ensure_export_space(
        self,
        images: Sequence[ImageMetadata],
        file_bytes: int,
        output_dir: Path,
    ) -> None:
        required = 2 * sum(item.size for item in images) + file_bytes + EXPORT_RESERVE_BYTES
        free = shutil.disk_usage(output_dir).free
        if free < required:
            raise PackageError(
                f"insufficient disk space for image export: required {required} bytes, "
                f"free {free} bytes",
                hint="Free disk space or move the project to a larger filesystem.",
            )

    def save(self, images: Sequence[str], output: Path) -> None:
        if not images:
            return
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runner.run(
            ["docker", "image", "save", "--output", str(output), *images]
        )
        if output.exists():
            output.chmod(0o600)

    def _run_mutation(
        self,
        argv: Sequence[str],
        *,
        operation: str,
        env: dict[str, str] | None = None,
    ) -> None:
        try:
            self.runner.run(argv, env=env)
        except PackageError as exc:
            raise PackageError(
                exc.message,
                stage=Stage.CONFIRMED,
                hint=f"Review the Docker {operation} error and correct the local Docker environment.",
                details=exc.details,
            ) from exc


def prepare_build_compose(
    compose: ComposeDocument,
    plan: PackagePlan,
    work_dir: Path,
) -> Path:
    work = work_dir.resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = copy.deepcopy(compose.data)
    image_plans = {
        item.service: item
        for item in plan.images
        if item.action is ImageAction.BUILD
    }
    build_args_by_service: dict[str, dict[str, str]] = {}
    for item in plan.build_args:
        build_args_by_service.setdefault(item.service, {})[item.name] = item.value

    for service, image_plan in image_plans.items():
        config = data["services"].get(service)
        if not isinstance(config, dict) or "build" not in config:
            raise PackageError(f"planned build service is missing from Compose: {service}")
        raw_build = config["build"]
        build = {"context": raw_build} if isinstance(raw_build, str) else copy.deepcopy(raw_build)
        if not isinstance(build, dict):
            raise PackageError(f"invalid build configuration for service {service}")
        context_value = build.get("context", ".")
        context = Path(str(context_value))
        if not context.is_absolute():
            context = compose.project_root / context
        context = context.resolve()

        destination_dir = work / "dockerfiles" / service
        destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = destination_dir / "Dockerfile"
        inline = build.pop("dockerfile_inline", None)
        if isinstance(inline, str):
            destination.write_text(inline, encoding="utf-8")
            source = None
        else:
            dockerfile_value = build.get("dockerfile", "Dockerfile")
            source = Path(str(dockerfile_value))
            if not source.is_absolute():
                source = context / source
            source = source.resolve()
            if not source.is_file():
                raise PackageError(f"Dockerfile does not exist for {service}: {source}")
            shutil.copy2(source, destination)

        _write_build_ignore(context, source, destination)
        build["context"] = str(context)
        build["dockerfile"] = str(destination)
        existing_args = build.get("args", {})
        if isinstance(existing_args, list):
            existing_args = {
                entry.split("=", 1)[0]: entry.split("=", 1)[1] if "=" in entry else None
                for entry in existing_args
                if isinstance(entry, str)
            }
        if not isinstance(existing_args, dict):
            existing_args = {}
        build["args"] = {**existing_args, **build_args_by_service.get(service, {})}
        config["build"] = build
        config["image"] = image_plan.final_image
        config["platform"] = plan.platform

    output = work / "build.compose.yaml"
    ComposeDocument.from_data(compose.project_root, data).dump(output)
    output.chmod(0o600)
    return output


def _write_build_ignore(
    context: Path,
    source: Path | None,
    destination: Path,
) -> None:
    chunks: list[str] = []
    candidates = [context / ".dockerignore"]
    if source is not None:
        candidates.append(source.with_name(f"{source.name}.dockerignore"))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen or not candidate.is_file():
            continue
        seen.add(resolved)
        chunks.append(candidate.read_text(encoding="utf-8").rstrip("\n"))
    content = "\n".join(chunk for chunk in chunks if chunk)
    if content:
        content += "\n"
    content += "\n.docker-manage/\n"
    target = destination.with_name(f"{destination.name}.dockerignore")
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)


def _verify_platform(metadata: ImageMetadata, expected: str) -> None:
    parts = expected.split("/")
    actual_parts = [metadata.os, metadata.architecture]
    if metadata.variant:
        actual_parts.append(metadata.variant)
    if actual_parts[:2] != parts[:2] or (len(parts) == 3 and actual_parts != parts):
        raise PackageError(
            f"image {metadata.reference} platform {metadata.platform} does not match {expected}"
        )
