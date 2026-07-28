import pytest
from docker_package_app.errors import AnswerRequired, PlanValidationError
from docker_package_app.models import (
    DefaultValue,
    EnvCandidate,
    Inspection,
    Question,
    SourceRef,
    Stage,
)
from docker_package_app.questions import (
    build_questions,
    format_environment_questions,
    parse_answer,
    parse_environment_overrides,
)


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
