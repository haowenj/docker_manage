# Current Mount Decision Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the bind mount decisions from the latest successful package and use them as defaults on the next inspection.

**Architecture:** Extend the existing current-configuration boundary in `current_config.py` with a strict, private `mounts.json` snapshot. Attach matching decisions to discovered `FileCandidate` objects before question generation, then atomically write environment, port, and mount snapshots from the successful `PackagePlan` with rollback across all three files.

**Tech Stack:** Python 3.11+, Pydantic 2, pytest, Ruff, existing `docker-package-app` CLI and fake-Docker integration harness.

## Global Constraints

- Only a complete successful package may update `.docker-manage/mounts.json`.
- A previous `copy` decision remains the next default `copy`; a previous `keep_server_path` decision remains the next default `keep_server_path`.
- The snapshot stores only `kind=bind`, deduplicated by normalized absolute `resolved_path`.
- Project-external bind mounts may never receive `copy` as a default.
- Existing projects without `mounts.json` retain the current behavior and become migrated after the next successful package.
- Repeat-package mode continues to depend only on `.docker-manage/.env` and `.docker-manage/ports.json`.
- Do not derive current mount decisions from historical `state.json`, answer files, manifests, or archives.
- Preserve the user's untracked `.docker-manage/` directory and do not stage it.

---

### Task 1: Represent and read current mount decisions

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/models.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Test: `tests/unit/test_models.py`
- Test: `tests/unit/test_current_config.py`

**Interfaces:**
- Produces: `FileCandidate.current_action: FileAction | None`.
- Produces: `CURRENT_MOUNTS_RELATIVE = Path(".docker-manage/mounts.json")`.
- Produces: `CURRENT_MOUNTS_SOURCE = ".docker-manage/mounts.json"`.
- Produces: `attach_current_mounts(project_root: Path, candidates: Sequence[FileCandidate]) -> tuple[FileCandidate, ...]`.

- [ ] **Step 1: Write failing model and snapshot-reading tests**

Add a model round-trip test showing old serialized candidates remain compatible and current actions round-trip:

```python
def test_file_candidate_current_action_is_optional_and_round_trips() -> None:
    old = FileCandidate.model_validate(
        {
            "service": "web",
            "compose_value": "./data",
            "resolved_path": "/project/data",
            "kind": "bind",
            "inside_project": True,
            "project_path": "data",
            "estimated_size": 0,
        }
    )
    assert old.current_action is None

    restored = FileCandidate.model_validate_json(
        old.model_copy(update={"current_action": FileAction.COPY}).model_dump_json()
    )
    assert restored.current_action is FileAction.COPY
```

Add `tests/unit/test_current_config.py` cases for:

```python
def test_missing_mount_snapshot_preserves_file_candidates(tmp_path: Path) -> None:
    candidates = (_bind("/project/data", inside=True),)
    assert attach_current_mounts(tmp_path, candidates) == candidates


def test_attach_current_mounts_matches_paths_and_ignores_stale_entries(
    tmp_path: Path,
) -> None:
    _write_mount_snapshot(
        tmp_path,
        [
            {"resolved_path": "/project/data", "action": "copy"},
            {"resolved_path": "/removed", "action": "keep_server_path"},
        ],
    )
    attached = attach_current_mounts(
        tmp_path,
        (_bind("/project/data", inside=True), _bind("/project/new", inside=True)),
    )
    assert attached[0].current_action is FileAction.COPY
    assert attached[1].current_action is None
```

Parameterize invalid JSON, schema version, duplicate paths, and invalid action; each must raise `UsageError` containing `当前挂载快照`. Add a separate case where a matching project-external candidate receives stored `copy` and assert `UsageError` contains `项目目录外` and `copy`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py::test_file_candidate_current_action_is_optional_and_round_trips \
  tests/unit/test_current_config.py -q
```

Expected: FAIL because `FileCandidate.current_action`, `CURRENT_MOUNTS_RELATIVE`, and `attach_current_mounts` do not exist.

- [ ] **Step 3: Implement the minimal strict snapshot reader**

In `models.py`, add:

```python
class FileCandidate(StrictModel):
    # existing fields remain unchanged
    current_action: FileAction | None = None
```

In `current_config.py`, add strict private models:

```python
CURRENT_MOUNTS_RELATIVE = Path(".docker-manage/mounts.json")
CURRENT_MOUNTS_SOURCE = CURRENT_MOUNTS_RELATIVE.as_posix()


class _MountSnapshotEntry(StrictModel):
    resolved_path: str
    action: FileAction


class _MountSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    mounts: tuple[_MountSnapshotEntry, ...] = ()
```

Implement `attach_current_mounts` using `Path(value).resolve()` for both snapshot entries and candidates. Reject duplicate normalized paths. Ignore stale snapshot paths, attach actions only to `kind="bind"`, and reject `FileAction.COPY` when the matching candidate has `inside_project=False`. Wrap read and Pydantic failures in `UsageError("当前挂载快照无效 ...")`; a path that exists but is not a regular file gets `UsageError("当前挂载快照不是普通文件 ...")`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Commit the snapshot model and reader**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/models.py \
  skills/package-docker-app/scripts/docker_package_app/current_config.py \
  tests/unit/test_models.py \
  tests/unit/test_current_config.py
git commit -m "feat: read current mount decisions"
```

---

### Task 2: Use current mount decisions as question defaults

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/questions.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py`
- Test: `tests/unit/test_questions.py`
- Test: `tests/integration/test_cli.py`

**Interfaces:**
- Consumes: `FileCandidate.current_action` and `attach_current_mounts(...)` from Task 1.
- Produces: inspection JSON containing `files[*].current_action` and file questions whose final default prefers the stored action.

- [ ] **Step 1: Write failing question and CLI inspection tests**

Add unit tests proving both stored actions override generic rules:

```python
@pytest.mark.parametrize("action", (FileAction.COPY, FileAction.KEEP_SERVER_PATH))
def test_current_bind_action_becomes_default_and_shows_source(action: FileAction) -> None:
    candidate = _file(
        service="web",
        source="./data",
        resolved="/project/data",
    ).model_copy(update={"current_action": action})
    question = build_questions(
        Inspection(
            run_id="run-1",
            project_root="/project",
            stage=Stage.INSPECTED,
            files=(candidate,),
        )
    )[0]
    assert question.default == action.value
    assert f"当前配置值：{action.value}" in question.prompt
    assert "来源：.docker-manage/mounts.json" in question.prompt
```

Retain the existing project-internal default and project-external no-default assertions for candidates without `current_action`.

Add an integration test that writes `.docker-manage/mounts.json`, invokes `inspect --json`, and asserts both `body["files"][0]["current_action"] == "copy"` and the matching file question default is `copy`. Add a CLI case for an external bind with a stored `copy` and assert exit code `2` plus the Chinese safety error.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py -q
```

Expected: new tests FAIL because `inspect` does not attach mount configuration and question generation ignores `current_action`.

- [ ] **Step 3: Attach decisions during inspect and prefer them in questions**

Import `attach_current_mounts` in `cli.py` and extend the final inspection update:

```python
inspection = inspection.model_copy(
    update={
        "env": attach_current_values(project, inspection.env),
        "ports": attach_current_ports(project, inspection.ports),
        "files": attach_current_mounts(project, inspection.files),
    }
)
```

In `build_questions`, preserve the existing choices and generic defaults, then override the default when every candidate grouped by normalized path has the same non-`None` current action. Append `当前配置值：...，来源：.docker-manage/mounts.json` to the prompt. Treat mixed current values for one grouped path as `UsageError` or `PlanValidationError` rather than selecting one silently.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Commit inspection and question behavior**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/questions.py \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py
git commit -m "feat: reuse mount decisions as defaults"
```

---

### Task 3: Persist mount decisions with three-snapshot rollback

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py`
- Test: `tests/unit/test_current_config.py`

**Interfaces:**
- Produces: `write_current_mounts(project_root: Path, assignments: Sequence[FileAssignment]) -> Path`.
- Changes: `write_current_configuration(project_root, environment, ports, files) -> tuple[Path, Path, Path]`.
- Consumes: final `PackagePlan.files` in `_perform_package`.

- [ ] **Step 1: Write failing mount-writer and transaction tests**

Add a test passing duplicate service-level assignments for the same bind path, plus a config assignment, and assert output is sorted, deduplicated, private, and bind-only:

```python
def test_write_current_mounts_is_bind_only_deduplicated_and_private(
    tmp_path: Path,
) -> None:
    path = write_current_mounts(
        tmp_path,
        (
            _file_assignment("web", "/project/data", "bind", FileAction.COPY),
            _file_assignment("worker", "/project/data", "bind", FileAction.COPY),
            _file_assignment("web", "/project/app.ini", "config", FileAction.COPY),
        ),
    )
    assert json.loads(path.read_text()) == {
        "schema_version": 1,
        "mounts": [{"resolved_path": "/project/data", "action": "copy"}],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
```

Add a transaction test with pre-existing `.env`, `ports.json`, and `mounts.json`. Monkeypatch `write_current_mounts` to raise `PackageError("mounts failed")`, call the four-argument `write_current_configuration`, and assert the byte content and modes of all three snapshots equal their pre-call values. Add a second case where `mounts.json` did not previously exist and assert rollback removes it.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_current_config.py -q
```

Expected: FAIL because the mount writer and four-argument transaction do not exist.

- [ ] **Step 3: Implement writer and extend the transaction**

Implement `write_current_mounts` by filtering `FileAssignment.kind == "bind"`, normalizing `resolved_path`, rejecting conflicting actions for one path, sorting by path, serializing `_MountSnapshot`, and calling `_atomic_write_snapshot`.

Extend `write_current_configuration` so `targets` contains all three snapshot paths and its `try` block calls environment, ports, then mounts. Return all three paths only after all writes succeed. Keep the existing rollback loop so every previous file and mode is restored. Update `_perform_package` to pass `plan.files`:

```python
write_current_configuration(
    paths.project_root,
    plan.environment,
    plan.ports,
    plan.files,
)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Run CLI regression tests for the changed call site**

```bash
uv run --project skills/package-docker-app pytest \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py -q
```

Expected: PASS with only environment-dependent tests skipped.

- [ ] **Step 6: Commit transactional persistence**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/current_config.py \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_current_config.py
git commit -m "feat: persist current mount decisions"
```

---

### Task 4: Prove successful-package reuse and legacy migration

**Files:**
- Modify: `tests/e2e/test_package_flow.py`

**Interfaces:**
- Consumes: the complete inspect-plan-package behavior implemented in Tasks 1–3.
- Produces: end-to-end regression coverage for both `copy` and `keep_server_path`, plus old-project migration.

- [ ] **Step 1: Extend the successful multi-service package test**

After the existing package assertions, read `.docker-manage/mounts.json` and assert:

```python
assert json.loads(
    (project / ".docker-manage/mounts.json").read_text(encoding="utf-8")
) == {
    "schema_version": 1,
    "mounts": [
        {"resolved_path": str((project / "config").resolve()), "action": "copy"},
        {
            "resolved_path": str((project / "data").resolve()),
            "action": "keep_server_path",
        },
    ],
}
```

Invoke a second `inspect --json`; locate both file questions with `_file_question_id` and assert the config default is `copy`, the data default is `keep_server_path`, and both prompts identify `.docker-manage/mounts.json` as their source.

- [ ] **Step 2: Verify the extended test would fail without persistence**

Temporarily disable the new mount write call or run the test against the Task 2 commit, then run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/e2e/test_package_flow.py::test_multi_service_package_is_complete -q
```

Expected: FAIL because `mounts.json` is absent or the prior `copy` decision falls back to `keep_server_path`. Restore the Task 3 implementation immediately after observing RED.

- [ ] **Step 3: Run the end-to-end reuse test and verify GREEN**

Run the Step 2 command again.

Expected: PASS.

- [ ] **Step 4: Add and run the legacy migration case**

Create a bind-based project with only valid `.docker-manage/.env` and `ports.json` snapshots. Assert its first inspection succeeds and uses the generic mount default, then complete a fake-Docker package and assert `mounts.json` is created. Run:

```bash
uv run --project skills/package-docker-app pytest tests/e2e/test_package_flow.py -q
```

Expected: PASS with only environment-dependent tests skipped.

- [ ] **Step 5: Commit end-to-end coverage**

```bash
git add tests/e2e/test_package_flow.py
git commit -m "test: cover mount decision reuse and migration"
```

---

### Task 5: Update the package skill contract and documentation

**Files:**
- Modify: `skills/package-docker-app/SKILL.md`
- Modify: `tests/unit/test_skill_contract.py`

**Interfaces:**
- Produces: user-facing rules that distinguish stored current mount decisions from generic defaults.

- [ ] **Step 1: Write failing skill-contract assertions**

Extend `test_skill_requires_explicit_bind_copy_or_server_preservation` or add a focused test requiring all of:

```python
required = (
    ".docker-manage/mounts.json",
    "最近一次完整成功打包使用的 bind mount 决策",
    "上次选择 `copy`",
    "上次选择 `keep_server_path`",
    "缺少挂载快照",
    "不得从历史 `state.json`",
    "一起更新",
)
```

- [ ] **Step 2: Run the contract test and verify RED**

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -q
```

Expected: FAIL because the skill does not mention the new snapshot contract.

- [ ] **Step 3: Update `SKILL.md` precisely**

Document:

- `mounts.json` meaning, ownership, private atomic update, and no history inference.
- Repeat-mode detection still uses only `.env` plus `ports.json`.
- A missing `mounts.json` is compatible and uses generic bind defaults.
- Stored `copy`/`keep_server_path` is shown as current configuration and becomes the final default.
- A new or path-changed bind uses existing project-internal/project-external rules.
- All three snapshots update together only after a complete successful package.

- [ ] **Step 4: Run contract tests and validate the skill**

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -q
python /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
```

Expected: tests PASS and validator prints `Skill is valid!`.

- [ ] **Step 5: Commit the skill contract**

```bash
git add skills/package-docker-app/SKILL.md tests/unit/test_skill_contract.py
git commit -m "docs: define current mount snapshot workflow"
```

---

### Task 6: Full verification and focused review

**Files:**
- Review: all files changed by Tasks 1–5

**Interfaces:**
- Verifies the complete feature against the approved design spec.

- [ ] **Step 1: Run formatting and static checks**

```bash
uv run --project skills/package-docker-app ruff format --check \
  skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
```

Expected: both commands exit `0` without diagnostics.

- [ ] **Step 2: Run the complete test suite**

```bash
uv run --project skills/package-docker-app pytest -q
```

Expected: exit `0`, zero failures; Docker-dependent tests may be reported as skipped.

- [ ] **Step 3: Re-run skill validation**

```bash
python /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
```

Expected: `Skill is valid!`.

- [ ] **Step 4: Audit requirements and the final diff**

```bash
git diff 34b19f3..HEAD --check
git diff 34b19f3..HEAD -- \
  skills/package-docker-app \
  tests \
  docs/superpowers/specs/2026-08-13-current-mount-snapshot-design.md
git status --short
```

Confirm line by line that the approved design's completion criteria are covered, no secrets or local `.docker-manage/` contents are staged, no current decision comes from historical state, and no unrelated files changed.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` on the complete diff. Address only verified actionable findings, rerun affected focused tests, then repeat Steps 1–3 before reporting completion.
