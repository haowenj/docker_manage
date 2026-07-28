import subprocess

import pytest

from docker_package_app.command import CommandRunner
from docker_package_app.errors import PackageError


def test_runner_never_uses_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CommandRunner().run(["docker", "version"])

    assert result.stdout == "ok"
    assert seen["argv"] == ["docker", "version"]
    assert seen["kwargs"]["shell"] is False  # type: ignore[index]


def test_runner_raises_structured_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 7, "", "daemon unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PackageError) as caught:
        CommandRunner().run(["docker", "version"])

    assert caught.value.details == "daemon unavailable"
    assert "docker version" in caught.value.message

