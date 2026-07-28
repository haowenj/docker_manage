from __future__ import annotations

import os
from pathlib import Path

import pytest
from docker_package_app.command import CommandRunner
from docker_package_app.docker import DockerEngine
from docker_package_app.errors import PackageError
from docker_package_app.models import Stage


@pytest.fixture
def fake_docker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = Path(__file__).with_name("fake_docker.py")
    source.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").symlink_to(source)
    log = tmp_path / "docker.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    return log


@pytest.mark.parametrize(
    ("operation", "expected_command"),
    [
        ("build", "docker compose"),
        ("pull", "docker pull"),
    ],
)
def test_docker_mutation_failure_has_stage_hint_and_full_stderr(
    operation: str,
    expected_command: str,
    fake_docker_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "daemon rejected operation\nsecond diagnostic line"
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "23")
    monkeypatch.setenv("FAKE_DOCKER_STDERR", stderr)
    engine = DockerEngine(CommandRunner())

    with pytest.raises(PackageError) as caught:
        if operation == "build":
            engine.build(tmp_path / "build.compose.yaml", ["web"], "linux/amd64")
        else:
            engine.pull("redis:7", "linux/amd64")

    error = caught.value
    assert error.stage is Stage.CONFIRMED
    assert error.hint and f"Docker {operation}" in error.hint
    assert "请检查" in error.hint
    assert error.details == stderr
    assert stderr not in error.message
    assert expected_command in error.message
    assert "操作失败" in error.message
