from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from docker_package_app.command import CommandRunner
from docker_package_app.errors import UsageError

COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
IGNORED_DIRECTORIES = {
    ".git",
    ".docker-manage",
    ".venv",
    "venv",
    "node_modules",
    "dist",
}


@dataclass(frozen=True)
class PreflightReport:
    docker_version: str
    compose_version: str
    buildx_version: str
    free_disk_bytes: int


@dataclass(frozen=True)
class DockerFileCandidates:
    dockerfiles: tuple[str, ...]
    compose_files: tuple[str, ...]
    profiles: tuple[str, ...]

    @property
    def requires_dockerfile_choice(self) -> bool:
        return len(self.dockerfiles) > 1

    @property
    def requires_compose_choice(self) -> bool:
        base_files = [
            path for path in self.compose_files if ".override." not in path
        ]
        return len(base_files) > 1


def preflight(project_root: Path, runner: CommandRunner) -> PreflightReport:
    project = project_root.resolve()
    if not project.is_dir():
        raise UsageError(f"project directory does not exist: {project}")

    docker = runner.run(
        [
            "docker",
            "version",
            "--format",
            "{{.Client.Version}}|{{.Server.Version}}",
        ]
    )
    compose = runner.run(["docker", "compose", "version"])
    buildx = runner.run(["docker", "buildx", "version"])
    return PreflightReport(
        docker_version=docker.stdout.strip(),
        compose_version=compose.stdout.strip(),
        buildx_version=buildx.stdout.strip(),
        free_disk_bytes=shutil.disk_usage(project).free,
    )


def discover_docker_files(project_root: Path) -> DockerFileCandidates:
    root = project_root.resolve()
    if not root.is_dir():
        raise UsageError(f"project directory does not exist: {root}")

    dockerfiles = tuple(
        sorted(
            path.name
            for path in root.iterdir()
            if path.is_file()
            and (path.name == "Dockerfile" or path.name.startswith("Dockerfile."))
        )
    )
    standard_compose = [root / name for name in COMPOSE_NAMES]
    override_compose = sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and (
            path.name.startswith("compose.override.")
            or path.name.startswith("docker-compose.override.")
        )
        and path.suffix in {".yaml", ".yml"}
    )
    compose_paths = [path for path in standard_compose if path.is_file()]
    compose_paths.extend(path for path in override_compose if path not in compose_paths)

    profiles: set[str] = set()
    for path in compose_paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise UsageError(f"unable to parse Compose candidate {path.name}: {exc}") from exc
        services = document.get("services", {}) if isinstance(document, dict) else {}
        if not isinstance(services, dict):
            continue
        for service in services.values():
            if not isinstance(service, dict):
                continue
            value = service.get("profiles", ())
            if isinstance(value, str):
                profiles.add(value)
            elif isinstance(value, list):
                profiles.update(item for item in value if isinstance(item, str))

    return DockerFileCandidates(
        dockerfiles=dockerfiles,
        compose_files=tuple(path.name for path in compose_paths),
        profiles=tuple(sorted(profiles)),
    )


def default_identity(
    project_root: Path,
    runner: CommandRunner,
    now: datetime | None = None,
) -> tuple[str, str]:
    project = project_root.resolve()
    app_name = _normalize_name(project.name)
    git_result = runner.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=project,
        check=False,
    )
    version = git_result.stdout.strip() if git_result.returncode == 0 else ""
    if not version:
        current = now or datetime.now(timezone.utc)
        version = current.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return app_name, version


def _normalize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "app"
