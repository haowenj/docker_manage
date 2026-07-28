from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import CliRunner


def test_missing_docker_files_are_generated_only_in_tool_directory(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/no-docker-python"
    project = tmp_path / "python-app"
    shutil.copytree(fixture, project)
    original_files = _project_files(project)

    initial = cli("inspect", str(project), "--json")
    assert initial.returncode == 20, initial.stderr
    run_id = json.loads(initial.stdout)["run_id"]

    generated = project / ".docker-manage/generated"
    dockerfile = generated / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"app.py\"]\n",
        encoding="utf-8",
    )
    compose = generated / "compose.yaml"
    compose_data = {
        "services": {
            "web": {
                "build": {
                    "context": ".",
                    "dockerfile": ".docker-manage/generated/Dockerfile",
                },
                "environment": {"PORT": "${PORT:-8000}"},
                "ports": ["8000:8000"],
            }
        }
    }
    compose.write_text(
        """services:
  web:
    build:
      context: .
      dockerfile: .docker-manage/generated/Dockerfile
    environment:
      PORT: ${PORT:-8000}
    ports:
      - "8000:8000"
""",
        encoding="utf-8",
    )
    supplement = generated / "supplement.json"
    supplement.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_files": [
                    {
                        "kind": "dockerfile",
                        "path": ".docker-manage/generated/Dockerfile",
                    },
                    {
                        "kind": "compose",
                        "path": ".docker-manage/generated/compose.yaml",
                    },
                ],
                "environment": [
                    {
                        "service": "web",
                        "name": "PORT",
                        "default": "8000",
                        "path": "app.py",
                        "line": 3,
                    }
                ],
                "ambiguities": [
                    {
                        "id": "startup.web",
                        "prompt": "Choose the web startup command",
                        "choices": ["python app.py", "python -m app"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    resumed = cli(
        "inspect",
        str(project),
        "--run-id",
        run_id,
        "--supplement",
        str(supplement),
        "--json",
        env={"FAKE_DOCKER_COMPOSE_CONFIG": json.dumps(compose_data)},
    )

    assert resumed.returncode == 20, resumed.stderr
    body = json.loads(resumed.stdout)
    assert body["stage"] == "needs_model"
    assert [question["id"] for question in body["questions"]] == [
        "model.startup.web",
    ]

    supplement_body = json.loads(supplement.read_text(encoding="utf-8"))
    supplement_body["ambiguities"] = []
    supplement.write_text(json.dumps(supplement_body), encoding="utf-8")
    completed = cli(
        "inspect",
        str(project),
        "--run-id",
        run_id,
        "--supplement",
        str(supplement),
        "--json",
        env={"FAKE_DOCKER_COMPOSE_CONFIG": json.dumps(compose_data)},
    )

    assert completed.returncode == 0, completed.stderr
    completed_body = json.loads(completed.stdout)
    assert completed_body["stage"] == "inspected"
    assert [question["id"] for question in completed_body["questions"]] == [
        "env.web.PORT",
        "port.web.8000/tcp.expose",
        "port.web.8000/tcp.host",
    ]
    assert _project_files(project, exclude_tool_state=True) == original_files
    assert not (project / "Dockerfile").exists()
    assert not (project / "compose.yaml").exists()


def _project_files(project: Path, *, exclude_tool_state: bool = False) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(project)
        if exclude_tool_state and relative.parts[0] == ".docker-manage":
            continue
        result[relative.as_posix()] = path.read_bytes()
    return result
