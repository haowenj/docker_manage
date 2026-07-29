from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import CliRunner


def test_multi_service_package_is_complete(cli: CliRunner, tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/e2e-multi"
    project = tmp_path / "multi-app"
    shutil.copytree(fixture, project)
    answers = project / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "8000",
                    "env.worker.PORT": "9000",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "18080",
                    "port.worker.9000/tcp.expose": "no",
                    "image.redis.decision": "registry.intra/redis:7-approved",
                    "image.helper.decision": "打包",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    inspect_records = [
        _metadata("sha256:helper"),
        _metadata("sha256:web"),
        _metadata("sha256:worker"),
    ]

    result = cli(
        "run",
        str(project),
        "--non-interactive",
        "--answers",
        str(answers),
        "--app-name",
        "demo",
        "--version",
        "v1",
        "--platform",
        "linux/amd64",
        "--json",
        env={"FAKE_DOCKER_INSPECT": json.dumps(inspect_records)},
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    archive_path = Path(output["archive"])
    assert output["packaged_images"] == [
        "busybox:1.36",
        "docker-manage/demo/web:v1",
        "docker-manage/demo/worker:v1",
    ]
    assert output["reused_images"] == ["registry.intra/redis:7-approved"]
    with tarfile.open(archive_path, "r:gz") as bundle:
        compose = yaml.safe_load(bundle.extractfile("compose.yaml"))
        assert all(
            "build" not in service
            for service in compose["services"].values()
        )
        assert compose["services"]["web"]["ports"][0]["published"] == 18080
        assert "ports" not in compose["services"]["worker"]
        assert compose["services"]["web"]["volumes"][0]["source"] == "./files/config"
        assert bundle.getmember("files/config/app.ini").size > 0
        assert bundle.getmember("manifest.json").size > 0
        assert bundle.getmember("checksums.sha256").size > 0


def test_successful_package_becomes_next_inspection_current_config(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    first_answers = compose_project / "first-answers.json"
    first_answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "9123",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    first_answers.chmod(0o600)

    first = cli(
        "run",
        str(compose_project),
        "--non-interactive",
        "--answers",
        str(first_answers),
        "--app-name",
        "snapshot-app",
        "--version",
        "v1",
        "--json",
        env={"FAKE_DOCKER_INSPECT": json.dumps([_metadata("sha256:web-v1")])},
    )

    assert first.returncode == 0, first.stderr
    snapshot = compose_project / ".docker-manage/.env"
    assert snapshot.read_text(encoding="utf-8") == "PORT='9123'\n"

    inspected = cli("inspect", str(compose_project), "--json")
    assert inspected.returncode == 0, inspected.stderr
    question = next(
        item
        for item in json.loads(inspected.stdout)["questions"]
        if item["id"] == "env.web.PORT"
    )
    assert question["default"] == "9123"
    assert "当前配置值：9123" in question["prompt"]


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
    reason="Docker Compose is required for generated Compose validation",
)
def test_interpolated_host_port_packages_user_selected_mapping(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "interpolated-port-app"
    project.mkdir()
    (project / "Dockerfile").write_text(
        "FROM scratch\nEXPOSE 8000\n",
        encoding="utf-8",
    )
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (project / "compose.yaml").write_text(
        """services:
  web:
    build: .
    ports:
      - "${PDF_TRANS_WEB_PORT:-8000}:8000"
""",
        encoding="utf-8",
    )
    answers = project / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PDF_TRANS_WEB_PORT": "8000",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8322",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)

    result = cli(
        "run",
        str(project),
        "--non-interactive",
        "--answers",
        str(answers),
        "--app-name",
        "interpolated-port",
        "--version",
        "v1",
        "--json",
        env={"FAKE_DOCKER_INSPECT": json.dumps([_metadata("sha256:web")])},
    )

    assert result.returncode == 0, result.stderr
    archive_path = Path(json.loads(result.stdout)["archive"])
    validation_dir = tmp_path / "compose-validation"
    validation_dir.mkdir()
    with tarfile.open(archive_path, "r:gz") as bundle:
        compose_text = bundle.extractfile("compose.yaml").read().decode("utf-8")
        env_text = bundle.extractfile(".env").read().decode("utf-8")

    compose = yaml.safe_load(compose_text)
    assert compose["services"]["web"]["ports"] == [
        {"target": 8000, "published": 8322, "protocol": "tcp"}
    ]
    assert "host_ip: ${PDF_TRANS_WEB_PORT" not in compose_text

    compose_path = validation_dir / "compose.yaml"
    env_path = validation_dir / ".env"
    compose_path.write_text(compose_text, encoding="utf-8")
    env_path.write_text(env_text, encoding="utf-8")
    docker = shutil.which("docker")
    assert docker is not None
    validated = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(compose_path),
            "config",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert validated.returncode == 0, validated.stderr


def _metadata(image_id: str) -> dict[str, object]:
    return {
        "Id": image_id,
        "RepoDigests": [],
        "Os": "linux",
        "Architecture": "amd64",
        "Size": 1024,
    }
