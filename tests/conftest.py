from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml


class CliRunner:
    def __init__(self, repo_root: Path, tmp_path: Path) -> None:
        self.repo_root = repo_root
        self.skill_root = repo_root / "skills/package-docker-app"
        self.log_path = tmp_path / "fake-docker.jsonl"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = repo_root / "tests/integration/fake_docker.py"
        fake.chmod(0o755)
        (bin_dir / "docker").symlink_to(fake)
        self.base_env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(self.log_path),
        }
        for name in (
            "FAKE_DOCKER_EXIT",
            "FAKE_DOCKER_STDERR",
            "FAKE_DOCKER_INSPECT",
            "FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG",
        ):
            self.base_env.pop(name, None)

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_env = dict(self.base_env)
        project = _project_argument(args)
        if project is not None:
            compose = next(
                (
                    project / name
                    for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
                    if (project / name).is_file()
                ),
                None,
            )
            if compose is not None:
                command_env["FAKE_DOCKER_COMPOSE_CONFIG"] = json.dumps(
                    yaml.safe_load(compose.read_text(encoding="utf-8"))
                )
        if env:
            command_env.update(env)
        return subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(self.skill_root),
                "docker-package-app",
                *args,
            ],
            cwd=self.repo_root,
            env=command_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def docker_log(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def clear_docker_log(self) -> None:
        self.log_path.unlink(missing_ok=True)


@pytest.fixture
def cli(tmp_path: Path) -> CliRunner:
    return CliRunner(Path(__file__).resolve().parents[1], tmp_path)


@pytest.fixture
def empty_project(tmp_path: Path) -> Path:
    project = tmp_path / "empty-app"
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return project


@pytest.fixture
def compose_project(tmp_path: Path) -> Path:
    project = tmp_path / "compose-app"
    project.mkdir()
    (project / "app.py").write_text(
        'import os\nPORT = os.getenv("PORT", "8000")\n',
        encoding="utf-8",
    )
    (project / "Dockerfile").write_text(
        "FROM scratch\nEXPOSE 8000\n",
        encoding="utf-8",
    )
    (project / "compose.yaml").write_text(
        """services:
  web:
    build: .
    ports:
      - "8080:8000"
""",
        encoding="utf-8",
    )
    return project


@pytest.fixture
def complete_answers(compose_project: Path) -> Path:
    path = compose_project / "answers.json"
    path.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "8000",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _project_argument(args: tuple[str, ...]) -> Path | None:
    for value in args[1:]:
        if not value.startswith("-"):
            candidate = Path(value)
            if candidate.is_dir():
                return candidate
    return None
