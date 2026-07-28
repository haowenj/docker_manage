# 环境变量稀疏覆盖与中文输出实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让大模型和交互式 CLI 只接收需要修改的环境变量序号，同时把全部自有可见文本统一为中文。

**Architecture:** 保持 `Question`、答案 JSON 和计划协议不变，在 `questions.py` 增加环境变量批量展示和稀疏解析接口，由 `cli.py` 与 `SKILL.md` 共同采用。CLI 之外的模块只翻译自有错误摘要和修复建议，第三方原始输出继续原样保留。

**Tech Stack:** Python 3.11、Pydantic 2、argparse、pytest、ruff、Codex Agent Skills

## Global Constraints

- 未提交且具有唯一默认值的环境变量自动沿用默认值。
- 未提交且没有默认值或默认值冲突的环境变量只追问缺失项。
- 使用 `<EMPTY>` 表示显式空字符串，使用 `无修改` 表示没有覆盖项。
- 保持 JSON 字段名、问题 ID、阶段值、枚举值、命令名和参数名不变。
- Docker、Git、操作系统和其他第三方工具的原始输出不得翻译或改写。
- 不改变计划哈希算法、补充文件 schema、归档格式或现有非交互答案文件协议。

---

### Task 1: 环境变量稀疏覆盖核心

**Files:**
- Modify: `tests/unit/test_questions.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/questions.py`

**Interfaces:**
- Consumes: `Question`、现有 `parse_answer(question, raw, chat_mode=...)`。
- Produces: `environment_questions(questions) -> tuple[Question, ...]`、`format_environment_questions(questions) -> tuple[str, ...]`、`parse_environment_overrides(questions, lines) -> dict[str, str]`。

- [ ] **Step 1: 编写失败测试，固定序号、默认值和稀疏覆盖行为**

```python
def test_environment_overrides_fill_omitted_defaults() -> None:
    questions = (
        Question(id="env.web.PORT", kind="env", prompt="设置 web.PORT", default="8000"),
        Question(id="env.web.API_KEY", kind="env", prompt="设置 web.API_KEY"),
    )

    assert format_environment_questions(questions) == (
        "1. 设置 web.PORT，默认值：8000",
        "2. 设置 web.API_KEY，必填，无默认值",
    )
    assert parse_environment_overrides(questions, ("2: secret",)) == {
        "env.web.PORT": "8000",
        "env.web.API_KEY": "secret",
    }
```

同时增加这些独立用例：`无修改`、空输入、`<EMPTY>`、缺失必填项、重复序号、越界序号、非数字序号、缺少冒号。错误断言必须检查中文关键字和出错序号。

- [ ] **Step 2: 运行测试并确认因接口不存在而失败**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_questions.py -q`

Expected: FAIL，导入 `format_environment_questions` 或 `parse_environment_overrides` 失败。

- [ ] **Step 3: 实现最小稀疏解析接口并翻译问题文案**

在 `questions.py` 中实现以下结构：

```python
NO_ENV_OVERRIDES = "无修改"


def environment_questions(questions: Sequence[Question]) -> tuple[Question, ...]:
    return tuple(question for question in questions if question.kind == "env")


def format_environment_questions(questions: Sequence[Question]) -> tuple[str, ...]:
    lines: list[str] = []
    for index, question in enumerate(environment_questions(questions), start=1):
        suffix = (
            f"，默认值：{question.default}"
            if question.default is not None
            else "，必填，无默认值"
        )
        lines.append(f"{index}. {question.prompt}{suffix}")
    return tuple(lines)


def parse_environment_overrides(
    questions: Sequence[Question],
    lines: Sequence[str],
) -> dict[str, str]:
    env = environment_questions(questions)
    overrides: dict[int, str] = {}
    meaningful = tuple(line.strip() for line in lines if line.strip())
    if meaningful != (NO_ENV_OVERRIDES,):
        for line in meaningful:
            sequence_text, separator, value = line.partition(":")
            if not separator or not sequence_text.strip().isdigit():
                raise PlanValidationError(f"环境变量输入格式错误：{line}；请使用 序号: 值")
            sequence = int(sequence_text.strip())
            if sequence < 1 or sequence > len(env):
                raise PlanValidationError(f"环境变量序号超出范围：{sequence}")
            if sequence in overrides:
                raise PlanValidationError(f"环境变量序号重复：{sequence}")
            overrides[sequence] = value.strip()

    answers: dict[str, str] = {}
    missing: list[str] = []
    for sequence, question in enumerate(env, start=1):
        if sequence in overrides:
            answers[question.id] = parse_answer(
                question,
                overrides[sequence],
                chat_mode=True,
            )
        elif question.default is not None:
            answers[question.id] = question.default
        else:
            missing.append(f"{sequence}. {question.id}")
    if missing:
        raise AnswerRequired("以下环境变量必须填写：" + "；".join(missing))
    return answers
```

将 `build_questions` 和 `parse_answer` 中的环境变量、构建参数、端口、镜像、外部文件及校验文案翻译为中文；机器选项值保持原样，并在提示中说明中文含义。

- [ ] **Step 4: 运行问题测试并确认通过**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_questions.py -q`

Expected: PASS。

- [ ] **Step 5: 提交核心解析改动**

```bash
git add tests/unit/test_questions.py skills/package-docker-app/scripts/docker_package_app/questions.py
git commit -m "feat: support sparse environment overrides"
```

---

### Task 2: CLI 批量环境变量交互与中文帮助

**Files:**
- Modify: `tests/integration/test_cli.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py`

**Interfaces:**
- Consumes: Task 1 的 `environment_questions`、`format_environment_questions`、`parse_environment_overrides`。
- Produces: `_read_environment_overrides(questions) -> dict[str, str]`，并保持 `_resolve_answers(...) -> AnswerBook` 签名不变。

- [ ] **Step 1: 编写失败的 CLI 交互与帮助测试**

增加真实子进程测试，输入只覆盖一个环境变量并以空行结束，断言：

```python
assert "请设置环境变量" in result.stdout
assert "1. " in result.stdout
assert "默认值" in result.stdout
assert "设置 web.PORT" not in result.stdout.split("请设置环境变量", 1)[1].splitlines()[-1]
```

让测试读取 dry-run 计划并断言未填写项使用默认值、覆盖项使用新值。再增加 `--help`、缺少子命令、未知参数和中断提示测试，断言自有文本包含 `用法`、`选项`、`参数错误` 或 `已取消`，不包含 argparse 的 `usage:`、`options:`、`: error:`、`cancelled`。

- [ ] **Step 2: 运行 CLI 测试并确认旧的逐项英文交互导致失败**

Run: `uv run --project skills/package-docker-app pytest tests/integration/test_cli.py -q`

Expected: FAIL，输出仍包含逐项英文问题或英文 argparse 标签。

- [ ] **Step 3: 实现环境变量批量交互**

在 `_resolve_answers` 开始处先处理环境变量问题：

```python
env = environment_questions(questions)
provided_env = {
    question.id: parse_answer(question, provided[question.id], chat_mode=False)
    for question in env
    if question.id in provided
}
pending_env = tuple(question for question in env if question.id not in provided_env)
if pending_env:
    if non_interactive:
        raise AnswerRequired(f"缺少答案：{pending_env[0].id}")
    answers.update(provided_env)
    answers.update(_read_environment_overrides(pending_env))
else:
    answers.update(provided_env)
```

`_read_environment_overrides` 打印编号列表，逐行读取到空行，将所有行交给 `parse_environment_overrides`。发生格式或必填错误时打印中文消息并重新展示列表。后续逐项循环跳过 `kind == "env"`。

- [ ] **Step 4: 本地化 argparse 和 CLI 标签**

增加 `ChineseArgumentParser`，将帮助标题和用法前缀改为 `位置参数`、`选项`、`用法：`，并在 `error()` 中把常见解析错误转换为中文。为每个子命令与参数提供中文 `help`。将 `_write_error` 标签改为 `阶段：`、`建议：`，将 Ctrl-C 输出改为 `已取消`。解析器保持 argparse 的退出码 `2`：

```python
class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: 参数错误：{_translate_argparse_error(message)}\n")
```

`_translate_argparse_error` 明确覆盖当前参数表可产生的四类消息：缺少必填参数、未知参数、无效子命令、选项缺少值。测试中的英文反向断言保证这些分支没有遗漏。

- [ ] **Step 5: 运行 CLI 和问题测试并确认通过**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_questions.py tests/integration/test_cli.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 CLI 改动**

```bash
git add tests/integration/test_cli.py skills/package-docker-app/scripts/docker_package_app/cli.py
git commit -m "feat: batch environment input in Chinese CLI"
```

---

### Task 3: 全部自有错误文本和技能提示中文化

**Files:**
- Modify: `tests/unit/test_command.py`
- Modify: `tests/unit/test_compose.py`
- Modify: `tests/unit/test_discovery.py`
- Modify: `tests/unit/test_docker.py`
- Modify: `tests/unit/test_files.py`
- Modify: `tests/unit/test_planning.py`
- Modify: `tests/unit/test_render.py`
- Modify: `tests/unit/test_supplement.py`
- Modify: `tests/unit/test_artifact.py`
- Modify: `tests/unit/test_workspace.py`
- Modify: `tests/unit/test_skill_contract.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/command.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/compose.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/discovery.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/docker.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/files.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/planning.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/render.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/supplement.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/artifact.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/workspace.py`
- Modify: `skills/package-docker-app/SKILL.md`

**Interfaces:**
- Consumes: 现有异常类型、退出码和机器协议。
- Produces: 不改变任何 Python 公共签名；只改变用户可见文本和技能契约。

- [ ] **Step 1: 编写失败测试，固定中文错误摘要且保留第三方原文**

为已有异常测试增加中文关键字断言。例如：

```python
assert "无法执行命令" in caught.value.message
assert "请安装所需命令" in caught.value.hint
assert caught.value.details == "daemon unavailable"
```

在 `test_skill_contract.py` 中断言技能正文包含 `所有面向用户的内容必须使用中文`、`序号: 值`、`无修改`、`<EMPTY>`，并且明确第三方原始输出和机器协议不翻译。

- [ ] **Step 2: 运行相关单元测试并确认英文文本导致失败**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_command.py tests/unit/test_compose.py tests/unit/test_discovery.py tests/unit/test_docker.py tests/unit/test_files.py tests/unit/test_planning.py tests/unit/test_render.py tests/unit/test_supplement.py tests/unit/test_artifact.py tests/unit/test_workspace.py tests/unit/test_skill_contract.py -q`

Expected: FAIL，断言收到英文自有错误消息或英文技能正文。

- [ ] **Step 3: 翻译模块中的自有错误消息与建议**

逐个翻译 `PackageError`、`UsageError`、`AnswerRequired`、`PlanValidationError`、`SupplementValidationError`、`ArtifactVerificationError` 和工作目录防护 `ValueError` 的消息，包括 `cli.py` 中规划状态、打包状态、答案文件、运行 ID、状态迁移和项目归属错误。以下技术内容保持原样嵌入中文句子：命令、路径、镜像名、服务名、平台、哈希、阶段枚举和第三方 `details`。直接替换字符串，不增加翻译字典或运行时国际化依赖：

```python
raise UsageError(f"运行 {state.run_id} 尚未准备好进行规划")
raise AnswerRequired(f"缺少答案：{question.id}")
raise PackageError(
    f"命令执行失败（退出码 {result.returncode}）：{shlex.join(command)}",
    hint="请检查命令错误，修正本地环境后重试。",
    details=result.stderr.strip() or result.stdout.strip(),
)
```

- [ ] **Step 4: 将技能正文完整改为中文并加入稀疏覆盖流程**

保留 YAML 的 `name: package-docker-app`，将 `description` 和正文改为中文。工作流第 2 步必须明确：

```text
把所有 kind=env 的问题按返回顺序一次性编号展示。用户只提交需要修改的“序号: 值”；未提交且有唯一默认值的项目自动使用默认值。无默认值或默认值冲突的项目必须填写，缺失时只追问这些序号。全部使用默认值时接受“无修改”，显式空字符串使用 <EMPTY>。
```

增加全局语言约束：所有解释、提问、选项说明、计划和结果使用中文；模型生成的 `ambiguities.prompt` 使用中文；机器字段和值、命令、路径及第三方原始输出保持原文。

- [ ] **Step 5: 运行模块和技能契约测试并确认通过**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_command.py tests/unit/test_compose.py tests/unit/test_discovery.py tests/unit/test_docker.py tests/unit/test_files.py tests/unit/test_planning.py tests/unit/test_render.py tests/unit/test_supplement.py tests/unit/test_artifact.py tests/unit/test_workspace.py tests/unit/test_skill_contract.py tests/integration/test_cli.py -q`

Expected: PASS。

- [ ] **Step 6: 提交中文化改动**

```bash
git add skills/package-docker-app tests/unit
git commit -m "feat: localize Docker packaging workflow"
```

---

### Task 4: 端到端回归与技能验证

**Files:**
- Verify: `tests/e2e/test_model_supplement_flow.py`
- Verify: `tests/e2e/test_package_flow.py`
- Verify: `tests/integration/test_docker_engine.py`

**Interfaces:**
- Consumes: 前三项任务的交互、中文文案和兼容协议。
- Produces: 端到端兼容性证据，不新增生产接口。

- [ ] **Step 1: 运行端到端兼容测试**

Run: `uv run --project skills/package-docker-app pytest tests/e2e tests/integration/test_docker_engine.py -q`

Expected: PASS；现有测试已经固定答案 JSON 使用 `env.web.PORT` 等原问题 ID，并验证最终计划环境变量值。

- [ ] **Step 2: 只修正兼容性测试揭示的遗漏**

若失败来自遗留英文自有文本，翻译对应文本；若失败来自机器字段、枚举、哈希或归档结构改变，恢复原协议，不修改测试来接受破坏性变更。

- [ ] **Step 3: 运行格式和全量测试**

```bash
uv run --project skills/package-docker-app ruff check skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app pytest -q
```

Expected: ruff 无错误；pytest 全部通过且无失败。

- [ ] **Step 4: 运行技能快速校验和工作树检查**

```bash
python /Users/wenjuhao/.codex-company/skills/.system/skill-creator/scripts/quick_validate.py skills/package-docker-app
git diff --check
git status --short
```

Expected: 技能校验成功；`git diff --check` 退出码为 `0`；状态只包含本计划产生的预期文件。

- [ ] **Step 5: 提交兼容性修正（仅在 Step 1 暴露遗漏时）**

```bash
git add skills/package-docker-app tests/e2e tests/integration/test_docker_engine.py
git commit -m "fix: preserve Docker packaging compatibility"
```

---

## 最终验收

- [ ] 环境变量列表只要求用户填写覆盖项。
- [ ] 缺失必填项时只追问缺失序号。
- [ ] 大模型工作流、CLI 帮助、交互提示和自有错误文本均为中文。
- [ ] 机器协议、退出码、哈希和归档结构未改变。
- [ ] 全量 pytest、ruff 和技能快速校验均通过。
