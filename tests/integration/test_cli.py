from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import CliRunner
from docker_package_app.cli import _resolve_answers, _translate_argparse_error
from docker_package_app.models import Question
from docker_package_app.questions import _file_question_id


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
    assert "缺少答案" in result.stderr


def test_inspect_and_plan_use_current_environment_snapshot(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='9123'\nUNKNOWN='ignored'\n", encoding="utf-8")

    inspected = cli("inspect", str(compose_project), "--json")

    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    assert [(item["service"], item["name"]) for item in body["env"]] == [
        ("web", "PORT")
    ]
    assert body["env"][0]["current"] == {
        "value": "9123",
        "source": {"path": ".docker-manage/.env", "line": None},
    }
    env_question = next(
        question for question in body["questions"] if question["id"] == "env.web.PORT"
    )
    assert env_question["default"] == "9123"
    assert "当前配置值：9123" in env_question["prompt"]

    answers = compose_project / "current-answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": env_question["default"],
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    planned = cli(
        "plan",
        str(compose_project),
        "--run-id",
        body["run_id"],
        "--non-interactive",
        "--answers",
        str(answers),
        "--json",
    )

    assert planned.returncode == 0, planned.stderr
    environment = json.loads(planned.stdout)["plan"]["environment"]
    assert environment[0]["value"] == "9123"


def test_inspect_and_plan_use_current_port_snapshot(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/ports.json"
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ports": [
                    {
                        "service": "web",
                        "container_port": 8000,
                        "protocol": "tcp",
                        "exposed": True,
                        "host_port": 8322,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    inspected = cli("inspect", str(compose_project), "--json")

    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    port = body["ports"][0]
    assert port["current"] == {"exposed": True, "host_port": 8322}
    questions = {item["id"]: item for item in body["questions"]}
    assert questions["port.web.8000/tcp.expose"]["default"] == "yes"
    assert questions["port.web.8000/tcp.host"]["default"] == "8322"

    answers = compose_project / "current-port-answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "8000",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8322",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    planned = cli(
        "plan",
        str(compose_project),
        "--run-id",
        body["run_id"],
        "--non-interactive",
        "--answers",
        str(answers),
        "--json",
    )

    assert planned.returncode == 0, planned.stderr
    assert json.loads(planned.stdout)["plan"]["ports"][0]["host_port"] == 8322


def test_inspect_uses_current_mount_snapshot(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "current-mount"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (project / "compose.yaml").write_text(
        """services:
  web:
    image: busybox:1.36
    volumes:
      - ./data:/data
""",
        encoding="utf-8",
    )
    snapshot = project / ".docker-manage/mounts.json"
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mounts": [
                    {"resolved_path": str(data.resolve()), "action": "copy"}
                ],
            }
        ),
        encoding="utf-8",
    )

    inspected = cli("inspect", str(project), "--json")

    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    assert body["files"][0]["current_action"] == "copy"
    question = next(
        item
        for item in body["questions"]
        if item["id"] == _file_question_id(str(data.resolve()))
    )
    assert question["default"] == "copy"
    assert "当前配置值：copy" in question["prompt"]
    assert "来源：.docker-manage/mounts.json" in question["prompt"]


def test_inspect_rejects_external_current_copy_action(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "external-current-copy"
    project.mkdir()
    external = tmp_path / "server-data"
    external.mkdir()
    (project / "compose.yaml").write_text(
        f"""services:
  web:
    image: busybox:1.36
    volumes:
      - {external}:/data
""",
        encoding="utf-8",
    )
    snapshot = project / ".docker-manage/mounts.json"
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mounts": [
                    {
                        "resolved_path": str(external.resolve()),
                        "action": "copy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    inspected = cli("inspect", str(project), "--json")

    assert inspected.returncode == 2
    assert "项目目录外" in inspected.stderr
    assert "copy" in inspected.stderr


def test_dry_run_stops_before_docker_mutations(
    cli: CliRunner,
    compose_project: Path,
    complete_answers: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='last-good'\n", encoding="utf-8")
    ports_snapshot = compose_project / ".docker-manage/ports.json"
    ports_snapshot.write_text(
        '{"schema_version":1,"ports":[]}\n',
        encoding="utf-8",
    )
    previous_ports = ports_snapshot.read_text(encoding="utf-8")

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
    assert snapshot.read_text(encoding="utf-8") == "PORT='last-good'\n"
    assert ports_snapshot.read_text(encoding="utf-8") == previous_ports
    mutations = {
        ("compose", "build"),
        ("pull", "--platform"),
        ("image", "save"),
    }
    assert all(tuple(call[:2]) not in mutations for call in cli.docker_log())


def test_failed_package_does_not_replace_current_environment(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='last-good'\n", encoding="utf-8")
    ports_snapshot = compose_project / ".docker-manage/ports.json"
    ports_snapshot.write_text(
        '{"schema_version":1,"ports":[]}\n',
        encoding="utf-8",
    )
    previous_ports = ports_snapshot.read_text(encoding="utf-8")
    answers = compose_project / "failed-package-answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "next",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    inspected = cli("inspect", str(compose_project), "--json")
    run_id = json.loads(inspected.stdout)["run_id"]
    planned = cli(
        "plan",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(answers),
        "--json",
    )
    plan_hash = json.loads(planned.stdout)["plan_hash"]

    packaged = cli(
        "package",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(answers),
        "--confirm-plan-hash",
        plan_hash,
        "--json",
        env={"FAKE_DOCKER_EXIT": "23"},
    )

    assert packaged.returncode == 1
    assert snapshot.read_text(encoding="utf-8") == "PORT='last-good'\n"
    assert ports_snapshot.read_text(encoding="utf-8") == previous_ports


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
    assert "计划哈希" in result.stderr
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


def test_interactive_environment_follow_up_preserves_overrides_and_only_asks_missing(
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
            id="env.web.API_KEY",
            kind="env",
            prompt="设置 web.API_KEY",
        ),
    )
    replies = iter(("1: 9000", "", "2: secret", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))

    result = _resolve_answers(questions, {}, non_interactive=False)

    assert result.values == {
        "env.web.PORT": "9000",
        "env.web.API_KEY": "secret",
    }
    captured = capsys.readouterr()
    assert captured.out.count("请设置环境变量") == 1
    assert "以下环境变量必须填写：2. env.web.API_KEY" in captured.err
    assert "请只补充上述缺失序号" in captured.out


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

    unknown_command = cli("unknown")
    assert unknown_command.returncode == 2
    assert "参数错误" in unknown_command.stderr
    assert "可选值" in unknown_command.stderr
    assert "invalid choice" not in unknown_command.stderr

    missing_value = cli("inspect", str(compose_project), "--run-id")
    assert missing_value.returncode == 2
    assert "参数 --run-id 需要一个值" in missing_value.stderr
    assert "expected one argument" not in missing_value.stderr


def test_unknown_argparse_error_does_not_expose_english_fallback() -> None:
    assert _translate_argparse_error("unexpected parser failure") == "参数内容不符合要求"


def test_bind_decision_is_required_and_changes_plan_hash(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "bind-plan"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "local.db").write_text("local", encoding="utf-8")
    (project / "compose.yaml").write_text(
        """services:
  app:
    image: busybox:1.36
    volumes:
      - ./data:/data
""",
        encoding="utf-8",
    )

    hashes: dict[str, str] = {}
    plans: dict[str, dict[str, object]] = {}
    question_id = _file_question_id(str(data.resolve()))
    for decision in ("copy", "keep_server_path"):
        inspected = cli("inspect", str(project), "--json")
        assert inspected.returncode == 0, inspected.stderr
        inspection = json.loads(inspected.stdout)
        question = next(
            item
            for item in inspection["questions"]
            if item["id"] == question_id
        )
        assert question["default"] == "keep_server_path"

        missing_answers = project / f"missing-{decision}.json"
        missing_answers.write_text(
            json.dumps(
                {"values": {"image.app.decision": "registry.intra/app:1"}}
            ),
            encoding="utf-8",
        )
        missing_answers.chmod(0o600)
        missing = cli(
            "plan",
            str(project),
            "--run-id",
            inspection["run_id"],
            "--answers",
            str(missing_answers),
            "--non-interactive",
            "--json",
        )
        assert missing.returncode == 10
        assert question_id in missing.stderr

        answers = project / f"answers-{decision}.json"
        answers.write_text(
            json.dumps(
                {
                    "values": {
                        "image.app.decision": "registry.intra/app:1",
                        question_id: decision,
                    }
                }
            ),
            encoding="utf-8",
        )
        answers.chmod(0o600)
        planned = cli(
            "plan",
            str(project),
            "--run-id",
            inspection["run_id"],
            "--answers",
            str(answers),
            "--non-interactive",
            "--json",
        )
        assert planned.returncode == 0, planned.stderr
        body = json.loads(planned.stdout)
        hashes[decision] = body["plan_hash"]
        plans[decision] = body["plan"]["files"][0]

    assert hashes["copy"] != hashes["keep_server_path"]
    assert plans["copy"]["action"] == "copy"
    assert plans["copy"]["payload_path"] == "files/data"
    assert plans["keep_server_path"]["action"] == "keep_server_path"
    assert plans["keep_server_path"]["payload_path"] is None


def test_plan_resolves_interpolated_bind_from_final_environment(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "interpolated-bind"
    project.mkdir()
    (project / "compose.yaml").write_text(
        """services:
  app:
    image: busybox:1.36
    environment:
      DATA_DIR: ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
    volumes:
      - type: bind
        source: ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
        target: ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
""",
        encoding="utf-8",
    )
    inspected = cli("inspect", str(project), "--json")
    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    file_question = next(
        item for item in body["questions"] if item["kind"] == "file"
    )
    answers = project / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.app.DATA_DIR": "/data/docker-manage-server",
                    "env.app.DOCKER_MANAGE_DATA_DIR": "/data/docker-manage-server",
                    "image.app.decision": "registry.intra/busybox:1.36",
                    file_question["id"]: "keep_server_path",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    resolved = {
        "services": {
            "app": {
                "image": "busybox:1.36",
                "environment": {"DATA_DIR": "/data/docker-manage-server"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/data/docker-manage-server",
                        "target": "/data/docker-manage-server",
                    }
                ],
            }
        }
    }

    planned = cli(
        "plan",
        str(project),
        "--run-id",
        body["run_id"],
        "--answers",
        str(answers),
        "--non-interactive",
        "--json",
        env={"FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG": json.dumps(resolved)},
    )

    assert planned.returncode == 0, planned.stderr
    file_plan = json.loads(planned.stdout)["plan"]["files"][0]
    assert file_plan["resolved_path"] == "/data/docker-manage-server"
    assert file_plan["action"] == "keep_server_path"
    assert file_plan["payload_path"] is None
