from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values, set_key

from docker_package_app.errors import PackageError, UsageError
from docker_package_app.models import (
    DefaultValue,
    EnvAssignment,
    EnvCandidate,
    SourceRef,
)

CURRENT_ENV_RELATIVE = Path(".docker-manage/.env")
CURRENT_ENV_SOURCE = CURRENT_ENV_RELATIVE.as_posix()


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
