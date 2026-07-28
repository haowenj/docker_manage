from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import AnswerRequired, PackageError
from docker_package_app.models import FileAction, FileCandidate


@dataclass(frozen=True)
class FileMaterialization:
    rewrites: Mapping[str, str]
    server_paths: tuple[str, ...]
    copied_bytes: int


def discover_file_dependencies(
    compose: ComposeDocument,
    project_root: Path,
) -> tuple[FileCandidate, ...]:
    project = project_root.resolve()
    found: list[FileCandidate] = []

    for service in compose.services():
        config = compose.service(service)
        volumes = config.get("volumes", ())
        if isinstance(volumes, list):
            for volume in volumes:
                source = _bind_source(volume)
                if source is not None:
                    found.append(_candidate(project, service, source, "bind"))

        for kind in ("config", "secret"):
            references = config.get(f"{kind}s", ())
            if not isinstance(references, list):
                continue
            top_level = compose.data.get(f"{kind}s", {})
            if not isinstance(top_level, dict):
                continue
            for reference in references:
                name = reference.get("source") if isinstance(reference, dict) else reference
                definition = top_level.get(name) if isinstance(name, str) else None
                source = definition.get("file") if isinstance(definition, dict) else None
                if isinstance(source, str):
                    found.append(_candidate(project, service, source, kind))

    unique: dict[tuple[str, str, str, str], FileCandidate] = {}
    for item in found:
        unique[(item.service, item.kind, item.compose_value, item.resolved_path)] = item
    return tuple(
        unique[key]
        for key in sorted(unique)
    )


def materialize_files(
    candidates: Sequence[FileCandidate],
    decisions: Mapping[str, FileAction],
    payload_root: Path,
) -> FileMaterialization:
    payload = payload_root.resolve()
    files_root = payload / "files"
    files_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rewrites: dict[str, str] = {}
    server_paths: set[str] = set()
    copied_sources: set[str] = set()
    copied_bytes = 0

    for candidate in sorted(candidates, key=lambda item: item.resolved_path):
        action = decisions.get(candidate.resolved_path)
        if action is None:
            if candidate.inside_project:
                action = FileAction.COPY
            else:
                raise AnswerRequired(
                    f"decision required for external path {candidate.resolved_path}"
                )
        if action is FileAction.KEEP_SERVER_PATH:
            server_paths.add(candidate.resolved_path)
            continue
        if not candidate.inside_project or not candidate.project_path:
            raise PackageError(
                f"cannot copy path outside project: {candidate.resolved_path}"
            )

        source = Path(candidate.resolved_path)
        if not source.exists():
            raise PackageError(f"local Compose dependency does not exist: {source}")
        destination = files_root / candidate.project_path
        if not destination.resolve(strict=False).is_relative_to(files_root):
            raise PackageError(f"unsafe payload path: {candidate.project_path}")
        if candidate.resolved_path not in copied_sources:
            _copy_dependency(source, destination)
            copied_sources.add(candidate.resolved_path)
            copied_bytes += candidate.estimated_size
        rewrites[candidate.compose_value] = f"./files/{Path(candidate.project_path).as_posix()}"

    return FileMaterialization(
        rewrites=rewrites,
        server_paths=tuple(sorted(server_paths)),
        copied_bytes=copied_bytes,
    )


def _candidate(
    project_root: Path,
    service: str,
    raw_path: str,
    kind: str,
) -> FileCandidate:
    source = Path(raw_path).expanduser()
    if not source.is_absolute():
        source = project_root / source
    resolved = source.resolve()
    inside = resolved.is_relative_to(project_root)
    project_path = None
    if inside:
        relative = resolved.relative_to(project_root)
        project_path = relative.as_posix() if relative.parts else "project"
    return FileCandidate(
        service=service,
        compose_value=raw_path,
        resolved_path=str(resolved),
        kind=kind,
        inside_project=inside,
        project_path=project_path,
        estimated_size=_path_size(resolved),
    )


def _bind_source(value: object) -> str | None:
    if isinstance(value, dict):
        if value.get("type") != "bind":
            return None
        source = value.get("source")
        return source if isinstance(source, str) else None
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) < 2 or not _looks_like_path(parts[0]):
        return None
    return parts[0]


def _looks_like_path(value: str) -> bool:
    return value.startswith((".", "/", "~")) or "/" in value or "\\" in value


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories if name != ".docker-manage"]
        for name in files:
            candidate = Path(root) / name
            if candidate.is_symlink():
                continue
            total += candidate.stat().st_size
    return total


def _copy_dependency(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, destination)
        return
    _validate_directory_symlinks(source)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=lambda _path, names: [name for name in names if name == ".docker-manage"],
    )


def _validate_directory_symlinks(source: Path) -> None:
    root = source.resolve()
    for path in source.rglob("*"):
        if ".docker-manage" in path.parts or not path.is_symlink():
            continue
        if not path.resolve().is_relative_to(root):
            raise PackageError(f"symlink escapes copied directory: {path}")
