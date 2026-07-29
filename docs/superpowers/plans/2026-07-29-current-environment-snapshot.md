# Current Environment Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `.docker-manage/.env` 保存最近一次完整成功打包所使用的环境变量，并让后续检查优先展示和采用这份项目级当前配置。

**Architecture:** 新增聚焦的 `current_config.py`，负责读取、匹配和原子写入当前环境变量快照；`EnvCandidate` 保存检查时看到的可选当前值，`questions.py` 决定展示和默认值优先级。CLI 只在环境变量发现完成后附加快照，只在最终归档验证成功后写回快照，历史 `state.json` 不参与跨任务选择。

**Tech Stack:** Python 3.11+、Pydantic 2、python-dotenv 1.1、pytest 8、Ruff、Docker Compose v2

## Global Constraints

- 工作分支固定为 `codex/reuse-docker-manage-env`。
- 使用 TDD；每个生产行为必须先看到对应测试因功能缺失而失败。
- `.docker-manage/.env` 是 CLI 管理的完整项目级快照，不是 Compose `env_file`。
- 只匹配已经由源码、Compose、Dockerfile 或模型补充明确发现的环境变量；未知快照键不得生成问题。
- 匹配优先级固定为服务专属键（例如 `WEB_PORT`）、通用键（例如 `PORT`）、唯一声明默认值。
- 当前值与声明默认值必须分别展示；两者不同不构成默认值冲突。
- 只有完整成功并验证最终归档后才能更新快照；inspect、plan、dry-run、模型补充等待和失败任务不得更新。
- 快照使用 `0600` 权限、同目录临时文件、`fsync` 和 `os.replace` 原子替换。
- 当前快照、状态和对话中的密码、Token、Key 保持完整值，不脱敏。
- 保持答案 JSON、问题 ID、命令参数、计划哈希和归档结构兼容。
- 不从 `.docker-manage/work/<run_id>/state.json` 推断当前配置。

---

### Task 1: 建立当前快照读取边界和持久模型

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Create: `tests/unit/test_current_config.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/models.py:47-50`
- Modify: `skills/package-docker-app/scripts/docker_package_app/planning.py:1-10, 91-113, 248-249`
- Modify: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: `EnvCandidate`, `DefaultValue`, `SourceRef` and a project root `Path`.
- Produces: `artifact_component(value: str) -> str`.
- Produces: `attach_current_values(project_root: Path, candidates: Sequence[EnvCandidate]) -> tuple[EnvCandidate, ...]`.
- Produces: `EnvCandidate.current: DefaultValue | None`, defaulting to `None` for old inspection state.

- [ ] **Step 1: Write failing model and snapshot-reader tests**

Create `tests/unit/test_current_config.py`:

```python
from pathlib import Path

import pytest
from docker_package_app.current_config import (
    CURRENT_ENV_SOURCE,
    artifact_component,
    attach_current_values,
)
from docker_package_app.errors import UsageError
from docker_package_app.models import DefaultValue, EnvCandidate, SourceRef


def _candidate(service: str, name: str, default: str = "declared") -> EnvCandidate:
    source = SourceRef(path=f"{service}.py", line=1)
    return EnvCandidate(
        service=service,
        name=name,
        defaults=(DefaultValue(value=default, source=source),),
        sources=(source,),
    )


def test_missing_snapshot_preserves_discovered_candidates(tmp_path: Path) -> None:
    candidates = (_candidate("web", "PORT", "8000"),)

    attached = attach_current_values(tmp_path, candidates)

    assert attached == candidates
    assert attached[0].current is None


def test_service_key_precedes_generic_and_unknown_keys_are_ignored(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text(
        "PORT='7000'\nWEB_PORT='8000'\nEMPTY=\nUNKNOWN='ignored'\n",
        encoding="utf-8",
    )
    candidates = (
        _candidate("web", "PORT"),
        _candidate("worker", "PORT"),
        _candidate("web", "EMPTY"),
    )

    attached = attach_current_values(tmp_path, candidates)

    assert [item.current.value if item.current else None for item in attached] == [
        "8000",
        "7000",
        "",
    ]
    assert all(
        item.current is not None
        and item.current.source == SourceRef(path=CURRENT_ENV_SOURCE)
        for item in attached
    )
    assert {(item.service, item.name) for item in attached} == {
        ("web", "PORT"),
        ("worker", "PORT"),
        ("web", "EMPTY"),
    }


def test_matched_key_without_value_is_rejected(tmp_path: Path) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT\n", encoding="utf-8")

    with pytest.raises(UsageError, match="PORT.*没有具体值"):
        attach_current_values(tmp_path, (_candidate("web", "PORT"),))


def test_artifact_component_matches_existing_service_prefix_rule() -> None:
    assert artifact_component("api-web") == "API_WEB"
    assert artifact_component("worker_2") == "WORKER_2"
```

In `tests/unit/test_models.py`, add `DefaultValue` to the existing model imports and add:

```python
def test_env_candidate_current_value_is_optional_and_round_trips() -> None:
    old = EnvCandidate.model_validate({"service": "web", "name": "PORT"})
    assert old.current is None

    current = DefaultValue(
        value="8322",
        source=SourceRef(path=".docker-manage/.env"),
    )
    restored = EnvCandidate.model_validate_json(
        EnvCandidate(service="web", name="PORT", current=current).model_dump_json()
    )

    assert restored.current == current
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py \
  tests/unit/test_models.py::test_env_candidate_current_value_is_optional_and_round_trips \
  -v
```

Expected: FAIL because `docker_package_app.current_config` and `EnvCandidate.current` do not exist.

- [ ] **Step 3: Add the optional current-value field and snapshot reader**

In `skills/package-docker-app/scripts/docker_package_app/models.py`, change `EnvCandidate` to:

```python
class EnvCandidate(StrictModel):
    service: str
    name: str
    defaults: tuple[DefaultValue, ...] = ()
    sources: tuple[SourceRef, ...] = ()
    current: DefaultValue | None = None
```

Create `skills/package-docker-app/scripts/docker_package_app/current_config.py`:

```python
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values

from docker_package_app.errors import UsageError
from docker_package_app.models import DefaultValue, EnvCandidate, SourceRef

CURRENT_ENV_RELATIVE = Path(".docker-manage/.env")
CURRENT_ENV_SOURCE = CURRENT_ENV_RELATIVE.as_posix()


def artifact_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def attach_current_values(
    project_root: Path,
    candidates: Sequence[EnvCandidate],
) -> tuple[EnvCandidate, ...]:
    snapshot = project_root.resolve() / CURRENT_ENV_RELATIVE
    if not snapshot.exists():
        return tuple(candidates)
    if not snapshot.is_file():
        raise UsageError(f"当前环境变量快照不是普通文件：{snapshot}")
    try:
        values = dotenv_values(snapshot)
    except OSError as exc:
        raise UsageError(f"无法读取当前环境变量快照 {snapshot}：{exc}") from exc

    attached: list[EnvCandidate] = []
    for candidate in candidates:
        service_key = f"{artifact_component(candidate.service)}_{candidate.name}"
        selected_key = (
            service_key
            if service_key in values
            else candidate.name
            if candidate.name in values
            else None
        )
        if selected_key is None:
            attached.append(candidate)
            continue
        value = values[selected_key]
        if value is None:
            raise UsageError(
                f"当前环境变量快照中的 {selected_key} 没有具体值；"
                f"空字符串请写成 {selected_key}="
            )
        attached.append(
            candidate.model_copy(
                update={
                    "current": DefaultValue(
                        value=value,
                        source=SourceRef(path=CURRENT_ENV_SOURCE),
                    )
                }
            )
        )
    return tuple(attached)
```

- [ ] **Step 4: Make planning reuse the same artifact-prefix function**

In `skills/package-docker-app/scripts/docker_package_app/planning.py`, add:

```python
from docker_package_app.current_config import artifact_component
```

Change the service-specific artifact assignment to:

```python
            if len(distinct_values) > 1:
                artifact_name = f"{artifact_component(service)}_{name}"
```

Delete the old private `_env_component()` function at the bottom of the file. Do not change any other planning behavior.

- [ ] **Step 5: Run snapshot, model, and planning tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py \
  tests/unit/test_models.py \
  tests/unit/test_planning.py \
  -v
```

Expected: all selected tests PASS; the existing `WEB_PORT` and `WORKER_PORT` planning assertions remain green.

- [ ] **Step 6: Commit the snapshot read boundary**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/current_config.py \
  skills/package-docker-app/scripts/docker_package_app/models.py \
  skills/package-docker-app/scripts/docker_package_app/planning.py \
  tests/unit/test_current_config.py \
  tests/unit/test_models.py
git commit -m "feat: read current environment snapshot"
```

---

### Task 2: 展示当前值并把检查快照带入规划

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/questions.py:14-33, 114-126`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py:28-31, 339-363`
- Modify: `tests/unit/test_questions.py`
- Modify: `tests/integration/test_cli.py`

**Interfaces:**
- Consumes: `attach_current_values(project_root, inspection.env)` from Task 1.
- Consumes: `EnvCandidate.current`.
- Produces: environment `Question.default` equal to the current snapshot value when present.
- Produces: `Inspection.env[*].current` in `inspect --json` and persisted `state.json`.

- [ ] **Step 1: Write failing question-priority tests**

In `tests/unit/test_questions.py`, add:

```python
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
```

Update `test_questions_show_conflicting_secret_defaults()` to expect the clearer label while preserving its conflict behavior:

```python
    assert format_environment_questions((question,)) == (
        (
            "1. 设置 web.API_KEY。声明默认值来源：.env:1=secret-one, "
            "app.py:4=secret-two，必填，默认值冲突"
        ),
    )
```

- [ ] **Step 2: Write a failing inspect-to-plan integration test**

In `tests/integration/test_cli.py`, add:

```python
def test_inspect_and_plan_use_current_environment_snapshot(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='9123'\nUNKNOWN='ignored'\n", encoding="utf-8")

    inspected = cli("inspect", str(compose_project), "--json")

    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    assert [(item["service"], item["name"]) for item in body["env"]] == [
        ("web", "PORT")
    ]
    assert body["env"][0]["current"] == {
        "value": "9123",
        "source": {"path": ".docker-manage/.env", "line": None},
    }
    env_question = next(
        question for question in body["questions"] if question["id"] == "env.web.PORT"
    )
    assert env_question["default"] == "9123"
    assert "当前配置值：9123" in env_question["prompt"]

    answers = compose_project / "current-answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": env_question["default"],
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    planned = cli(
        "plan",
        str(compose_project),
        "--run-id",
        body["run_id"],
        "--non-interactive",
        "--answers",
        str(answers),
        "--json",
    )

    assert planned.returncode == 0, planned.stderr
    environment = json.loads(planned.stdout)["plan"]["environment"]
    assert environment[0]["value"] == "9123"
```

- [ ] **Step 3: Run the question and integration regressions and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py::test_current_value_becomes_default_without_hiding_declared_defaults \
  tests/unit/test_questions.py::test_questions_show_conflicting_secret_defaults \
  tests/integration/test_cli.py::test_inspect_and_plan_use_current_environment_snapshot \
  -v
```

Expected: FAIL because questions ignore `current`, use the old “默认值来源” label, and CLI inspection does not attach `.docker-manage/.env`.

- [ ] **Step 4: Implement current-value display and priority**

In `skills/package-docker-app/scripts/docker_package_app/questions.py`, replace the environment-question prefix of `build_questions()` with:

```python
    for candidate in sorted(inspection.env, key=lambda item: (item.service, item.name)):
        values = sorted({item.value for item in candidate.defaults})
        default = (
            candidate.current.value
            if candidate.current is not None
            else values[0]
            if len(values) == 1
            else None
        )
        source_text = ", ".join(
            f"{item.source.path}:{item.source.line or '-'}={item.value}"
            for item in candidate.defaults
        )
        prompt = f"设置 {candidate.service}.{candidate.name}"
        if candidate.current is not None:
            prompt += (
                f"。当前配置值：{candidate.current.value}，"
                f"来源：{candidate.current.source.path}"
            )
        if source_text:
            prompt += f"。声明默认值来源：{source_text}"
        questions.append(
            Question(
                id=f"env.{candidate.service}.{candidate.name}",
                kind="env",
                prompt=prompt,
                default=default,
            )
        )
```

In `format_environment_questions()`, replace the conflict-source check with:

```python
        elif "声明默认值来源：" in question.prompt:
            suffix = "，必填，默认值冲突"
```

- [ ] **Step 5: Attach the snapshot after all environment discovery**

In `skills/package-docker-app/scripts/docker_package_app/cli.py`, import:

```python
from docker_package_app.current_config import attach_current_values
```

Immediately before building final questions in `_perform_inspect()`, after supplement ambiguity handling, add:

```python
    inspection = inspection.model_copy(
        update={
            "env": attach_current_values(project, inspection.env),
        }
    )
    questions = (*build_questions(inspection), *extra_questions)
```

This placement must remain after `merge_supplement()` so supplement-discovered variables can also receive current values, and before `_store_initial()` so `plan` reuses the exact inspection snapshot.

- [ ] **Step 6: Run question and integration tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py \
  -v
```

Expected: all selected tests PASS; existing no-snapshot defaults and sparse override behavior remain green.

- [ ] **Step 7: Commit current-value inspection behavior**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/questions.py \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py
git commit -m "feat: prefer current environment values"
```

---

### Task 3: 在完整成功后原子写回最新快照

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py:15-20, 461-472`
- Modify: `tests/unit/test_current_config.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/e2e/test_package_flow.py`

**Interfaces:**
- Consumes: `Sequence[EnvAssignment]` from `PackagePlan.environment`.
- Produces: `write_current_environment(project_root: Path, assignments: Sequence[EnvAssignment]) -> Path`.
- Guarantees: complete sorted dotenv snapshot, `0600` mode, old file preserved on pre-replace or replace failure.

- [ ] **Step 1: Write failing atomic-writer unit tests**

Add these imports to `tests/unit/test_current_config.py`:

```python
import stat

from dotenv import dotenv_values
from docker_package_app.current_config import write_current_environment
from docker_package_app.errors import PackageError
from docker_package_app.models import EnvAssignment
```

Add:

```python
def test_write_current_environment_is_complete_sorted_and_private(
    tmp_path: Path,
) -> None:
    previous = tmp_path / ".docker-manage/.env"
    previous.parent.mkdir()
    previous.write_text("STALE='remove-me'\n", encoding="utf-8")

    path = write_current_environment(
        tmp_path,
        (
            EnvAssignment(
                service="web",
                container_name="PORT",
                artifact_name="PORT",
                value="8322",
            ),
            EnvAssignment(
                service="web",
                container_name="API_KEY",
                artifact_name="API_KEY",
                value="secret",
            ),
        ),
    )

    assert path == tmp_path / ".docker-manage/.env"
    assert dotenv_values(path) == {"API_KEY": "secret", "PORT": "8322"}
    assert path.read_text(encoding="utf-8").splitlines() == [
        "API_KEY='secret'",
        "PORT='8322'",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_failure_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='last-good'\n", encoding="utf-8")
    monkeypatch.setattr(
        "docker_package_app.current_config.os.replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(PackageError, match="无法更新当前环境变量快照"):
        write_current_environment(
            tmp_path,
            (
                EnvAssignment(
                    service="web",
                    container_name="PORT",
                    artifact_name="PORT",
                    value="next",
                ),
            ),
        )

    assert snapshot.read_text(encoding="utf-8") == "PORT='last-good'\n"
```

- [ ] **Step 2: Write failing success and failure workflow tests**

In `tests/e2e/test_package_flow.py`, add:

```python
def test_successful_package_becomes_next_inspection_current_config(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    first_answers = compose_project / "first-answers.json"
    first_answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "9123",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    first_answers.chmod(0o600)

    first = cli(
        "run",
        str(compose_project),
        "--non-interactive",
        "--answers",
        str(first_answers),
        "--app-name",
        "snapshot-app",
        "--version",
        "v1",
        "--json",
        env={"FAKE_DOCKER_INSPECT": json.dumps([_metadata("sha256:web-v1")])},
    )

    assert first.returncode == 0, first.stderr
    snapshot = compose_project / ".docker-manage/.env"
    assert snapshot.read_text(encoding="utf-8") == "PORT='9123'\n"

    inspected = cli("inspect", str(compose_project), "--json")
    assert inspected.returncode == 0, inspected.stderr
    question = next(
        item
        for item in json.loads(inspected.stdout)["questions"]
        if item["id"] == "env.web.PORT"
    )
    assert question["default"] == "9123"
    assert "当前配置值：9123" in question["prompt"]
```

In `tests/integration/test_cli.py`, add:

```python
def test_failed_package_does_not_replace_current_environment(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='last-good'\n", encoding="utf-8")
    answers = compose_project / "failed-package-answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "next",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8080",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    inspected = cli("inspect", str(compose_project), "--json")
    run_id = json.loads(inspected.stdout)["run_id"]
    planned = cli(
        "plan",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(answers),
        "--json",
    )
    plan_hash = json.loads(planned.stdout)["plan_hash"]

    packaged = cli(
        "package",
        str(compose_project),
        "--run-id",
        run_id,
        "--non-interactive",
        "--answers",
        str(answers),
        "--confirm-plan-hash",
        plan_hash,
        "--json",
        env={"FAKE_DOCKER_EXIT": "23"},
    )

    assert packaged.returncode == 1
    assert snapshot.read_text(encoding="utf-8") == "PORT='last-good'\n"
```

Also extend the existing `test_dry_run_stops_before_docker_mutations()` in
`tests/integration/test_cli.py` so it proves dry-run preservation:

```python
    snapshot = compose_project / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='last-good'\n", encoding="utf-8")

    result = cli(
        "run",
        str(compose_project),
        "--dry-run",
        "--non-interactive",
        "--answers",
        str(complete_answers),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["stage"] == "planned"
    assert snapshot.read_text(encoding="utf-8") == "PORT='last-good'\n"
```

Retain the test's existing Docker mutation assertions after the new snapshot
assertion.

- [ ] **Step 3: Run writer and workflow tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py::test_write_current_environment_is_complete_sorted_and_private \
  tests/unit/test_current_config.py::test_write_failure_preserves_previous_snapshot \
  tests/e2e/test_package_flow.py::test_successful_package_becomes_next_inspection_current_config \
  tests/integration/test_cli.py::test_failed_package_does_not_replace_current_environment \
  -v
```

Expected: FAIL because `write_current_environment` does not exist and successful package does not update the project snapshot.

- [ ] **Step 4: Implement atomic dotenv snapshot writing**

Extend `skills/package-docker-app/scripts/docker_package_app/current_config.py` imports:

```python
import os
import tempfile

from dotenv import dotenv_values, set_key

from docker_package_app.errors import PackageError, UsageError
from docker_package_app.models import (
    DefaultValue,
    EnvAssignment,
    EnvCandidate,
    SourceRef,
)
```

Add:

```python
def write_current_environment(
    project_root: Path,
    assignments: Sequence[EnvAssignment],
) -> Path:
    target = project_root.resolve() / CURRENT_ENV_RELATIVE
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    temporary: Path | None = None
    values = {
        assignment.artifact_name: assignment.value
        for assignment in assignments
    }
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        for name in sorted(values):
            set_key(
                temporary,
                name,
                values[name],
                quote_mode="always",
            )
        temporary.chmod(0o600)
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    except OSError as exc:
        raise PackageError(f"无法更新当前环境变量快照 {target}：{exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
```

Keep `UsageError` for read failures and `PackageError` for write failures.

- [ ] **Step 5: Call the writer only after the verified archive exists**

In `skills/package-docker-app/scripts/docker_package_app/cli.py`, import:

```python
from docker_package_app.current_config import (
    attach_current_values,
    write_current_environment,
)
```

In `_perform_package()`, immediately after `create_verified_archive(...)` returns and before transitioning to `Stage.PACKAGED`, add:

```python
    write_current_environment(paths.project_root, plan.environment)
    state = _transition(state, Stage.PACKAGED, archive=str(archive))
```

Do not call the writer from `_perform_inspect()`, `_perform_plan()`, the `--dry-run` branch, or any earlier package stage.

- [ ] **Step 6: Run writer and workflow tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py \
  -v
```

Expected: all selected tests PASS; successful packaging writes the snapshot and the forced package failure preserves `last-good`.

- [ ] **Step 7: Commit atomic current-snapshot persistence**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/current_config.py \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_current_config.py \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py
git commit -m "feat: persist successful environment snapshot"
```

---

### Task 4: 更新技能契约并完成质量门禁

**Files:**
- Modify: `skills/package-docker-app/SKILL.md:15-24, 45-65, 87-99`
- Modify: `tests/unit/test_skill_contract.py`

**Interfaces:**
- Consumes: the CLI behavior completed in Tasks 1–3.
- Produces: an explicit agent contract for showing current values, expanding unmodified values, and allowing only the CLI to update `.docker-manage/.env`.

- [ ] **Step 1: Write a failing skill-contract test**

In `tests/unit/test_skill_contract.py`, add:

```python
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
```

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py::test_skill_uses_project_current_environment_snapshot \
  -v
```

Expected: FAIL because the current skill text does not define the snapshot contract.

- [ ] **Step 3: Document the exact current-snapshot workflow**

In the `SKILL.md` invariants section, add these bullets:

```markdown
- `<project>/.docker-manage/.env` 表示最近一次完整成功打包使用的项目级当前环境变量配置；不得从历史 `state.json` 推断当前配置。
- 模型不得直接编辑 `.docker-manage/.env`。只有随附 CLI 可以在完整成功打包后原子更新该文件。
```

In the environment-question workflow, require this behavior:

```markdown
如果问题同时包含当前配置值和声明默认值，完整显示两者及各自来源，并优先采用当前配置值。当前配置值与声明默认值不同不算冲突；用户未提交覆盖项时自动把当前配置值展开到答案 JSON。只有没有当前配置值且声明默认值缺失或冲突的项目才必须填写。
```

After the package command description, add:

```markdown
CLI 只有完整成功打包后才会更新 `.docker-manage/.env`。`inspect`、`plan`、`--dry-run`、等待模型补充和失败任务不得改变当前配置快照。
```

Do not weaken the existing requirements to show secrets in full, present the complete plan, or require explicit plan-hash confirmation.

- [ ] **Step 4: Run the skill contract and focused feature suite**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py \
  tests/unit/test_current_config.py \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py \
  -v
```

Expected: all selected tests PASS.

- [ ] **Step 5: Run the full quality gate**

Run:

```bash
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app pytest \
  tests/unit tests/integration tests/e2e \
  -v --cov=docker_package_app --cov-report=term-missing --cov-fail-under=85
uv run --with pyyaml python \
  /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
git diff --check
git status --short --branch
```

Expected: Ruff exits zero; the complete non-smoke suite has zero failures and at least 85% coverage; skill validation reports success; `git diff --check` emits no output; status lists only the intended Task 4 files before commit.

- [ ] **Step 6: Review the final diff against the design**

Run:

```bash
git diff -- \
  skills/package-docker-app \
  tests
```

Confirm every design requirement has direct evidence:

- current value lookup is service-specific then generic;
- unknown snapshot keys do not create questions;
- current and declared values remain distinguishable;
- no-snapshot behavior is unchanged;
- only verified successful packaging writes `.docker-manage/.env`;
- failure preserves the prior snapshot;
- old `state.json` remains readable;
- the skill instructs the model not to edit the snapshot directly.

- [ ] **Step 7: Commit the documented and verified feature**

```bash
git add \
  skills/package-docker-app/SKILL.md \
  tests/unit/test_skill_contract.py
git commit -m "docs: describe current environment snapshot"
```

- [ ] **Step 8: Verify the committed branch is clean**

Run:

```bash
git status --short --branch
git log -5 --oneline --decorate
```

Expected: branch is `codex/reuse-docker-manage-env`, the worktree has no uncommitted files, and the latest four commits are the three tested feature increments plus the skill-contract update.
