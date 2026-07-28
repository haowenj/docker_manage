import pytest
from docker_package_app.errors import AnswerRequired
from docker_package_app.models import (
    DefaultValue,
    EnvCandidate,
    Inspection,
    Question,
    SourceRef,
    Stage,
)
from docker_package_app.questions import build_questions, parse_answer


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

