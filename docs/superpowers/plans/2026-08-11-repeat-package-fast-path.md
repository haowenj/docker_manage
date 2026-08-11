# Repeat Package Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `package-docker-app` reuse the last successful environment and port snapshots through one configuration review, then package immediately after planning without weakening first-package confirmation or CLI plan-hash validation.

**Architecture:** Keep the behavior entirely in the model-facing skill. Detect repeat packaging from two existing CLI-owned snapshot files, branch the question and confirmation workflow in `SKILL.md`, and lock the wording with positive contract assertions. The CLI, question schema, answer protocol, plan generation, and Docker operations remain unchanged.

**Tech Stack:** Markdown Agent Skill, Python 3.11+, pytest 8, uv

## Global Constraints

- Repeat mode requires both `<project>/.docker-manage/.env` and `<project>/.docker-manage/ports.json` to exist as regular files.
- A missing snapshot or only one snapshot preserves the first-package workflow.
- Invalid existing snapshots remain CLI errors; the model must not repair or bypass them.
- First packaging keeps full questions, third-party-image pauses, and explicit plan confirmation.
- Repeat packaging lists every question and default once, accepts sparse `序号: 值` changes or `无修改`, and asks again only for missing or invalid values.
- Required questions without defaults must never be guessed.
- Repeat packaging displays the complete plan and `plan_hash`, then immediately calls `package` with the exact CLI hash.
- The CLI, answer JSON format, snapshot formats, Docker behavior, and archive structure must not change.
- Existing user-owned `.docker-manage/` content in this repository must not be staged or modified.

## File Structure

- `skills/package-docker-app/SKILL.md` owns first-versus-repeat interaction behavior and the conditional plan confirmation rule.
- `tests/unit/test_skill_contract.py` verifies the model-facing workflow text and preserves existing safety contracts.
- `docs/superpowers/specs/2026-08-11-repeat-package-fast-path-design.md` is the approved source of requirements and is not modified by implementation.

---

### Task 1: Establish the Current Skill Behavior Baseline

**Files:**
- Read: `skills/package-docker-app/SKILL.md`
- No repository files are modified.

**Interfaces:**
- Consumes: the current `package-docker-app` skill and a synthetic repeat-package inspection containing defaults for environment, ports, a third-party image, and a project bind mount
- Produces: recorded evidence that the current skill still pauses on the third-party image and requires a second plan confirmation

- [ ] **Step 1: Run a no-new-guidance control five times**

Dispatch fresh-context agents with the current skill and this scenario, without describing the intended fix:

```text
Use $package-docker-app at
/Users/wenjuhao/code/python/docker_manage/skills/package-docker-app to explain the
next user-facing interaction and whether you may invoke package immediately.
Assume inspect succeeded; both .docker-manage/.env and
.docker-manage/ports.json exist; every returned question has a default; the
questions include environment, port, third-party-image, and project bind-mount
items. The user originally asked to package the project. Do not execute commands.
```

Run five independent samples. Read each response manually and record whether it:

```text
A. asks environment and non-environment questions through the existing split flow
B. pauses separately for the third-party image
C. waits for explicit confirmation after displaying plan_hash
```

- [ ] **Step 2: Verify RED**

Expected: at least one control response exhibits `B` or `C`; the current skill does not implement the approved one-review repeat flow. If all five already follow the desired flow, stop because the proposed wording has no demonstrated behavior gap.

- [ ] **Step 3: Summarize the baseline failure before editing**

Keep a short working note containing the observed response excerpts and map them to the missing conditional rules:

```text
repeat predicate missing -> first and repeat runs are indistinguishable
single all-defaults review missing -> questions remain split or third-party pauses
conditional confirmation missing -> plan always waits for explicit confirmation
```

Do not commit this temporary evaluation note.

---

### Task 2: Specify the Repeat-Mode Contract Test First

**Files:**
- Modify: `tests/unit/test_skill_contract.py:24-37`

**Interfaces:**
- Consumes: `skill_text: str` fixture containing `skills/package-docker-app/SKILL.md`
- Produces: contract tests that require an observable repeat predicate, one sparse review, missing-value fallback, and conditional plan confirmation

- [ ] **Step 1: Add failing positive contract assertions**

Append these tests to `tests/unit/test_skill_contract.py`:

```python
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
```

Keep the existing test that asserts `明确确认`; first packaging still requires it.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py::test_skill_defines_repeat_package_fast_path \
  tests/unit/test_skill_contract.py::test_skill_confirmation_depends_on_package_mode \
  -v
```

Expected: both tests fail because `SKILL.md` does not yet contain `重复打包模式` or the conditional confirmation contract. Confirm the failures are assertion mismatches, not collection or syntax errors.

- [ ] **Step 3: Commit the RED tests**

```bash
git add tests/unit/test_skill_contract.py
git commit -m "test: require repeat package fast path"
```

---

### Task 3: Implement the Minimal Skill Workflow

**Files:**
- Modify: `skills/package-docker-app/SKILL.md:54-114`

**Interfaces:**
- Consumes: `inspect` question objects with `id`, `kind`, `prompt`, `default`, `choices`, and the existing project snapshots
- Produces: a complete `{"values": {...}}` answer JSON, a valid `plan`, and a mode-dependent transition to `package`

- [ ] **Step 1: Add the observable package-mode predicate**

At the start of `## 工作流`, make the first step resolve `$PROJECT`, check the two paths without editing them, and define the modes using this exact rule:

```markdown
1. 把目标项目解析为绝对路径。仅当
   `<project>/.docker-manage/.env` 和
   `<project>/.docker-manage/ports.json` 同时存在且都是普通文件时，进入
   **重复打包模式**；否则进入 **首次打包模式**。只存在其中一个快照时不得
   复用配置。路径存在但不是普通文件时不进入重复打包模式，后续 `inspect`
   若报告快照无效，按 CLI 错误停止。
```

Keep the existing `inspect` command in the same step and preserve `run_id` and `EXIT_MODEL_REQUIRED=20` handling.

- [ ] **Step 2: Add the repeat-package one-review branch**

Immediately after `inspect`, add a repeat-only step with this positive output recipe:

```markdown
2. 在重复打包模式下，把 `inspect` 返回的所有问题按 CLI 返回顺序统一编号。
   每项显示问题 ID、中文提示、完整当前配置及来源、声明默认值及来源、最终
   默认答案、机器选项和中文含义。第三方镜像继续显示 `【重要】` 警告；bind
   mount 继续显示服务、原始 source、解析路径、项目内外位置、估算大小、稳定
   部署路径和归档行为。完整显示密码、Token 和 Key。

   只询问一次：`如需修改，请回复“序号: 值”；全部沿用以上默认答案请回复
   “无修改”。` `无修改` 表示用户明确接受清单中的全部默认答案；一个或多个
   `序号: 值` 只覆盖对应项，其余项采用已展示的默认答案。`<EMPTY>` 表示显式
   空字符串。拒绝重复、越界、非数字序号、缺少冒号和选项外的值，并指出具体
   问题。必填且没有默认值的项目标记为“必填，无默认值”；用户回复 `无修改`
   或提交稀疏修改后，只追问缺失或无效项。选择 `abort` 或明确要求停止时立即
   停止，不得运行 `plan`。
```

After this branch, direct repeat mode to the shared answer-file step. Keep the existing environment, non-environment, and third-party-image steps under an explicit `首次打包模式` branch without reducing their rules.

- [ ] **Step 3: Make plan confirmation conditional**

Replace the unconditional confirmation wording after plan display with:

```markdown
- 首次打包模式：展示完整计划和 `plan_hash` 后等待用户明确确认，不得把用户
  最初的打包请求视为这次确认。
- 重复打包模式：完整展示计划和 `plan_hash` 作为进度与审计输出，不等待第二次
  确认；`plan` 成功且返回哈希后立即运行 `package`。即使用户提交了配置修改，
  完成前述一次配置确认后也不再增加计划确认。
```

Keep the existing `package` command and the rule requiring the CLI-returned exact hash. Add explicit stop rules for nonzero `plan`, missing `plan_hash`, inconsistent `run_id`/answers/platform/profiles, and `abort`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py::test_skill_defines_repeat_package_fast_path \
  tests/unit/test_skill_contract.py::test_skill_confirmation_depends_on_package_mode \
  tests/unit/test_skill_contract.py::test_skill_requires_sparse_environment_overrides_and_confirmation \
  -v
```

Expected: all three tests pass. The legacy confirmation test proves first-package confirmation remains documented.

- [ ] **Step 5: Commit the GREEN skill change**

```bash
git add skills/package-docker-app/SKILL.md
git commit -m "feat: streamline repeat package workflow"
```

---

### Task 4: Forward-Test and Verify Deployment Quality

**Files:**
- Verify: `skills/package-docker-app/SKILL.md`
- Verify: `tests/unit/test_skill_contract.py`
- No new repository files are required.

**Interfaces:**
- Consumes: the updated skill, contract tests, and approved design
- Produces: evidence that fresh agents follow both branches and the complete repository remains green

- [ ] **Step 1: Micro-test the updated wording five times**

Repeat Task 1's fresh-context scenario with the updated skill. Manually verify every response satisfies this shape:

```text
1. recognizes repeat mode only from both regular snapshot files
2. emits one unified defaults review
3. treats no-default questions as missing rather than guessing
4. keeps the third-party warning in the review
5. displays plan_hash and proceeds to package without another user wait
```

If responses diverge, tighten the positive recipe in `SKILL.md`, rerun the focused contract tests, and repeat the five-sample check.

- [ ] **Step 2: Test the first-package counterexample**

Run a fresh-context scenario where only `.docker-manage/.env` exists. Expected: the agent selects first-package mode, follows split environment/non-environment questions, pauses on each third-party image, and waits after displaying the plan.

- [ ] **Step 3: Run skill validation and repository checks**

Run:

```bash
python /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app pytest -q
git diff --check
```

Expected: skill validation reports a valid skill, Ruff exits `0`, all pytest tests pass, and `git diff --check` prints no output.

- [ ] **Step 4: Review scope and working tree**

Run:

```bash
git status --short
git diff HEAD~2 -- \
  skills/package-docker-app/SKILL.md \
  tests/unit/test_skill_contract.py
```

Expected: implementation changes are limited to the skill and contract test. The pre-existing untracked `.docker-manage/` remains untouched and unstaged.

- [ ] **Step 5: Apply any evaluation-only wording refinement**

If Task 4 required a wording refinement, stage only the skill and commit it:

```bash
git add skills/package-docker-app/SKILL.md
git commit -m "docs: tighten repeat package guidance"
```

If no refinement was needed, do not create an empty commit.
