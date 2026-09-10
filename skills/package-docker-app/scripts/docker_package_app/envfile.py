from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from docker_package_app.errors import PackageError

_COMMENT_PATTERN = re.compile(r"(?:^|[ \t])#")


def serialize_environment(values: Mapping[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(values):
        value = values[name]
        if "\n" in value or "\r" in value:
            raise PackageError(
                f"环境变量 {name} 包含换行符，无法无损写入 Docker env-file"
            )
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n" if lines else ""


def write_environment_file(path: Path, values: Mapping[str, str]) -> None:
    path.write_text(serialize_environment(values), encoding="utf-8")


def compose_environment_value(artifact_name: str, value: str) -> str:
    if _requires_literal_compose_value(value):
        return value.replace("$", "$$")
    return f"${{{artifact_name}}}"


def _requires_literal_compose_value(value: str) -> bool:
    return (
        value != value.strip()
        or any(character in value for character in "\t\\'\"$")
        or _COMMENT_PATTERN.search(value) is not None
    )
