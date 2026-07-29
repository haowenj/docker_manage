import stat
from pathlib import Path

import pytest
from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import AnswerRequired
from docker_package_app.files import (
    _make_bind_writable,
    discover_file_dependencies,
    materialize_files,
)
from docker_package_app.models import FileAction


def _compose(root: Path, source: str) -> ComposeDocument:
    return ComposeDocument.from_data(
        root,
        {
            "services": {
                "web": {
                    "image": "demo:1",
                    "volumes": [
                        {"type": "bind", "source": source, "target": "/app/config"}
                    ],
                    "configs": [{"source": "app-config", "target": "/app/app.ini"}],
                }
            },
            "configs": {"app-config": {"file": "./app.ini"}},
        },
    )


def test_project_relative_bind_is_copied_without_tool_state(tmp_path: Path) -> None:
    source = tmp_path / "config"
    source.mkdir()
    (source / "app.ini").write_text("mode=prod\n", encoding="utf-8")
    (source / ".docker-manage").mkdir()
    (source / ".docker-manage/old.tar.gz").write_bytes(b"large")
    (tmp_path / "app.ini").write_text("mode=prod\n", encoding="utf-8")
    candidates = discover_file_dependencies(_compose(tmp_path, "./config"), tmp_path)
    bind = next(item for item in candidates if item.kind == "bind")

    result = materialize_files(
        candidates,
        {item.resolved_path: FileAction.COPY for item in candidates},
        tmp_path / "payload",
    )

    assert bind.inside_project is True
    assert (tmp_path / "payload/files/config/app.ini").exists()
    assert not (tmp_path / "payload/files/config/.docker-manage").exists()
    assert result.rewrites["./config"] == "./files/config"


def test_symlink_outside_project_requires_server_path_decision(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-shared-secret"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "secret-link").symlink_to(outside)
    (tmp_path / "app.ini").write_text("", encoding="utf-8")
    candidates = discover_file_dependencies(_compose(tmp_path, "./secret-link"), tmp_path)
    bind = next(item for item in candidates if item.kind == "bind")

    assert bind.inside_project is False
    with pytest.raises(AnswerRequired):
        materialize_files(candidates, {}, tmp_path / "payload")

    result = materialize_files(
        candidates,
        {bind.resolved_path: FileAction.KEEP_SERVER_PATH},
        tmp_path / "payload",
    )
    assert result.server_paths == (str(outside.resolve()),)


def test_copied_bind_is_world_readable_and_writable_recursively(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config"
    nested = source / "nested"
    nested.mkdir(parents=True)
    file_path = nested / "app.ini"
    file_path.write_text("mode=prod\n", encoding="utf-8")
    source.chmod(0o750)
    nested.chmod(0o700)
    file_path.chmod(0o600)
    candidate = next(
        item
        for item in discover_file_dependencies(_compose(tmp_path, "./config"), tmp_path)
        if item.kind == "bind"
    )

    materialize_files(
        (candidate,),
        {candidate.resolved_path: FileAction.COPY},
        tmp_path / "payload",
    )

    copied = tmp_path / "payload/files/config"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o777
    assert stat.S_IMODE((copied / "nested").stat().st_mode) == 0o777
    assert stat.S_IMODE((copied / "nested/app.ini").stat().st_mode) == 0o666


def test_bind_permission_normalization_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    outside.chmod(0o600)
    copied_bind = tmp_path / "payload/files/bind"
    copied_bind.mkdir(parents=True)
    (copied_bind / "link").symlink_to(outside)

    _make_bind_writable(copied_bind)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert (copied_bind / "link").is_symlink()


def test_copied_config_permissions_are_preserved(tmp_path: Path) -> None:
    config_source = tmp_path / "app.ini"
    config_source.write_text("secret=true\n", encoding="utf-8")
    config_source.chmod(0o600)
    candidates = tuple(
        item
        for item in discover_file_dependencies(
            _compose(tmp_path, "./unused-bind"),
            tmp_path,
        )
        if item.kind == "config"
    )

    materialize_files(
        candidates,
        {item.resolved_path: FileAction.COPY for item in candidates},
        tmp_path / "payload",
    )

    assert stat.S_IMODE(
        (tmp_path / "payload/files/app.ini").stat().st_mode
    ) == 0o600


def test_named_volume_is_not_a_file_dependency(tmp_path: Path) -> None:
    compose = ComposeDocument.from_data(
        tmp_path,
        {
            "services": {
                "db": {"image": "postgres:16", "volumes": ["db-data:/var/lib/postgresql/data"]}
            },
            "volumes": {"db-data": {}},
        },
    )

    assert discover_file_dependencies(compose, tmp_path) == ()
