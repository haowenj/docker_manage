from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import CliRunner
from docker_package_app.cli import _resolve_answers
from docker_package_app.models import Question


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


def test_interactive_answers_batch_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    questions = (
        Question(
            id="env.web.PORT",
            kind="env",
            prompt="设置 web.PORT",
            default="8000",
        ),
        Question(
            id="env.web.LOG_LEVEL",
            kind="env",
            prompt="设置 web.LOG_LEVEL",
            default="info",
        ),
        Question(
            id="port.web.8000/tcp.expose",
            kind="port_expose",
            prompt="是否暴露端口",
            default="yes",
            choices=("yes", "no"),
        ),
        Question(
            id="port.web.8000/tcp.host",
            kind="port_host",
            prompt="设置主机端口",
            default="8080",
        ),
    )
    replies = iter(("1: 9000", "", "", ""))
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("builtins.input", answer)

    result = _resolve_answers(questions, {}, non_interactive=False)

    assert result.values == {
        "env.web.PORT": "9000",
        "env.web.LOG_LEVEL": "info",
        "port.web.8000/tcp.expose": "yes",
        "port.web.8000/tcp.host": "8080",
    }
    output = capsys.readouterr().out
    assert "请设置环境变量" in output
    assert "1. 设置 web.PORT，默认值：8000" in output
    assert "2. 设置 web.LOG_LEVEL，默认值：info" in output
    assert len(prompts) == 4


def test_cli_help_and_argument_errors_use_chinese(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    help_result = cli("--help")
    assert help_result.returncode == 0
    assert "用法：" in help_result.stdout
    assert "选项" in help_result.stdout
    assert "usage:" not in help_result.stdout
    assert "options:" not in help_result.stdout

    missing_command = cli()
    assert missing_command.returncode == 2
    assert "参数错误" in missing_command.stderr
    assert "缺少必填参数" in missing_command.stderr
    assert ": error:" not in missing_command.stderr

    unknown_argument = cli("inspect", str(compose_project), "--unknown")
    assert unknown_argument.returncode == 2
    assert "参数错误" in unknown_argument.stderr
    assert "无法识别的参数" in unknown_argument.stderr
