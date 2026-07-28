from pathlib import Path

import pytest


@pytest.fixture
def skill_text() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "skills/package-docker-app/SKILL.md"
    ).read_text(encoding="utf-8")


def test_skill_declares_fixed_cli_and_model_boundary(skill_text: str) -> None:
    required = (
        "uv run --project",
        "EXIT_MODEL_REQUIRED=20",
        ".docker-manage/generated",
        "Do not modify existing project files",
        "Do not run Docker build, pull, save, or archive commands directly",
        "references/model-supplement.schema.json",
        "--confirm-plan-hash",
    )
    for text in required:
        assert text in skill_text


def test_skill_requires_every_question_and_explicit_confirmation(
    skill_text: str,
) -> None:
    assert "Ask every returned question in order" in skill_text
    assert "Show full defaults, including passwords, tokens, and keys" in skill_text
    assert "默认" in skill_text
    assert "explicit confirmation" in skill_text


def test_skill_has_no_template_markers(skill_text: str) -> None:
    for marker in ("[TO" + "DO", "TO" + "DO:", "T" + "BD", "PLACE" + "HOLDER"):
        assert marker not in skill_text
