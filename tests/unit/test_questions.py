from pathlib import Path

import pytest
from docker_package_app.errors import AnswerRequired, PlanValidationError
from docker_package_app.models import (
    CurrentPortSelection,
    DefaultValue,
    EnvCandidate,
    FileCandidate,
    Inspection,
    PortCandidate,
    Question,
    SourceRef,
    Stage,
)
from docker_package_app.questions import (
    _file_question_id,
    build_questions,
    format_environment_questions,
    parse_answer,
    parse_environment_overrides,
)


def _file(
    *,
    service: str,
    source: str,
    resolved: str,
    kind: str = "bind",
    inside: bool = True,
    size: int = 12,
) -> FileCandidate:
    return FileCandidate(
        service=service,
        compose_value=source,
        resolved_path=resolved,
        kind=kind,
        inside_project=inside,
        project_path=Path(resolved).name if inside else None,
        estimated_size=size,
    )


def test_project_bind_question_offers_copy_keep_and_abort() -> None:
    candidate = _file(
        service="web",
        source="./data",
        resolved="/project/data",
        size=2048,
    )
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=(candidate,),
    )

    question = build_questions(inspection)[0]

    assert question.id == _file_question_id("/project/data")
    assert question.kind == "file"
    assert question.default == "keep_server_path"
    assert question.choices == ("copy", "keep_server_path", "abort")
    assert "web" in question.prompt
    assert "./data" in question.prompt
    assert "/project/data" in question.prompt
    assert "项目目录内" in question.prompt
    assert "2048" in question.prompt


def test_shared_bind_source_generates_one_question() -> None:
    files = (
        _file(
            service="api",
            source="./data",
            resolved="/project/data",
        ),
        _file(
            service="worker",
            source="./data",
            resolved="/project/data",
        ),
    )
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=files,
    )

    questions = build_questions(inspection)

    assert len(questions) == 1
    assert "api" in questions[0].prompt
    assert "worker" in questions[0].prompt


def test_external_dependency_keeps_existing_choices_without_default() -> None:
    candidate = _file(
        service="web",
        source="/srv/app.ini",
        resolved="/srv/app.ini",
        kind="config",
        inside=False,
    )
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=(candidate,),
    )

    question = build_questions(inspection)[0]

    assert question.default is None
    assert question.choices == ("keep_server_path", "abort")
    assert "项目目录外" in question.prompt


def test_project_config_and_secret_do_not_generate_file_questions() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=(
            _file(
                service="web",
                source="./app.ini",
                resolved="/project/app.ini",
                kind="config",
            ),
            _file(
                service="web",
                source="./secret.txt",
                resolved="/project/secret.txt",
                kind="secret",
            ),
        ),
    )

    assert build_questions(inspection) == ()


def test_default_and_explicit_empty_rules() -> None:
    with_default = Question(
        id="env.web.PORT",
        kind="env",
        prompt="PORT",
        default="8000",
    )
    assert parse_answer(with_default, "默认", chat_mode=True) == "8000"
    assert parse_answer(with_default, "", chat_mode=False) == "8000"

    required = Question(id="env.web.API_KEY", kind="env", prompt="API_KEY")
    assert parse_answer(required, "<EMPTY>", chat_mode=True) == ""
    with pytest.raises(AnswerRequired):
        parse_answer(required, "", chat_mode=False)


def test_questions_show_conflicting_secret_defaults() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        env=(
            EnvCandidate(
                service="web",
                name="API_KEY",
                defaults=(
                    DefaultValue(value="secret-one", source=SourceRef(path=".env", line=1)),
                    DefaultValue(value="secret-two", source=SourceRef(path="app.py", line=4)),
                ),
            ),
        ),
    )

    question = build_questions(inspection)[0]

    assert question.id == "env.web.API_KEY"
    assert question.default is None
    assert "secret-one" in question.prompt
    assert "secret-two" in question.prompt
    assert format_environment_questions((question,)) == (
        (
            "1. 设置 web.API_KEY。声明默认值来源：.env:1=secret-one, "
            "app.py:4=secret-two，必填，默认值冲突"
        ),
    )


def test_current_value_becomes_default_without_hiding_declared_defaults() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        env=(
            EnvCandidate(
                service="web",
                name="PORT",
                current=DefaultValue(
                    value="8322",
                    source=SourceRef(path=".docker-manage/.env"),
                ),
                defaults=(
                    DefaultValue(
                        value="8000",
                        source=SourceRef(path="app.py", line=2),
                    ),
                ),
            ),
        ),
    )

    question = build_questions(inspection)[0]

    assert question.default == "8322"
    assert "当前配置值：8322" in question.prompt
    assert "来源：.docker-manage/.env" in question.prompt
    assert "声明默认值来源：app.py:2=8000" in question.prompt
    assert format_environment_questions((question,)) == (
        (
            "1. 设置 web.PORT。当前配置值：8322，来源：.docker-manage/.env。"
            "声明默认值来源：app.py:2=8000，默认值：8322"
        ),
    )
    assert parse_environment_overrides((question,), ("无修改",)) == {
        "env.web.PORT": "8322"
    }


def test_empty_current_value_remains_a_valid_default() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        env=(
            EnvCandidate(
                service="web",
                name="OPTIONAL_VALUE",
                current=DefaultValue(
                    value="",
                    source=SourceRef(path=".docker-manage/.env"),
                ),
                defaults=(
                    DefaultValue(
                        value="fallback",
                        source=SourceRef(path="app.py", line=3),
                    ),
                ),
            ),
        ),
    )

    question = build_questions(inspection)[0]

    assert question.default == ""
    assert parse_environment_overrides((question,), ("无修改",)) == {
        "env.web.OPTIONAL_VALUE": ""
    }


def test_current_port_selection_precedes_declared_mapping() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        ports=(
            PortCandidate(
                service="web",
                container_port=8000,
                host_port=8080,
                current=CurrentPortSelection(exposed=True, host_port=8322),
            ),
            PortCandidate(
                service="worker",
                container_port=9000,
                host_port=9090,
                current=CurrentPortSelection(exposed=False, host_port=None),
            ),
        ),
    )

    questions = {item.id: item for item in build_questions(inspection)}

    web_expose = questions["port.web.8000/tcp.expose"]
    web_host = questions["port.web.8000/tcp.host"]
    worker_expose = questions["port.worker.9000/tcp.expose"]
    assert web_expose.default == "yes"
    assert web_host.default == "8322"
    assert worker_expose.default == "no"
    assert "当前配置：已暴露，主机端口 8322" in web_expose.prompt
    assert "声明映射：主机端口 8080" in web_expose.prompt
    assert "当前配置：不暴露" in worker_expose.prompt


def test_environment_overrides_fill_omitted_defaults() -> None:
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

    assert format_environment_questions(questions) == (
        "1. 设置 web.PORT，默认值：8000",
        "2. 设置 web.API_KEY，必填，无默认值",
    )
    assert parse_environment_overrides(questions, ("2 : secret",)) == {
        "env.web.PORT": "8000",
        "env.web.API_KEY": "secret",
    }


def test_environment_overrides_accept_no_changes_and_explicit_empty() -> None:
    questions = (
        Question(
            id="env.web.PORT",
            kind="env",
            prompt="设置 web.PORT",
            default="8000",
        ),
    )

    assert parse_environment_overrides(questions, ("无修改",)) == {
        "env.web.PORT": "8000"
    }
    assert parse_environment_overrides(questions, ()) == {"env.web.PORT": "8000"}
    assert parse_environment_overrides(questions, ("1: <EMPTY>",)) == {
        "env.web.PORT": ""
    }


def test_environment_overrides_report_only_missing_required_items() -> None:
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

    with pytest.raises(AnswerRequired, match="2.*env.web.API_KEY"):
        parse_environment_overrides(questions, ("无修改",))


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (("1: first", "1: second"), "序号重复：1"),
        (("3: value",), "序号超出范围：3"),
        (("x: value",), "格式错误"),
        (("1 value",), "格式错误"),
    ],
)
def test_environment_overrides_reject_invalid_sequences(
    lines: tuple[str, ...],
    message: str,
) -> None:
    questions = (
        Question(
            id="env.web.PORT",
            kind="env",
            prompt="设置 web.PORT",
            default="8000",
        ),
    )

    with pytest.raises(PlanValidationError, match=message):
        parse_environment_overrides(questions, lines)
