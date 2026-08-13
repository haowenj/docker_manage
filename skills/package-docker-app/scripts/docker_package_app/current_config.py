from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, set_key
from pydantic import Field, ValidationError

from docker_package_app.errors import PackageError, UsageError
from docker_package_app.models import (
    CurrentPortSelection,
    DefaultValue,
    EnvAssignment,
    EnvCandidate,
    FileAction,
    FileCandidate,
    PortAssignment,
    PortCandidate,
    SourceRef,
    StrictModel,
)

CURRENT_ENV_RELATIVE = Path(".docker-manage/.env")
CURRENT_ENV_SOURCE = CURRENT_ENV_RELATIVE.as_posix()
CURRENT_PORTS_RELATIVE = Path(".docker-manage/ports.json")
CURRENT_MOUNTS_RELATIVE = Path(".docker-manage/mounts.json")
CURRENT_MOUNTS_SOURCE = CURRENT_MOUNTS_RELATIVE.as_posix()


class _PortSnapshotEntry(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    exposed: bool
    host_port: int | None = Field(default=None, ge=1, le=65535)


class _PortSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    ports: tuple[_PortSnapshotEntry, ...] = ()


class _MountSnapshotEntry(StrictModel):
    resolved_path: str
    action: FileAction


class _MountSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    mounts: tuple[_MountSnapshotEntry, ...] = ()


def artifact_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def attach_current_values(
    project_root: Path,
    candidates: Sequence[EnvCandidate],
) -> tuple[EnvCandidate, ...]:
    snapshot = project_root.resolve() / CURRENT_ENV_RELATIVE
    if not snapshot.exists():
        return tuple(candidates)
    if not snapshot.is_file():
        raise UsageError(f"当前环境变量快照不是普通文件：{snapshot}")
    try:
        values = dotenv_values(snapshot)
    except OSError as exc:
        raise UsageError(f"无法读取当前环境变量快照 {snapshot}：{exc}") from exc

    attached: list[EnvCandidate] = []
    for candidate in candidates:
        service_key = f"{artifact_component(candidate.service)}_{candidate.name}"
        selected_key = (
            service_key
            if service_key in values
            else candidate.name
            if candidate.name in values
            else None
        )
        if selected_key is None:
            attached.append(candidate)
            continue
        value = values[selected_key]
        if value is None:
            raise UsageError(
                f"当前环境变量快照中的 {selected_key} 没有具体值；"
                f"空字符串请写成 {selected_key}="
            )
        attached.append(
            candidate.model_copy(
                update={
                    "current": DefaultValue(
                        value=value,
                        source=SourceRef(path=CURRENT_ENV_SOURCE),
                    )
                }
            )
        )
    return tuple(attached)


def attach_current_ports(
    project_root: Path,
    candidates: Sequence[PortCandidate],
) -> tuple[PortCandidate, ...]:
    snapshot = project_root.resolve() / CURRENT_PORTS_RELATIVE
    if not snapshot.exists():
        return tuple(candidates)
    if not snapshot.is_file():
        raise UsageError(f"当前端口快照不是普通文件：{snapshot}")
    try:
        body = _PortSnapshot.model_validate_json(
            snapshot.read_text(encoding="utf-8")
        )
        identities = [
            (item.service, item.container_port, item.protocol)
            for item in body.ports
        ]
        if len(identities) != len(set(identities)):
            raise UsageError(f"当前端口快照包含重复端口：{snapshot}")
        selections = {
            (item.service, item.container_port, item.protocol): (
                CurrentPortSelection(
                    exposed=item.exposed,
                    host_port=item.host_port,
                )
            )
            for item in body.ports
        }
    except (OSError, ValidationError, ValueError) as exc:
        raise UsageError(f"当前端口快照无效 {snapshot}：{exc}") from exc

    return tuple(
        candidate.model_copy(
            update={
                "current": selections.get(
                    (
                        candidate.service,
                        candidate.container_port,
                        candidate.protocol,
                    )
                )
            }
        )
        for candidate in candidates
    )


def attach_current_mounts(
    project_root: Path,
    candidates: Sequence[FileCandidate],
) -> tuple[FileCandidate, ...]:
    snapshot = project_root.resolve() / CURRENT_MOUNTS_RELATIVE
    if not snapshot.exists():
        return tuple(candidates)
    if not snapshot.is_file():
        raise UsageError(f"当前挂载快照不是普通文件：{snapshot}")
    try:
        body = _MountSnapshot.model_validate_json(
            snapshot.read_text(encoding="utf-8")
        )
        selections: dict[str, FileAction] = {}
        for item in body.mounts:
            raw_path = Path(item.resolved_path)
            if not raw_path.is_absolute():
                raise ValueError(f"挂载路径不是绝对路径：{item.resolved_path}")
            resolved_path = str(raw_path.resolve())
            if resolved_path in selections:
                raise ValueError(f"挂载路径重复：{resolved_path}")
            selections[resolved_path] = item.action
    except (OSError, ValidationError, ValueError) as exc:
        raise UsageError(f"当前挂载快照无效 {snapshot}：{exc}") from exc

    attached: list[FileCandidate] = []
    for candidate in candidates:
        if candidate.kind != "bind":
            attached.append(candidate)
            continue
        resolved_path = str(Path(candidate.resolved_path).resolve())
        action = selections.get(resolved_path)
        if action is FileAction.COPY and not candidate.inside_project:
            raise UsageError(
                f"当前挂载快照无效 {snapshot}：项目目录外路径 "
                f"{resolved_path} 不能使用 copy"
            )
        attached.append(candidate.model_copy(update={"current_action": action}))
    return tuple(attached)


def write_current_environment(
    project_root: Path,
    assignments: Sequence[EnvAssignment],
) -> Path:
    target = project_root.resolve() / CURRENT_ENV_RELATIVE
    temporary: Path | None = None
    values = {
        assignment.artifact_name: assignment.value
        for assignment in assignments
    }
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        for name in sorted(values):
            set_key(
                temporary,
                name,
                values[name],
                quote_mode="always",
            )
        temporary.chmod(0o600)
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    except OSError as exc:
        raise PackageError(f"无法更新当前环境变量快照 {target}：{exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_current_ports(
    project_root: Path,
    assignments: Sequence[PortAssignment],
) -> Path:
    target = project_root.resolve() / CURRENT_PORTS_RELATIVE
    entries = tuple(
        _PortSnapshotEntry(
            service=item.service,
            container_port=item.container_port,
            protocol=item.protocol,
            exposed=item.exposed,
            host_port=item.host_port,
        )
        for item in sorted(
            assignments,
            key=lambda value: (
                value.service,
                value.container_port,
                value.protocol,
            ),
        )
    )
    body = _PortSnapshot(ports=entries).model_dump_json(indent=2) + "\n"
    return _atomic_write_snapshot(target, body)


def _atomic_write_snapshot(target: Path, body: str) -> Path:
    temporary: Path | None = None
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
        temporary = None
        return target
    except OSError as exc:
        raise PackageError(f"无法更新当前配置快照 {target}：{exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_current_configuration(
    project_root: Path,
    environment: Sequence[EnvAssignment],
    ports: Sequence[PortAssignment],
) -> tuple[Path, Path]:
    root = project_root.resolve()
    targets = (
        root / CURRENT_ENV_RELATIVE,
        root / CURRENT_PORTS_RELATIVE,
    )
    previous: dict[Path, tuple[bytes | None, int | None]] = {}
    try:
        for target in targets:
            if target.exists():
                previous[target] = (
                    target.read_bytes(),
                    target.stat().st_mode & 0o777,
                )
            else:
                previous[target] = (None, None)
    except OSError as exc:
        raise PackageError(f"无法读取当前配置快照以便回滚：{exc}") from exc

    try:
        env_path = write_current_environment(root, environment)
        ports_path = write_current_ports(root, ports)
        return env_path, ports_path
    except PackageError as original:
        failures: list[str] = []
        for target, (body, mode) in previous.items():
            try:
                if body is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write_snapshot(target, body.decode("utf-8"))
                    if mode is not None:
                        target.chmod(mode)
            except (OSError, PackageError, UnicodeDecodeError) as restore_error:
                failures.append(f"{target}: {restore_error}")
        if failures:
            raise PackageError(
                f"{original}；当前配置恢复失败，请人工检查："
                + "；".join(failures)
            ) from original
        raise
