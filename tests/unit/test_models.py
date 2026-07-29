from pathlib import Path

import pytest
from docker_package_app.models import (
    DefaultValue,
    DiskEstimate,
    EnvCandidate,
    Inspection,
    PackagePlan,
    SourceRef,
    Stage,
)
from pydantic import ValidationError


def test_inspection_round_trips_with_schema_version() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/workspace/app",
        stage=Stage.INSPECTED,
        env=(
            EnvCandidate(
                service="web",
                name="PORT",
                sources=(SourceRef(path="app.py", line=8),),
            ),
        ),
    )

    restored = Inspection.model_validate_json(inspection.model_dump_json())

    assert restored == inspection
    assert restored.schema_version == 1


def test_env_candidate_current_value_is_optional_and_round_trips() -> None:
    old = EnvCandidate.model_validate({"service": "web", "name": "PORT"})
    assert old.current is None

    current = DefaultValue(
        value="8322",
        source=SourceRef(path=".docker-manage/.env"),
    )
    restored = EnvCandidate.model_validate_json(
        EnvCandidate(service="web", name="PORT", current=current).model_dump_json()
    )

    assert restored.current == current


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SourceRef(path="app.py", line=8, command="rm -rf data")


def test_package_plan_uses_service_level_assignments() -> None:
    plan = PackagePlan(
        run_id="run-1",
        project_root=str(Path("/workspace/app")),
        app_name="demo",
        version="v1",
        compose_project_name="demo",
        disk=DiskEstimate(known_input_bytes=0, free_bytes=1024),
    )

    assert plan.platform == "linux/amd64"
    assert plan.environment == ()
    assert plan.images == ()
