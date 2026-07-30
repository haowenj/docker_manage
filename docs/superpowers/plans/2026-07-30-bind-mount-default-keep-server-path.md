# Bind Mount Default Keep Server Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make project-internal bind mounts default to `keep_server_path` while preserving all choices, explicit-answer requirements, and deployment behavior.

**Architecture:** Keep the change at the question-construction boundary by replacing the project-internal bind default constant only. Lock the behavior with unit and CLI integration assertions, then update the package skill contract so model-facing guidance matches the CLI.

**Tech Stack:** Python 3.11+, Pydantic 2, pytest 8, uv, Ruff

## Global Constraints

- Project-internal bind mount choices remain exactly `copy`, `keep_server_path`, and `abort`.
- The project-internal bind mount default becomes exactly `keep_server_path`.
- Project-external local dependencies remain limited to `keep_server_path` and `abort`, with no default.
- Answer files must still explicitly contain the final bind mount decision.
- Planning, materialization, rendering, and deployment behavior for `copy`, `keep_server_path`, and `abort` must not change.
- Do not add deployment-history detection, global configuration, or model fields.

## File Structure

- `skills/package-docker-app/scripts/docker_package_app/questions.py` owns question choices and defaults.
- `tests/unit/test_questions.py` verifies question construction in isolation.
- `tests/integration/test_cli.py` verifies the serialized `inspect` response and explicit-answer requirement.
- `skills/package-docker-app/SKILL.md` defines the model-facing packaging contract.
- `tests/unit/test_skill_contract.py` prevents the contract from drifting away from CLI behavior.

---

### Task 1: Change the CLI Question Default

**Files:**
- Modify: `tests/unit/test_questions.py:45-69`
- Modify: `tests/integration/test_cli.py:429-515`
- Modify: `skills/package-docker-app/scripts/docker_package_app/questions.py:130-150`

**Interfaces:**
- Consumes: `build_questions(inspection: Inspection) -> tuple[Question, ...]`
- Produces: project-internal bind `Question` objects whose `default` is `keep_server_path`

- [ ] **Step 1: Update the unit and integration assertions first**

In `tests/unit/test_questions.py`, change only the expected default:

```python
    assert question.id == _file_question_id("/project/data")
    assert question.kind == "file"
    assert question.default == "keep_server_path"
    assert question.choices == ("copy", "keep_server_path", "abort")
```

In `tests/integration/test_cli.py`, change only the serialized default assertion:

```python
        question = next(
            item
            for item in inspection["questions"]
            if item["id"] == question_id
        )
        assert question["default"] == "keep_server_path"
```

Keep the missing-answer assertions and both explicit `copy` and
`keep_server_path` plan branches unchanged. They prove that a default does not
silently replace the required answer.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py::test_project_bind_question_offers_copy_keep_and_abort \
  tests/integration/test_cli.py::test_bind_decision_is_required_and_changes_plan_hash \
  -v
```

Expected: both tests fail because the actual default is still `copy`; the
failure output compares `copy` with `keep_server_path`.

- [ ] **Step 3: Make the minimal production change**

In `skills/package-docker-app/scripts/docker_package_app/questions.py`, change
the project-internal branch to:

```python
        if inside_project:
            choices = ("copy", "keep_server_path", "abort")
            default = "keep_server_path"
            meaning = (
                "copy（复制本机内容）、keep_server_path（保留服务器现有路径）"
                "或 abort（中止）"
            )
```

Do not change the external-path branch or any planning logic.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py::test_project_bind_question_offers_copy_keep_and_abort \
  tests/unit/test_questions.py::test_external_dependency_keeps_existing_choices_without_default \
  tests/integration/test_cli.py::test_bind_decision_is_required_and_changes_plan_hash \
  -v
```

Expected: all three tests pass. The integration test must still show that
omitting the bind answer returns exit code `10`, while explicit `copy` and
`keep_server_path` produce different valid plans.

- [ ] **Step 5: Commit the behavior change**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/questions.py \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py
git commit -m "fix: preserve server bind paths by default"
```

---

### Task 2: Synchronize the Skill Contract and Verify the Suite

**Files:**
- Modify: `tests/unit/test_skill_contract.py:83-100`
- Modify: `skills/package-docker-app/SKILL.md:29`

**Interfaces:**
- Consumes: the CLI behavior established in Task 1
- Produces: model-facing guidance that names `keep_server_path` as the project-internal bind default

- [ ] **Step 1: Add a failing contract assertion**

In `tests/unit/test_skill_contract.py`, add the exact new default statement to
the existing `required` tuple:

```python
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
```

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py::test_skill_requires_explicit_bind_copy_or_server_preservation \
  -v
```

Expected: FAIL because `SKILL.md` still says the project-internal bind default
is `copy`.

- [ ] **Step 3: Update the model-facing contract**

In `skills/package-docker-app/SKILL.md`, replace the project-internal default
sentence while preserving the choices and explicit-answer requirement:

```markdown
- 每个 bind mount 都必须在计划前明确决定：项目内路径可选 `copy`（复制本机内容）、`keep_server_path`（保留服务器现有路径）或 `abort`（中止）；项目外路径只允许 `keep_server_path` 或 `abort`。项目内 bind 默认值为 `keep_server_path`，但答案文件仍必须包含最终决定。
```

- [ ] **Step 4: Run the contract test and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py::test_skill_requires_explicit_bind_copy_or_server_preservation \
  -v
```

Expected: PASS.

- [ ] **Step 5: Run formatting and the full regression suite**

Run:

```bash
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app pytest -q
git diff --check
```

Expected: Ruff exits `0`, the complete pytest suite passes, and
`git diff --check` prints no output.

- [ ] **Step 6: Commit the synchronized contract**

```bash
git add \
  skills/package-docker-app/SKILL.md \
  tests/unit/test_skill_contract.py
git commit -m "docs: document server bind path default"
```
