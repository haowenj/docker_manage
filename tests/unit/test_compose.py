import json
from pathlib import Path

import pytest
import yaml

from docker_package_app.command import CommandResult
from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import UsageError


class ComposeRunner:
    def __init__(self, document: dict) -> None:
        self.document = document
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs):
        call = list(argv)
        self.calls.append(call)
        return CommandResult(call, 0, json.dumps(self.document), "")


def test_compose_classifies_build_before_image() -> None:
    fixture = Path("tests/fixtures/multi-compose").resolve()
    merged = {
        "services": {
            "web": {
                "build": {"context": ".", "dockerfile": "Dockerfile"},
                "image": "example/web:dev",
                "environment": {"LOG_LEVEL": "debug"},
            },
            "worker": {"build": {"context": "worker"}},
            "redis": {"image": "redis:7"},
        }
    }
    runner = ComposeRunner(merged)

    document = ComposeDocument.load(
        fixture,
        [fixture / "compose.yaml", fixture / "compose.override.yaml"],
        ["debug"],
        runner,
    )

    assert document.build_services() == ("web", "worker")
    assert document.image_services() == ("redis",)
    assert document.data["services"]["web"]["environment"]["LOG_LEVEL"] == "debug"
    assert runner.calls == [
        [
            "docker",
            "compose",
            "--project-directory",
            str(fixture),
            "-f",
            str(fixture / "compose.yaml"),
            "-f",
            str(fixture / "compose.override.yaml"),
            "--profile",
            "debug",
            "config",
            "--format",
            "json",
            "--no-interpolate",
        ]
    ]


def test_compose_dump_is_structurally_readable(tmp_path: Path) -> None:
    document = ComposeDocument.from_data(
        tmp_path,
        {"services": {"web": {"image": "demo:1"}}},
    )

    output = tmp_path / "compose.yaml"
    document.dump(output)

    assert yaml.safe_load(output.read_text(encoding="utf-8")) == document.data


def test_compose_rejects_non_mapping_services(tmp_path: Path) -> None:
    runner = ComposeRunner({"services": []})

    with pytest.raises(UsageError, match="services"):
        ComposeDocument.load(tmp_path, [tmp_path / "compose.yaml"], [], runner)
