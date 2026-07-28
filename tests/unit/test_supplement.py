import json
from pathlib import Path

import pytest
from docker_package_app.errors import SupplementValidationError
from docker_package_app.models import Inspection, Stage
from docker_package_app.supplement import load_supplement, merge_supplement


def _payload(**updates: object) -> dict:
    payload = {
        "schema_version": 1,
        "generated_files": [],
        "environment": [],
        "ambiguities": [],
    }
    payload.update(updates)
    return payload


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_supplement_adds_explicit_env(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    path = _write(
        tmp_path / "supplement.json",
        _payload(
            environment=[
                {
                    "service": "web",
                    "name": "API_URL",
                    "default": None,
                    "path": "app.py",
                    "line": 1,
                }
            ]
        ),
    )
    inspection = Inspection(
        run_id="run-1",
        project_root=str(tmp_path),
        stage=Stage.NEEDS_MODEL,
        model_reasons=("unknown_environment_api",),
    )

    supplement = load_supplement(
        path,
        tmp_path,
        tmp_path / ".docker-manage/generated",
        {"web"},
    )
    merged = merge_supplement(inspection, supplement)

    assert merged.stage is Stage.INSPECTED
    assert merged.model_reasons == ()
    assert [(item.service, item.name) for item in merged.env] == [("web", "API_URL")]


def test_generated_file_must_stay_in_generated_root(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "supplement.json",
        _payload(
            generated_files=[
                {"kind": "dockerfile", "path": "../../Dockerfile"}
            ]
        ),
    )

    with pytest.raises(SupplementValidationError, match="生成"):
        load_supplement(path, tmp_path, tmp_path / ".docker-manage/generated", {"web"})


def test_unknown_service_and_extra_fields_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("", encoding="utf-8")
    path = _write(
        tmp_path / "supplement.json",
        _payload(
            environment=[
                {
                    "service": "admin",
                    "name": "TOKEN",
                    "default": "secret",
                    "path": "app.py",
                    "line": 1,
                    "command": "docker image rm all",
                }
            ]
        ),
    )

    with pytest.raises(SupplementValidationError):
        load_supplement(path, tmp_path, tmp_path / ".docker-manage/generated", {"web"})
