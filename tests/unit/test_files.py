from pathlib import Path

import pytest
from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import AnswerRequired
from docker_package_app.files import discover_file_dependencies, materialize_files
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
