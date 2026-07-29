from pathlib import Path

import pytest
from docker_package_app.current_config import (
    CURRENT_ENV_SOURCE,
    artifact_component,
    attach_current_values,
)
from docker_package_app.errors import UsageError
from docker_package_app.models import DefaultValue, EnvCandidate, SourceRef


def _candidate(service: str, name: str, default: str = "declared") -> EnvCandidate:
    source = SourceRef(path=f"{service}.py", line=1)
    return EnvCandidate(
        service=service,
        name=name,
        defaults=(DefaultValue(value=default, source=source),),
        sources=(source,),
    )


def test_missing_snapshot_preserves_discovered_candidates(tmp_path: Path) -> None:
    candidates = (_candidate("web", "PORT", "8000"),)

    attached = attach_current_values(tmp_path, candidates)

    assert attached == candidates
    assert attached[0].current is None


def test_service_key_precedes_generic_and_unknown_keys_are_ignored(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text(
        "PORT='7000'\nWEB_PORT='8000'\nEMPTY=\nUNKNOWN='ignored'\n",
        encoding="utf-8",
    )
    candidates = (
        _candidate("web", "PORT"),
        _candidate("worker", "PORT"),
        _candidate("web", "EMPTY"),
    )

    attached = attach_current_values(tmp_path, candidates)

    assert [item.current.value if item.current else None for item in attached] == [
        "8000",
        "7000",
        "",
    ]
    assert all(
        item.current is not None
        and item.current.source == SourceRef(path=CURRENT_ENV_SOURCE)
        for item in attached
    )
    assert {(item.service, item.name) for item in attached} == {
        ("web", "PORT"),
        ("worker", "PORT"),
        ("web", "EMPTY"),
    }


def test_matched_key_without_value_is_rejected(tmp_path: Path) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT\n", encoding="utf-8")

    with pytest.raises(UsageError, match="PORT.*没有具体值"):
        attach_current_values(tmp_path, (_candidate("web", "PORT"),))


def test_artifact_component_matches_existing_service_prefix_rule() -> None:
    assert artifact_component("api-web") == "API_WEB"
    assert artifact_component("worker_2") == "WORKER_2"
