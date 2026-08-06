from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from docker_package_app.command import CommandRunner
from docker_package_app.compose import ComposeDocument


def _has_docker_compose() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    result = subprocess.run(
        [docker, "compose", "version"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    not _has_docker_compose(),
    reason="Docker Compose is required for path-preservation validation",
)
def test_real_compose_keeps_relative_bind_source(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(
        "services:\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        "    volumes:\n"
        "      - ./data:/app/data\n",
        encoding="utf-8",
    )

    document = ComposeDocument.load(
        tmp_path,
        [compose_path],
        [],
        CommandRunner(),
    )

    assert document.service("app")["volumes"][0]["source"] == "./data"


@pytest.mark.skipif(
    not _has_docker_compose(),
    reason="Docker Compose is required for interpolation validation",
)
def test_real_compose_resolves_nested_data_directory_default(tmp_path: Path) -> None:
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(
        "services:\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        "    volumes:\n"
        "      - ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}:${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}\n",
        encoding="utf-8",
    )

    document = ComposeDocument.load(
        tmp_path,
        [compose_path],
        [],
        CommandRunner(),
        environment={
            "DOCKER_MANAGE_DATA_DIR": "/data/docker-manage-server",
            "PWD": str(tmp_path),
        },
        interpolate=True,
    )

    volume = document.service("app")["volumes"][0]
    assert volume["source"] == "/data/docker-manage-server"
    assert volume["target"] == "/data/docker-manage-server"
