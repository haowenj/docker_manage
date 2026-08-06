from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import AnswerRequired, PackageError
from docker_package_app.models import FileAction, FileAssignment, FileCandidate

FileIdentity = tuple[str, str, str]
FileRewriteKey = tuple[str, str, str]


@dataclass(frozen=True)
class FileMaterialization:
    rewrites: Mapping[FileRewriteKey, str]
    server_paths: tuple[str, ...]
    copied_bytes: int


def discover_file_dependencies(
    compose: ComposeDocument,
    project_root: Path,
    *,
    resolved_compose: ComposeDocument | None = None,
) -> tuple[FileCandidate, ...]:
    project = project_root.resolve()
    final = resolved_compose or compose
    found: list[FileCandidate] = []

    if final.services() != compose.services():
        raise PackageError("Compose 插值前后的服务集合不一致")

    for service in compose.services():
        raw_config = compose.service(service)
        final_config = final.service(service)
        raw_volumes = raw_config.get("volumes", ())
        final_volumes = final_config.get("volumes", ())
        if isinstance(raw_volumes, list):
            if not isinstance(final_volumes, list) or len(final_volumes) != len(raw_volumes):
                raise PackageError(f"服务 {service} 的 Compose volume 无法一一对应")
            for index, raw_volume in enumerate(raw_volumes):
                raw_source = _bind_source(raw_volume)
                if raw_source is None:
                    continue
                final_source = _bind_source(final_volumes[index])
                if final_source is None:
                    raise PackageError(
                        f"服务 {service} 的 bind source 插值结果无效：{raw_source}"
                    )
                found.append(
                    _candidate(
                        project,
                        service,
                        raw_source,
                        "bind",
                        effective_path=final_source,
                    )
                )

        for kind in ("config", "secret"):
            references = raw_config.get(f"{kind}s", ())
            if not isinstance(references, list):
                continue
            raw_definitions = compose.data.get(f"{kind}s", {})
            final_definitions = final.data.get(f"{kind}s", {})
            if not isinstance(raw_definitions, dict) or not isinstance(final_definitions, dict):
                continue
            for reference in references:
                name = reference.get("source") if isinstance(reference, dict) else reference
                raw_definition = raw_definitions.get(name) if isinstance(name, str) else None
                final_definition = final_definitions.get(name) if isinstance(name, str) else None
                raw_source = (
                    raw_definition.get("file")
                    if isinstance(raw_definition, dict)
                    else None
                )
                final_source = (
                    final_definition.get("file")
                    if isinstance(final_definition, dict)
                    else None
                )
                if isinstance(raw_source, str):
                    if not isinstance(final_source, str):
                        raise PackageError(f"{kind} {name} 的文件路径插值结果无效")
                    found.append(
                        _candidate(
                            project,
                            service,
                            raw_source,
                            kind,
                            effective_path=final_source,
                        )
                    )

    unique: dict[tuple[str, str, str, str], FileCandidate] = {}
    for item in found:
        unique[(item.service, item.kind, item.compose_value, item.resolved_path)] = item
    return tuple(
        unique[key]
        for key in sorted(unique)
    )


def materialize_files(
    candidates: Sequence[FileCandidate],
    assignments: Sequence[FileAssignment],
    payload_root: Path,
    project_root: Path,
) -> FileMaterialization:
    assignment_by_identity = {_assignment_identity(item): item for item in assignments}
    payload = payload_root.resolve()
    files_root = payload / "files"
    rewrites: dict[FileRewriteKey, str] = {}
    server_paths: set[str] = set()
    copied_sources: set[str] = set()
    bind_destinations: set[Path] = set()
    copied_bytes = 0
    project = project_root.resolve()

    for candidate in sorted(candidates, key=lambda item: item.resolved_path):
        assignment = assignment_by_identity.get(_candidate_identity(candidate))
        if assignment is None:
            raise AnswerRequired(
                "文件依赖缺少已确认决定："
                f"{candidate.service}/{candidate.kind}/{candidate.compose_value}"
            )
        action = assignment.action
        source = Path(assignment.resolved_path).resolve()
        inside_project = source.is_relative_to(project)

        if action is FileAction.KEEP_SERVER_PATH:
            server_source = required_server_path(
                project,
                assignment.resolved_path,
                assignment.original_value,
            )
            server_paths.add(server_source)
            if candidate.kind == "bind" and inside_project:
                rewrites[_rewrite_key(candidate)] = server_source
            continue

        if not inside_project or not assignment.payload_path:
            raise PackageError(
                f"无法复制项目目录之外的路径：{assignment.resolved_path}"
            )
        if not source.exists():
            raise PackageError(f"本地 Compose 依赖不存在：{source}")
        destination = payload / assignment.payload_path
        if not destination.resolve(strict=False).is_relative_to(files_root):
            raise PackageError(f"制品载荷路径不安全：{assignment.payload_path}")
        files_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if assignment.resolved_path not in copied_sources:
            _copy_dependency(source, destination)
            copied_sources.add(assignment.resolved_path)
            copied_bytes += _path_size(source)
        if candidate.kind == "bind":
            bind_destinations.add(destination)
        rewrites[_rewrite_key(candidate)] = deployment_source(
            project,
            assignment.resolved_path,
            assignment.original_value,
        )

    for destination in sorted(bind_destinations):
        _make_bind_writable(destination)

    return FileMaterialization(
        rewrites=rewrites,
        server_paths=tuple(sorted(server_paths)),
        copied_bytes=copied_bytes,
    )


def deployment_source(
    project_root: Path,
    resolved_path: str,
    original_value: str,
) -> str:
    root = project_root.resolve()
    resolved = Path(resolved_path).resolve()
    if resolved.is_relative_to(root):
        relative = resolved.relative_to(root)
        project_path = relative if relative.parts else Path("project")
        return f"./files/{project_path.as_posix()}"
    return original_value


def required_server_path(
    project_root: Path,
    resolved_path: str,
    original_value: str,
) -> str:
    root = project_root.resolve()
    resolved = Path(resolved_path).resolve()
    if resolved.is_relative_to(root):
        return deployment_source(root, resolved_path, original_value)
    if "${" in original_value:
        return str(resolved)
    return original_value


def _candidate_identity(candidate: FileCandidate) -> FileIdentity:
    return candidate.service, candidate.kind, candidate.compose_value


def _assignment_identity(assignment: FileAssignment) -> FileIdentity:
    return assignment.service, assignment.kind, assignment.original_value


def _rewrite_key(candidate: FileCandidate) -> FileRewriteKey:
    return candidate.service, candidate.kind, candidate.compose_value


def _candidate(
    project_root: Path,
    service: str,
    raw_path: str,
    kind: str,
    *,
    effective_path: str | None = None,
) -> FileCandidate:
    source = Path(effective_path or raw_path).expanduser()
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


def _make_bind_writable(path: Path) -> None:
    try:
        if path.is_symlink():
            return
        if path.is_file():
            path.chmod(0o666)
            return
        if not path.is_dir():
            raise PackageError(f"bind mount 副本不是普通文件或目录：{path}")
        for root, directories, files in os.walk(path, followlinks=False):
            current = Path(root)
            current.chmod(0o777)
            for name in directories:
                child = current / name
                if not child.is_symlink():
                    child.chmod(0o777)
            for name in files:
                child = current / name
                if not child.is_symlink():
                    if not child.is_file():
                        raise PackageError(
                            f"bind mount 副本包含非普通文件：{child}"
                        )
                    child.chmod(0o666)
    except OSError as exc:
        raise PackageError(f"无法设置 bind mount 副本权限 {path}：{exc}") from exc


def _validate_directory_symlinks(source: Path) -> None:
    root = source.resolve()
    for path in source.rglob("*"):
        if ".docker-manage" in path.parts or not path.is_symlink():
            continue
        if not path.resolve().is_relative_to(root):
            raise PackageError(f"符号链接指向复制目录之外：{path}")
