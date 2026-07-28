from __future__ import annotations

from pathlib import Path

import pytest
from docker_package_app.command import CommandResult
from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import PackageError
from docker_package_app.models import (
    DiskEstimate,
    EnvAssignment,
    ImageAction,
    ImageAssignment,
    PackagePlan,
    PortAssignment,
)
from docker_package_app.render import (
    render_deployment,
    validate_deployment,
    write_deployment,
)


class RecordingRunner:
    def __init__(self, *, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.stderr = stderr

    def run(self, argv, **kwargs):
        command = list(argv)
        self.calls.append(command)
        return CommandResult(command, 0, self.stdout, self.stderr)


def _plan(root: Path) -> PackagePlan:
    return PackagePlan(
        run_id="run-1",
        project_root=str(root),
        app_name="demo",
        version="v1",
        compose_project_name="demo",
        platform="linux/amd64",
        environment=(
            EnvAssignment(
                service="web",
                container_name="PORT",
                artifact_name="WEB_PORT",
                value="8000",
            ),
            EnvAssignment(
                service="web",
                container_name="API_KEY",
                artifact_name="WEB_API_KEY",
                value="a b#c",
            ),
        ),
        ports=(
            PortAssignment(
                service="web",
                container_port=8000,
                exposed=True,
                host_ip="127.0.0.1",
                host_port=18000,
            ),
            PortAssignment(
                service="web",
                container_port=9000,
                exposed=False,
            ),
        ),
        images=(
            ImageAssignment(
                service="web",
                original_image="demo:dev",
                final_image="docker-manage/demo/web:v1",
                action=ImageAction.BUILD,
                platform="linux/amd64",
            ),
        ),
        disk=DiskEstimate(known_input_bytes=0, free_bytes=1_000_000_000),
    )


def _base(root: Path) -> ComposeDocument:
    return ComposeDocument.from_data(
        root,
        {
            "name": "original",
            "services": {
                "web": {
                    "build": ".",
                    "image": "demo:dev",
                    "env_file": [".env"],
                    "environment": {"PORT": "8000", "API_KEY": "old"},
                    "ports": ["8080:8000", "9000"],
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "./config",
                            "target": "/app/config",
                            "read_only": True,
                        },
                        {"type": "volume", "source": "data", "target": "/data"},
                    ],
                    "configs": [{"source": "app-config", "target": "/app/app.ini"}],
                    "restart": "unless-stopped",
                    "healthcheck": {"test": ["CMD", "true"]},
                }
            },
            "configs": {"app-config": {"file": "./app.ini"}},
            "volumes": {"data": {}},
            "networks": {"default": {}},
        },
    )


def test_rendered_compose_is_deploy_only(tmp_path: Path) -> None:
    base = _base(tmp_path)
    rendered = render_deployment(
        base,
        _plan(tmp_path),
        {"./config": "./files/config", "./app.ini": "./files/app.ini"},
    )

    web = rendered["services"]["web"]
    assert "build" not in web
    assert "env_file" not in web
    assert web["image"] == "docker-manage/demo/web:v1"
    assert web["platform"] == "linux/amd64"
    assert web["environment"] == {
        "API_KEY": "${WEB_API_KEY}",
        "PORT": "${WEB_PORT}",
    }
    assert web["ports"] == [
        {
            "target": 8000,
            "published": 18000,
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert web["volumes"][0]["source"] == "./files/config"
    assert web["volumes"][1]["source"] == "data"
    assert rendered["configs"]["app-config"]["file"] == "./files/app.ini"
    assert web["restart"] == "unless-stopped"
    assert web["healthcheck"] == {"test": ["CMD", "true"]}
    assert base.service("web")["build"] == "."


def test_write_and_validate_deployment(tmp_path: Path) -> None:
    rendered = render_deployment(_base(tmp_path), _plan(tmp_path), {})
    compose_path, env_path = write_deployment(
        rendered,
        {"WEB_PORT": "8000", "WEB_API_KEY": "a b#c"},
        tmp_path / "payload",
    )
    runner = RecordingRunner()

    validate_deployment(compose_path, env_path, runner)

    assert compose_path.stat().st_mode & 0o777 == 0o600
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "WEB_API_KEY='a b#c'",
        "WEB_PORT='8000'",
    ]
    assert runner.calls == [
        [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(compose_path),
            "config",
        ]
    ]


def test_validation_rejects_unset_variable_warning(tmp_path: Path) -> None:
    runner = RecordingRunner(
        stderr='The "MISSING" variable is not set. Defaulting to a blank string.\n'
    )

    with pytest.raises(PackageError, match="未设置的变量"):
        validate_deployment(tmp_path / "compose.yaml", tmp_path / ".env", runner)
