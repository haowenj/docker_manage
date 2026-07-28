from __future__ import annotations

import re
import shlex
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from docker_package_app.compose import ComposeDocument
from docker_package_app.models import (
    BuildArgCandidate,
    DefaultValue,
    EnvCandidate,
    SourceRef,
)

MAX_SOURCE_SIZE = 2 * 1024 * 1024
IGNORED_PARTS = {
    ".git",
    ".docker-manage",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}
ENV_INTERPOLATION = re.compile(
    r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?:(?::-|-)(.*))?\}$"
)


@dataclass(frozen=True)
class SourcePattern:
    regex: re.Pattern[str]
    default_group: int | None = None


SOURCE_PATTERNS: dict[str, tuple[SourcePattern, ...]] = {
    ".py": (
        SourcePattern(
            re.compile(
                r"os\.getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
                r"(?:\s*,\s*['\"]([^'\"]*)['\"])?"
            ),
            2,
        ),
        SourcePattern(
            re.compile(
                r"os\.environ\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
                r"(?:\s*,\s*['\"]([^'\"]*)['\"])?"
            ),
            2,
        ),
        SourcePattern(
            re.compile(r"os\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
        ),
    ),
    ".js": (
        SourcePattern(re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")),
        SourcePattern(
            re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
        ),
    ),
    ".ts": (
        SourcePattern(re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")),
        SourcePattern(
            re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
        ),
    ),
    ".java": (
        SourcePattern(
            re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}"),
            2,
        ),
    ),
    ".properties": (
        SourcePattern(
            re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}"),
            2,
        ),
    ),
    ".go": (
        SourcePattern(
            re.compile(r"os\.(?:Getenv|LookupEnv)\(\s*['\"`]([A-Za-z_][A-Za-z0-9_]*)['\"`]\s*\)")
        ),
    ),
    ".rb": (
        SourcePattern(re.compile(r"ENV\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")),
    ),
    ".php": (
        SourcePattern(re.compile(r"getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\)")),
    ),
}


def scan_environment(
    project_root: Path,
    service_roots: Mapping[str, Path],
    compose: ComposeDocument,
    dockerfiles: Mapping[str, Path],
) -> tuple[EnvCandidate, ...]:
    project = project_root.resolve()
    candidates: list[EnvCandidate] = []

    for service, root in sorted(service_roots.items()):
        candidates.extend(_scan_source_tree(project, service, root.resolve()))

    for service in compose.services():
        candidates.extend(_scan_compose_service(project, service, compose.service(service)))

    for service, dockerfile in sorted(dockerfiles.items()):
        candidates.extend(_scan_dockerfile_env(project, service, dockerfile.resolve()))

    return merge_defaults(candidates)


def scan_build_args(
    dockerfiles: Mapping[str, Path],
) -> tuple[BuildArgCandidate, ...]:
    found: list[BuildArgCandidate] = []
    for service, dockerfile in sorted(dockerfiles.items()):
        for line_number, logical_line in _dockerfile_lines(dockerfile):
            if not logical_line.upper().startswith("ARG "):
                continue
            declaration = logical_line[4:].strip()
            name, separator, default = declaration.partition("=")
            name = name.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            found.append(
                BuildArgCandidate(
                    service=service,
                    name=name,
                    default=default if separator else None,
                    source=SourceRef(path=str(dockerfile), line=line_number),
                )
            )
    return tuple(sorted(found, key=lambda item: (item.service, item.name)))


def merge_defaults(candidates: Iterable[EnvCandidate]) -> tuple[EnvCandidate, ...]:
    grouped: dict[tuple[str, str], list[EnvCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.service, candidate.name)].append(candidate)

    merged: list[EnvCandidate] = []
    for (service, name), items in sorted(grouped.items()):
        sources: dict[tuple[str, int | None], SourceRef] = {}
        defaults: dict[tuple[str, str, int | None], DefaultValue] = {}
        for item in items:
            for source in item.sources:
                sources[(source.path, source.line)] = source
            for default in item.defaults:
                key = (default.value, default.source.path, default.source.line)
                defaults[key] = default
        merged.append(
            EnvCandidate(
                service=service,
                name=name,
                sources=tuple(sources[key] for key in sorted(sources)),
                defaults=tuple(defaults[key] for key in sorted(defaults)),
            )
        )
    return tuple(merged)


def _scan_source_tree(
    project_root: Path,
    service: str,
    source_root: Path,
) -> list[EnvCandidate]:
    if not source_root.is_dir():
        return []
    found: list[EnvCandidate] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        patterns = SOURCE_PATTERNS.get(path.suffix.lower())
        if not patterns or path.stat().st_size > MAX_SOURCE_SIZE:
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        source_path = _display_path(project_root, path)
        for pattern in patterns:
            for match in pattern.regex.finditer(text):
                source = SourceRef(
                    path=source_path,
                    line=text.count("\n", 0, match.start()) + 1,
                )
                default: str | None = None
                if pattern.default_group is not None and match.lastindex:
                    default = match.group(pattern.default_group)
                found.append(_env_candidate(service, match.group(1), source, default))
    return found


def _scan_compose_service(
    project_root: Path,
    service: str,
    config: dict,
) -> list[EnvCandidate]:
    found: list[EnvCandidate] = []
    compose_source = SourceRef(path="compose", line=None)
    environment = config.get("environment", {})
    if isinstance(environment, dict):
        for name, value in environment.items():
            if not isinstance(name, str):
                continue
            found.append(
                _env_candidate(service, name, compose_source, _compose_default(value))
            )
    elif isinstance(environment, list):
        for entry in environment:
            if not isinstance(entry, str):
                continue
            name, separator, value = entry.partition("=")
            found.append(
                _env_candidate(
                    service,
                    name,
                    compose_source,
                    value if separator else None,
                )
            )

    env_files = config.get("env_file", ())
    if isinstance(env_files, (str, dict)):
        env_files = [env_files]
    if isinstance(env_files, list):
        for entry in env_files:
            raw_path = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(raw_path, str):
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = project_root / path
            if not path.is_file():
                continue
            values = dotenv_values(path)
            for name, value in values.items():
                source = SourceRef(path=_display_path(project_root, path), line=None)
                found.append(_env_candidate(service, name, source, value))
    return found


def _scan_dockerfile_env(
    project_root: Path,
    service: str,
    dockerfile: Path,
) -> list[EnvCandidate]:
    found: list[EnvCandidate] = []
    for line_number, logical_line in _dockerfile_lines(dockerfile):
        if not logical_line.upper().startswith("ENV "):
            continue
        tokens = shlex.split(logical_line[4:].strip())
        pairs: list[tuple[str, str]] = []
        if tokens and all("=" in token for token in tokens):
            pairs = [tuple(token.split("=", 1)) for token in tokens]
        elif len(tokens) >= 2:
            pairs = [(tokens[0], " ".join(tokens[1:]))]
        source = SourceRef(
            path=_display_path(project_root, dockerfile),
            line=line_number,
        )
        for name, value in pairs:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                found.append(_env_candidate(service, name, source, value))
    return found


def _dockerfile_lines(path: Path) -> list[tuple[int, str]]:
    if not path.is_file():
        return []
    logical: list[tuple[int, str]] = []
    buffer = ""
    start_line = 1
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not buffer:
            start_line = line_number
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        buffer += stripped
        if buffer and not buffer.startswith("#"):
            logical.append((start_line, buffer))
        buffer = ""
    if buffer:
        logical.append((start_line, buffer))
    return logical


def _env_candidate(
    service: str,
    name: str,
    source: SourceRef,
    default: str | None,
) -> EnvCandidate:
    defaults = () if default is None else (DefaultValue(value=str(default), source=source),)
    return EnvCandidate(
        service=service,
        name=name,
        sources=(source,),
        defaults=defaults,
    )


def _compose_default(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    match = ENV_INTERPOLATION.fullmatch(text)
    if match:
        return match.group(1)
    return text


def _display_path(project_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return str(resolved)
