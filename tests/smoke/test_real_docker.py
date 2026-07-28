from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_SMOKE") != "1",
    reason="set RUN_DOCKER_SMOKE=1 to run the daemon-backed smoke test",
)
def test_saved_scratch_image_can_be_removed_and_loaded(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    skill = repo / "skills/package-docker-app"
    fixture = repo / "tests/fixtures/scratch-compose"
    project = tmp_path / "scratch-compose"
    shutil.copytree(fixture, project)
    answers = project / "answers.json"
    answers.write_text('{"values": {}}\n', encoding="utf-8")
    answers.chmod(0o600)
    version = f"smoke-{uuid4().hex[:8]}"
    image = f"docker-manage/docker-manage-smoke/app:{version}"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FAKE_DOCKER_")
    }

    daemon = subprocess.run(
        ["docker", "info"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert daemon.returncode == 0, daemon.stderr

    try:
        packaged = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(skill),
                "docker-package-app",
                "run",
                str(project),
                "--non-interactive",
                "--answers",
                str(answers),
                "--app-name",
                "docker-manage-smoke",
                "--version",
                version,
                "--platform",
                "linux/amd64",
                "--json",
            ],
            cwd=repo,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert packaged.returncode == 0, packaged.stderr
        archive = Path(json.loads(packaged.stdout)["archive"])
        original_id = _image_id(image, environment)

        image_tar = tmp_path / "images.tar"
        with tarfile.open(archive, "r:gz") as bundle:
            source = bundle.extractfile("images.tar")
            assert source is not None
            with image_tar.open("wb") as destination:
                shutil.copyfileobj(source, destination)

        removed = subprocess.run(
            ["docker", "image", "rm", image],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
        loaded = subprocess.run(
            ["docker", "image", "load", "--input", str(image_tar)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert loaded.returncode == 0, loaded.stderr
        assert _image_id(image, environment) == original_id
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", image],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )


def _image_id(image: str, environment: dict[str, str]) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
