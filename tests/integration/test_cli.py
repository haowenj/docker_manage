from __future__ import annotations

import json
from pathlib import Path

from conftest import CliRunner


def test_inspect_missing_docker_files_requests_model(
    cli: CliRunner,
    empty_project: Path,
) -> None:
    result = cli("inspect", str(empty_project), "--json")

    assert result.returncode == 20, result.stderr
    body = json.loads(result.stdout)
    assert body["stage"] == "needs_model"
    assert set(body["model_reasons"]) == {
        "dockerfile_missing",
        "compose_missing",
    }
    assert body["run_id"]


def test_noninteractive_plan_rejects_missing_answers(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    inspected = cli("inspect", str(compose_project), "--json")
    assert inspected.returncode == 0, inspected.stderr
    run_id = json.loads(inspected.stdout)["run_id"]

    result = cli(
        "plan",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(compose_project / "missing-answers.json"),
    )

    assert result.returncode == 10
    assert "missing answer" in result.stderr.lower()


def test_dry_run_stops_before_docker_mutations(
    cli: CliRunner,
    compose_project: Path,
    complete_answers: Path,
) -> None:
    result = cli(
        "run",
        str(compose_project),
        "--dry-run",
        "--non-interactive",
        "--answers",
        str(complete_answers),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["stage"] == "planned"
    mutations = {
        ("compose", "build"),
        ("pull", "--platform"),
        ("image", "save"),
    }
    assert all(tuple(call[:2]) not in mutations for call in cli.docker_log())


def test_package_rejects_wrong_plan_hash_before_docker_mutation(
    cli: CliRunner,
    compose_project: Path,
    complete_answers: Path,
) -> None:
    inspected = cli("inspect", str(compose_project), "--json")
    run_id = json.loads(inspected.stdout)["run_id"]
    planned = cli(
        "plan",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(complete_answers),
        "--json",
    )
    assert planned.returncode == 0, planned.stderr
    cli.clear_docker_log()

    result = cli(
        "package",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(complete_answers),
        "--confirm-plan-hash",
        "0" * 64,
        "--json",
    )

    assert result.returncode == 1
    assert "plan hash" in result.stderr.lower()
    assert cli.docker_log() == []
