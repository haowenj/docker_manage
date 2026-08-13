from pathlib import Path

import pytest
from docker_package_app.models import (
    CurrentPortSelection,
    DefaultValue,
    DiskEstimate,
    EnvCandidate,
    FileAction,
    FileAssignment,
    FileCandidate,
    Inspection,
    PackagePlan,
    PortCandidate,
    SourceRef,
    Stage,
)
from pydantic import ValidationError


def test_file_assignment_records_dependency_kind() -> None:
    assignment = FileAssignment(
        service="web",
        original_value="./data",
        resolved_path="/project/data",
        kind="bind",
        action=FileAction.KEEP_SERVER_PATH,
    )

    restored = FileAssignment.model_validate_json(assignment.model_dump_json())

    assert restored.kind == "bind"


def test_file_candidate_current_action_is_optional_and_round_trips() -> None:
    old = FileCandidate.model_validate(
        {
            "service": "web",
            "compose_value": "./data",
            "resolved_path": "/project/data",
            "kind": "bind",
            "inside_project": True,
            "project_path": "data",
            "estimated_size": 0,
        }
    )
    assert old.current_action is None

    restored = FileCandidate.model_validate_json(
        old.model_copy(
            update={"current_action": FileAction.COPY}
        ).model_dump_json()
    )

    assert restored.current_action is FileAction.COPY


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


def test_port_candidate_current_selection_is_optional_and_round_trips() -> None:
    old = PortCandidate.model_validate(
        {"service": "web", "container_port": 8000, "host_port": 8080}
    )
    assert old.current is None

    current = CurrentPortSelection(exposed=True, host_port=8322)
    restored = PortCandidate.model_validate_json(
        PortCandidate(
            service="web",
            container_port=8000,
            host_port=8080,
            current=current,
        ).model_dump_json()
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
