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
        "不得修改现有项目文件",
        "不得直接运行 Docker build、pull、save 或归档命令",
        "references/model-supplement.schema.json",
        "--confirm-plan-hash",
    )
    for text in required:
        assert text in skill_text


def test_skill_requires_sparse_environment_overrides_and_confirmation(
    skill_text: str,
) -> None:
    assert "所有面向用户的内容必须使用中文" in skill_text
    assert "序号: 值" in skill_text
    assert "无修改" in skill_text
    assert "<EMPTY>" in skill_text
    assert "只追问这些缺失序号" in skill_text
    assert "完整显示默认值，包括密码、Token 和 Key" in skill_text
    assert "明确确认" in skill_text
    assert "重复运行 `inspect`，直到退出码为 `0`" in skill_text


def test_skill_preserves_machine_protocol_and_raw_tool_output(skill_text: str) -> None:
    assert "机器协议" in skill_text
    assert "第三方工具的原始输出" in skill_text
    assert '"values":{"question.id":"answer"}' in skill_text


def test_skill_has_no_template_markers(skill_text: str) -> None:
    for marker in ("[TO" + "DO", "TO" + "DO:", "T" + "BD", "PLACE" + "HOLDER"):
        assert marker not in skill_text
