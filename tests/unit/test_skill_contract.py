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


def test_skill_defines_repeat_package_fast_path(skill_text: str) -> None:
    required = (
        "重复打包模式",
        "同时存在且都是普通文件",
        "`.docker-manage/.env`",
        "`.docker-manage/ports.json`",
        "所有问题按 CLI 返回顺序统一编号",
        "序号: 值",
        "无修改",
        "必填且没有默认值",
        "只追问缺失或无效项",
    )
    for text in required:
        assert text in skill_text


def test_skill_confirmation_depends_on_package_mode(skill_text: str) -> None:
    required = (
        "首次打包模式",
        "等待用户明确确认",
        "重复打包模式",
        "不等待第二次确认",
        "立即运行 `package`",
        "CLI 返回的精确 `plan_hash`",
    )
    for text in required:
        assert text in skill_text


def test_skill_preserves_machine_protocol_and_raw_tool_output(skill_text: str) -> None:
    assert "机器协议" in skill_text
    assert "第三方工具的原始输出" in skill_text
    assert '"values":{"question.id":"answer"}' in skill_text


def test_skill_has_no_template_markers(skill_text: str) -> None:
    for marker in ("[TO" + "DO", "TO" + "DO:", "T" + "BD", "PLACE" + "HOLDER"):
        assert marker not in skill_text


def test_skill_uses_project_current_environment_snapshot(skill_text: str) -> None:
    required = (
        ".docker-manage/.env",
        "当前配置值",
        "声明默认值",
        "优先采用当前配置值",
        "只有完整成功打包后",
        "不得直接编辑 `.docker-manage/.env`",
        "不得从历史 `state.json`",
    )
    for text in required:
        assert text in skill_text


def test_skill_uses_current_ports_and_writable_bind_copies(
    skill_text: str,
) -> None:
    required = (
        ".docker-manage/ports.json",
        "当前端口配置",
        "声明端口映射",
        "优先采用当前端口配置",
        "不得直接编辑 `.docker-manage/ports.json`",
        "目录权限为 `0777`",
        "普通文件权限为 `0666`",
        "不得修改原项目文件权限",
    )
    for text in required:
        assert text in skill_text


def test_skill_requires_explicit_bind_copy_or_server_preservation(
    skill_text: str,
) -> None:
    required = (
        "每个 bind mount",
        "`copy`",
        "`keep_server_path`",
        "`abort`",
        "项目内 bind 默认值为 `keep_server_path`",
        "不进入归档",
        "稳定部署路径",
        "`./files/`",
        "不得包含开发电脑的绝对路径",
        "重复解压",
        "复制文件",
        "保留的服务器路径",
    )
    for text in required:
        assert text in skill_text
