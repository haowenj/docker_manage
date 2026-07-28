from datetime import UTC, datetime
from pathlib import Path

from docker_package_app.command import CommandResult
from docker_package_app.discovery import (
    default_identity,
    discover_docker_files,
    preflight,
)


class RecordingRunner:
    def __init__(self, git_stdout: str = "abc1234\n") -> None:
        self.calls: list[list[str]] = []
        self.git_stdout = git_stdout

    def run(self, argv, **kwargs):
        call = list(argv)
        self.calls.append(call)
        if call[:2] == ["git", "rev-parse"]:
            return CommandResult(call, 0, self.git_stdout, "")
        return CommandResult(call, 0, "ok", "")


def test_discovery_returns_ambiguity_and_profiles(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "Dockerfile.worker").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  web:\n    image: demo\n    profiles: [debug, tools]\n",
        encoding="utf-8",
    )

    result = discover_docker_files(tmp_path)

    assert result.dockerfiles == ("Dockerfile", "Dockerfile.worker")
    assert result.compose_files == ("compose.yaml",)
    assert result.profiles == ("debug", "tools")
    assert result.requires_dockerfile_choice is True


def test_default_identity_uses_directory_and_git_sha(tmp_path: Path) -> None:
    project = tmp_path / "My Demo_App"
    project.mkdir()

    identity = default_identity(
        project,
        RecordingRunner(),
        datetime(2026, 7, 28, 0, 0, tzinfo=UTC),
    )

    assert identity == ("my-demo-app", "abc1234")


def test_preflight_runs_required_probes_and_reports_free_space(tmp_path: Path) -> None:
    runner = RecordingRunner()

    report = preflight(tmp_path, runner)

    assert report.free_disk_bytes > 0
    assert runner.calls[:3] == [
        ["docker", "version", "--format", "{{.Client.Version}}|{{.Server.Version}}"],
        ["docker", "compose", "version"],
        ["docker", "buildx", "version"],
    ]
