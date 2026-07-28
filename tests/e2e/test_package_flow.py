from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

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


def _metadata(image_id: str) -> dict[str, object]:
    return {
        "Id": image_id,
        "RepoDigests": [],
        "Os": "linux",
        "Architecture": "amd64",
        "Size": 1024,
    }
