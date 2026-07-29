# Bind Mount Server Data Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个 bind mount 在打包计划中明确选择复制本机内容或保留服务器路径，并保证选择保留的目录完全不进入归档、不会在服务器重复原地解压时覆盖现有数据。

**Architecture:** `questions.py` 按解析后的 source 路径生成稳定且去重的处理问题，`planning.py` 把答案固化为带依赖种类的 `FileAssignment`。`files.py` 使用完整文件身份物化选择复制的依赖，并产出按服务与依赖种类区分的 rewrite；`render.py` 只改写对应的 Compose 引用。CLI 直接使用已确认计划物化载荷，manifest 继续从计划中的 `KEEP_SERVER_PATH` 分配生成服务器前置路径。

**Tech Stack:** Python 3.11+、Pydantic 2、PyYAML 6、pytest 8、Ruff 0.12、Docker Compose v2

## Global Constraints

- 工作分支固定为 `codex/reuse-docker-manage-env`。
- 使用 TDD；每个生产行为必须先看到对应测试因功能缺失而失败。
- 每个项目内 bind source 提供 `copy`、`keep_server_path`、`abort`，默认值为 `copy`。
- 每个项目外本地文件依赖只提供 `keep_server_path`、`abort`，不得复制项目目录外内容。
- 项目内 config 和 secret 继续自动复制；项目外 config 和 secret 保持现有保留或中止规则。
- 同一个 bind source 被多个服务引用时共享一个问题，但计划保留每个引用的独立记录。
- 文件决定的内部身份必须包含 `service`、`kind`、原始 Compose 值和解析路径。
- `keep_server_path` 不得创建任何载荷目录或文件，不得产生 rewrite，Compose 必须保留原始 source。
- 相对 source 不得替换为开发电脑的绝对路径；manifest 可以记录解析后的服务器前置路径。
- `copy` 的 bind 副本继续使用目录 `0777`、普通文件 `0666`；config 和 secret 不放宽权限。
- named volume、端口、环境变量、镜像、计划确认哈希和归档格式保持兼容。
- CLI 不修改原始 Compose、原始挂载目录、服务器目录或其权限。
- 普通覆盖式解压只保证不覆盖归档中不存在的保留路径；不实现或支持先清空部署目录的外部脚本。

---

### Task 1: 建立 bind 问题和带类型的计划决定

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/models.py:123-129`
- Modify: `skills/package-docker-app/scripts/docker_package_app/questions.py:124-143`
- Modify: `skills/package-docker-app/scripts/docker_package_app/planning.py:35-88, 198-232`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/unit/test_questions.py`
- Modify: `tests/unit/test_planning.py`

**Interfaces:**
- Consumes: `Inspection.files: tuple[FileCandidate, ...]` and `AnswerBook.values`.
- Produces: `FileAssignment.kind: Literal["bind", "config", "secret"]`.
- Produces: `_file_question_id(resolved_path: str) -> str`, unchanged and shared by all references to one resolved source.
- Produces: `_build_files(inspection: Inspection, answers: AnswerBook) -> tuple[FileAssignment, ...]`.
- Produces: `_copied_input_bytes(inspection: Inspection, assignments: Sequence[FileAssignment]) -> int`.

- [ ] **Step 1: Write failing model and question tests**

Add `FileAction` and `FileAssignment` to the imports in
`tests/unit/test_models.py`, then add:

```python
def test_file_assignment_records_dependency_kind() -> None:
    assignment = FileAssignment(
        service="web",
        original_value="./data",
        resolved_path="/project/data",
        kind="bind",
        action=FileAction.KEEP_SERVER_PATH,
    )

    restored = FileAssignment.model_validate_json(assignment.model_dump_json())

    assert restored.kind == "bind"
```

Add `FileCandidate` to the model imports and `_file_question_id` to the
question imports in `tests/unit/test_questions.py`, then add:

```python
def _file(
    *,
    service: str,
    source: str,
    resolved: str,
    kind: str = "bind",
    inside: bool = True,
    size: int = 12,
) -> FileCandidate:
    return FileCandidate(
        service=service,
        compose_value=source,
        resolved_path=resolved,
        kind=kind,
        inside_project=inside,
        project_path=Path(resolved).name if inside else None,
        estimated_size=size,
    )


def test_project_bind_question_offers_copy_keep_and_abort() -> None:
    candidate = _file(
        service="web",
        source="./data",
        resolved="/project/data",
        size=2048,
    )
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=(candidate,),
    )

    question = build_questions(inspection)[0]

    assert question.id == _file_question_id("/project/data")
    assert question.kind == "file"
    assert question.default == "copy"
    assert question.choices == ("copy", "keep_server_path", "abort")
    assert "web" in question.prompt
    assert "./data" in question.prompt
    assert "/project/data" in question.prompt
    assert "项目目录内" in question.prompt
    assert "2048" in question.prompt


def test_shared_bind_source_generates_one_question() -> None:
    files = (
        _file(
            service="api",
            source="./data",
            resolved="/project/data",
        ),
        _file(
            service="worker",
            source="./data",
            resolved="/project/data",
        ),
    )
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=files,
    )

    questions = build_questions(inspection)

    assert len(questions) == 1
    assert "api" in questions[0].prompt
    assert "worker" in questions[0].prompt


def test_external_dependency_keeps_existing_choices_without_default() -> None:
    candidate = _file(
        service="web",
        source="/srv/app.ini",
        resolved="/srv/app.ini",
        kind="config",
        inside=False,
    )
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=(candidate,),
    )

    question = build_questions(inspection)[0]

    assert question.default is None
    assert question.choices == ("keep_server_path", "abort")
    assert "项目目录外" in question.prompt


def test_project_config_and_secret_do_not_generate_file_questions() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        files=(
            _file(
                service="web",
                source="./app.ini",
                resolved="/project/app.ini",
                kind="config",
            ),
            _file(
                service="web",
                source="./secret.txt",
                resolved="/project/secret.txt",
                kind="secret",
            ),
        ),
    )

    assert build_questions(inspection) == ()
```

Add `from pathlib import Path` at the top of `tests/unit/test_questions.py`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py::test_file_assignment_records_dependency_kind \
  tests/unit/test_questions.py::test_project_bind_question_offers_copy_keep_and_abort \
  tests/unit/test_questions.py::test_shared_bind_source_generates_one_question \
  tests/unit/test_questions.py::test_external_dependency_keeps_existing_choices_without_default \
  tests/unit/test_questions.py::test_project_config_and_secret_do_not_generate_file_questions \
  -v
```

Expected: FAIL because `FileAssignment.kind` does not exist and project-local
binds do not yet generate questions.

- [ ] **Step 3: Add the dependency kind and grouped file questions**

Change `FileAssignment` in `models.py` to:

```python
class FileAssignment(StrictModel):
    service: str
    original_value: str
    resolved_path: str
    kind: Literal["bind", "config", "secret"]
    action: FileAction
    payload_path: str | None = None
```

Add `defaultdict` to the imports in `questions.py`:

```python
from collections import defaultdict
```

Replace the current external-file question loop in `build_questions()` with:

```python
    grouped_files: dict[str, list[FileCandidate]] = defaultdict(list)
    for candidate in inspection.files:
        if candidate.kind == "bind" or not candidate.inside_project:
            grouped_files[candidate.resolved_path].append(candidate)

    for resolved_path in sorted(grouped_files):
        candidates = grouped_files[resolved_path]
        inside_project = all(item.inside_project for item in candidates)
        services = ", ".join(sorted({item.service for item in candidates}))
        compose_values = ", ".join(
            sorted({item.compose_value for item in candidates})
        )
        kinds = ", ".join(sorted({item.kind for item in candidates}))
        estimated_size = max(item.estimated_size for item in candidates)
        location = "项目目录内" if inside_project else "项目目录外"
        if inside_project:
            choices = ("copy", "keep_server_path", "abort")
            default = "copy"
            meaning = (
                "copy（复制本机内容）、keep_server_path（保留服务器现有路径）"
                "或 abort（中止）"
            )
        else:
            choices = ("keep_server_path", "abort")
            default = None
            meaning = "keep_server_path（保留服务器路径）或 abort（中止）"
        questions.append(
            Question(
                id=_file_question_id(resolved_path),
                kind="file",
                prompt=(
                    f"本地依赖 {resolved_path} 位于{location}；"
                    f"服务：{services}；类型：{kinds}；"
                    f"Compose source：{compose_values}；"
                    f"估算大小：{estimated_size} 字节。请选择 {meaning}。"
                ),
                default=default,
                choices=choices,
            )
        )
```

Add `FileCandidate` to the model imports in `questions.py`.

- [ ] **Step 4: Run the model and question tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py \
  tests/unit/test_questions.py \
  -v
```

Expected: all tests PASS.

- [ ] **Step 5: Write failing planning tests for copy, preserve, abort, and identity**

In `tests/unit/test_planning.py`, import `FileAction` and
`_file_question_id`. Change `_answers()` to accept the inspection root and use
the exact project config path:

```python
def _answers(
    project_root: Path,
    *,
    collision: bool = False,
    file_decision: str = "copy",
) -> AnswerBook:
    return AnswerBook(
        values={
            "env.web.PORT": "8000",
            "env.worker.PORT": "9000",
            "port.web.8000/tcp.expose": "yes",
            "port.web.8000/tcp.host": "8080",
            "port.worker.9000/tcp.expose": "yes",
            "port.worker.9000/tcp.host": "8080" if collision else "9090",
            "image.redis.decision": "registry.intra/redis:7-approved",
            _file_question_id(str((project_root / "config").resolve())): file_decision,
        }
    )
```

Update existing `_answers()` calls to pass `tmp_path`, then add:

```python
def test_project_bind_can_keep_server_path(tmp_path: Path) -> None:
    plan = build_plan(
        _inspection(tmp_path),
        _answers(tmp_path, file_decision="keep_server_path"),
        app_name="demo",
        version="v1",
        platform="linux/amd64",
    )

    assignment = plan.files[0]
    assert assignment.kind == "bind"
    assert assignment.action is FileAction.KEEP_SERVER_PATH
    assert assignment.payload_path is None
    assert plan.disk.known_input_bytes == 0


def test_project_bind_abort_stops_planning(tmp_path: Path) -> None:
    with pytest.raises(PlanValidationError, match="已因路径.*config.*中止打包"):
        build_plan(
            _inspection(tmp_path),
            _answers(tmp_path, file_decision="abort"),
            app_name="demo",
            version="v1",
            platform="linux/amd64",
        )


def test_bind_keep_does_not_override_same_path_config_copy(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.ini"
    shared.write_text("mode=server\n", encoding="utf-8")
    inspection = _inspection(tmp_path).model_copy(
        update={
            "files": (
                FileCandidate(
                    service="web",
                    compose_value="./shared.ini",
                    resolved_path=str(shared),
                    kind="bind",
                    inside_project=True,
                    project_path="shared.ini",
                    estimated_size=12,
                ),
                FileCandidate(
                    service="web",
                    compose_value="./shared.ini",
                    resolved_path=str(shared),
                    kind="config",
                    inside_project=True,
                    project_path="shared.ini",
                    estimated_size=12,
                ),
            )
        }
    )
    values = dict(_answers(tmp_path).values)
    values.pop(_file_question_id(str((tmp_path / "config").resolve())))
    values[_file_question_id(str(shared.resolve()))] = "keep_server_path"

    plan = build_plan(
        inspection,
        AnswerBook(values=values),
        app_name="demo",
        version="v1",
        platform="linux/amd64",
    )

    decisions = {(item.kind, item.action) for item in plan.files}
    assert decisions == {
        ("bind", FileAction.KEEP_SERVER_PATH),
        ("config", FileAction.COPY),
    }
    assert plan.disk.known_input_bytes == 12
```

Also change the existing payload assertion to verify the new kind:

```python
    assert plan.files[0].kind == "bind"
    assert plan.files[0].payload_path == "files/config"
```

- [ ] **Step 6: Run planning tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_planning.py -v
```

Expected: FAIL because project bind decisions are ignored, `kind` is not
populated, and disk estimation still includes every discovered dependency.

- [ ] **Step 7: Implement answer-driven file planning**

Add `Sequence` to `planning.py` imports:

```python
from collections.abc import Sequence
```

Move file construction before the disk estimate in `build_plan()` and replace
the old known byte sum:

```python
    files = _build_files(inspection, answers)
    copied_input_bytes = _copied_input_bytes(inspection, files)
```

Then construct `DiskEstimate` with:

```python
        disk=DiskEstimate(
            known_input_bytes=copied_input_bytes,
            free_bytes=inspection.free_disk_bytes,
            unknown_components=unknown,
        ),
```

Replace `_build_files()` with:

```python
def _build_files(
    inspection: Inspection,
    answers: AnswerBook,
) -> tuple[FileAssignment, ...]:
    root = Path(inspection.project_root).resolve()
    assignments: list[FileAssignment] = []
    for item in sorted(
        inspection.files,
        key=lambda value: (
            value.resolved_path,
            value.service,
            value.kind,
            value.compose_value,
        ),
    ):
        path = Path(item.resolved_path).resolve()
        if item.kind == "bind" or not item.inside_project:
            decision = answers.values[_file_question_id(str(path))]
            if decision == "abort":
                raise PlanValidationError(f"已因路径 {path} 中止打包")
            action = FileAction(decision)
        else:
            action = FileAction.COPY

        if action is FileAction.COPY and not item.inside_project:
            raise PlanValidationError(f"无法复制项目目录之外的路径：{path}")

        payload_path = None
        if action is FileAction.COPY:
            relative = path.relative_to(root)
            payload = Path("files") / (
                relative if relative.parts else Path("project")
            )
            payload_path = payload.as_posix()

        assignments.append(
            FileAssignment(
                service=item.service,
                original_value=item.compose_value,
                resolved_path=str(path),
                kind=item.kind,
                action=action,
                payload_path=payload_path,
            )
        )
    return tuple(assignments)


def _copied_input_bytes(
    inspection: Inspection,
    assignments: Sequence[FileAssignment],
) -> int:
    copied_paths = {
        item.resolved_path
        for item in assignments
        if item.action is FileAction.COPY
    }
    size_by_path: dict[str, int] = {}
    for item in inspection.files:
        if item.resolved_path in copied_paths:
            size_by_path[item.resolved_path] = max(
                size_by_path.get(item.resolved_path, 0),
                item.estimated_size,
            )
    return sum(size_by_path.values())
```

- [ ] **Step 8: Run planning tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py \
  tests/unit/test_questions.py \
  tests/unit/test_planning.py \
  -v
```

Expected: all tests PASS.

- [ ] **Step 9: Commit the question and plan boundary**

Run:

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/models.py \
  skills/package-docker-app/scripts/docker_package_app/questions.py \
  skills/package-docker-app/scripts/docker_package_app/planning.py \
  tests/unit/test_models.py \
  tests/unit/test_questions.py \
  tests/unit/test_planning.py
git commit -m "feat: plan bind mount preservation"
```

Expected: one commit containing only model, question, planning, and focused
unit-test changes.

---

### Task 2: 按完整身份复制和改写文件依赖

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/files.py:13-100`
- Modify: `skills/package-docker-app/scripts/docker_package_app/render.py:14-17, 47-74, 124-144`
- Modify: `tests/unit/test_files.py`
- Modify: `tests/unit/test_render.py`

**Interfaces:**
- Consumes: `materialize_files(candidates: Sequence[FileCandidate], assignments: Sequence[FileAssignment], payload_root: Path)`.
- Produces: `FileIdentity = tuple[str, str, str, str]`.
- Produces: `FileRewriteKey = tuple[str, str, str]`.
- Produces: `FileMaterialization.rewrites: Mapping[FileRewriteKey, str]`.
- Consumes: `render_deployment(base: ComposeDocument, plan: PackagePlan, file_rewrites: Mapping[FileRewriteKey, str])`.

- [ ] **Step 1: Convert file tests to plan assignments and add preservation regressions**

In `tests/unit/test_files.py`, import `FileAssignment` and add:

```python
def _assignment(
    candidate: FileCandidate,
    action: FileAction,
) -> FileAssignment:
    payload_path = None
    if action is FileAction.COPY and candidate.project_path is not None:
        payload_path = f"files/{candidate.project_path}"
    return FileAssignment(
        service=candidate.service,
        original_value=candidate.compose_value,
        resolved_path=candidate.resolved_path,
        kind=candidate.kind,
        action=action,
        payload_path=payload_path,
    )
```

Replace every `materialize_files()` decision mapping in that file with a tuple
of `_assignment(candidate, action)` objects. For example:

```python
    result = materialize_files(
        candidates,
        tuple(_assignment(item, FileAction.COPY) for item in candidates),
        tmp_path / "payload",
    )
```

Update the existing rewrite assertion:

```python
    assert result.rewrites[("web", "bind", "./config")] == "./files/config"
```

Add:

```python
def test_kept_project_bind_is_not_materialized_or_rewritten(
    tmp_path: Path,
) -> None:
    source = tmp_path / "data"
    source.mkdir()
    (source / "local.db").write_text("developer-data", encoding="utf-8")
    (tmp_path / "app.ini").write_text("", encoding="utf-8")
    candidate = next(
        item
        for item in discover_file_dependencies(
            _compose(tmp_path, "./data"),
            tmp_path,
        )
        if item.kind == "bind"
    )

    result = materialize_files(
        (candidate,),
        (_assignment(candidate, FileAction.KEEP_SERVER_PATH),),
        tmp_path / "payload",
    )

    assert not (tmp_path / "payload/files/data").exists()
    assert result.rewrites == {}
    assert result.server_paths == (str(source.resolve()),)
    assert result.copied_bytes == 0


def test_same_source_bind_can_be_kept_while_config_is_copied(
    tmp_path: Path,
) -> None:
    source = tmp_path / "shared.ini"
    source.write_text("mode=server\n", encoding="utf-8")
    compose = ComposeDocument.from_data(
        tmp_path,
        {
            "services": {
                "web": {
                    "image": "demo:1",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "./shared.ini",
                            "target": "/app/shared.ini",
                        }
                    ],
                    "configs": [{"source": "shared"}],
                }
            },
            "configs": {"shared": {"file": "./shared.ini"}},
        },
    )
    candidates = discover_file_dependencies(compose, tmp_path)
    assignments = tuple(
        _assignment(
            item,
            FileAction.KEEP_SERVER_PATH
            if item.kind == "bind"
            else FileAction.COPY,
        )
        for item in candidates
    )

    result = materialize_files(candidates, assignments, tmp_path / "payload")

    assert (tmp_path / "payload/files/shared.ini").is_file()
    assert ("web", "bind", "./shared.ini") not in result.rewrites
    assert result.rewrites[
        ("web", "config", "./shared.ini")
    ] == "./files/shared.ini"
```

- [ ] **Step 2: Run file tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_files.py -v
```

Expected: FAIL because `materialize_files()` still accepts a path-keyed action
mapping and rewrites are keyed only by raw Compose value.

- [ ] **Step 3: Implement identity-aware materialization**

In `files.py`, import `FileAssignment` and define:

```python
FileIdentity = tuple[str, str, str, str]
FileRewriteKey = tuple[str, str, str]
```

Change `FileMaterialization` to:

```python
@dataclass(frozen=True)
class FileMaterialization:
    rewrites: Mapping[FileRewriteKey, str]
    server_paths: tuple[str, ...]
    copied_bytes: int
```

Add:

```python
def _candidate_identity(candidate: FileCandidate) -> FileIdentity:
    return (
        candidate.service,
        candidate.kind,
        candidate.compose_value,
        candidate.resolved_path,
    )


def _assignment_identity(assignment: FileAssignment) -> FileIdentity:
    return (
        assignment.service,
        assignment.kind,
        assignment.original_value,
        assignment.resolved_path,
    )


def _rewrite_key(candidate: FileCandidate) -> FileRewriteKey:
    return candidate.service, candidate.kind, candidate.compose_value
```

Change the `materialize_files()` signature and setup:

```python
def materialize_files(
    candidates: Sequence[FileCandidate],
    assignments: Sequence[FileAssignment],
    payload_root: Path,
) -> FileMaterialization:
    assignment_by_identity = {
        _assignment_identity(item): item
        for item in assignments
    }
    payload = payload_root.resolve()
    files_root = payload / "files"
    rewrites: dict[FileRewriteKey, str] = {}
```

Create `files_root` lazily only immediately before the first copy:

```python
        files_root.mkdir(mode=0o700, parents=True, exist_ok=True)
```

Replace the action lookup with:

```python
        assignment = assignment_by_identity.get(_candidate_identity(candidate))
        if assignment is None:
            raise AnswerRequired(
                "文件依赖缺少已确认决定："
                f"{candidate.service}/{candidate.kind}/{candidate.compose_value}"
            )
        action = assignment.action
```

Keep the existing external-copy rejection, copy-once behavior, byte count, bind
permission normalization, and server path collection. Replace rewrite insertion
with:

```python
        rewrites[_rewrite_key(candidate)] = (
            f"./files/{Path(candidate.project_path).as_posix()}"
        )
```

This order is required: a `KEEP_SERVER_PATH` assignment exits before creating
`files_root`, copying, recording a rewrite, or normalizing permissions.

- [ ] **Step 4: Run file tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_files.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Write failing render tests for service- and kind-specific rewrites**

In `tests/unit/test_render.py`, update the existing rewrite mapping to:

```python
        {
            ("web", "bind", "./config"): "./files/config",
            ("web", "config", "./app.ini"): "./files/app.ini",
        },
```

Add:

```python
def test_render_keeps_bind_when_same_source_is_only_copied_as_config(
    tmp_path: Path,
) -> None:
    base = _base(tmp_path)
    base.data["services"]["web"]["volumes"][0]["source"] = "./app.ini"

    rendered = render_deployment(
        base,
        _plan(tmp_path),
        {("web", "config", "./app.ini"): "./files/app.ini"},
    )

    web = rendered["services"]["web"]
    assert web["volumes"][0]["source"] == "./app.ini"
    assert rendered["configs"]["app-config"]["file"] == "./files/app.ini"
```

- [ ] **Step 6: Run render tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_render.py -v
```

Expected: FAIL because rendering still looks up rewrites using only the raw
source string.

- [ ] **Step 7: Implement scoped Compose rewrites**

Import `FileRewriteKey` from `docker_package_app.files` in `render.py` and
change signatures:

```python
def render_deployment(
    base: ComposeDocument,
    plan: PackagePlan,
    file_rewrites: Mapping[FileRewriteKey, str],
) -> dict[str, Any]:
```

Pass the service name:

```python
        _rewrite_service_volumes(service, config, file_rewrites)
```

Replace the config/secret rewrite block with:

```python
    for kind in ("config", "secret"):
        definitions = data.get(f"{kind}s")
        if not isinstance(definitions, dict):
            continue
        for definition in definitions.values():
            if not isinstance(definition, dict):
                continue
            source = definition.get("file")
            if not isinstance(source, str):
                continue
            destinations = {
                destination
                for (_service, rewrite_kind, original), destination
                in file_rewrites.items()
                if rewrite_kind == kind and original == source
            }
            if len(destinations) > 1:
                raise PackageError(
                    f"{kind} 文件 {source} 存在不一致的载荷改写"
                )
            if destinations:
                definition["file"] = destinations.pop()
```

Change `_rewrite_service_volumes()` to:

```python
def _rewrite_service_volumes(
    service: str,
    config: dict[str, Any],
    rewrites: Mapping[FileRewriteKey, str],
) -> None:
    volumes = config.get("volumes")
    if not isinstance(volumes, list):
        return
    for index, volume in enumerate(volumes):
        if isinstance(volume, dict):
            source = volume.get("source")
            if volume.get("type") == "bind" and isinstance(source, str):
                rewrite = rewrites.get((service, "bind", source))
                if rewrite is not None:
                    volume["source"] = rewrite
            continue
        if not isinstance(volume, str):
            continue
        source, separator, remainder = volume.partition(":")
        rewrite = rewrites.get((service, "bind", source))
        if separator and rewrite is not None:
            volumes[index] = f"{rewrite}:{remainder}"
```

- [ ] **Step 8: Run materialization and render tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_files.py \
  tests/unit/test_render.py \
  -v
```

Expected: all tests PASS.

- [ ] **Step 9: Commit identity-aware file handling**

Run:

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/files.py \
  skills/package-docker-app/scripts/docker_package_app/render.py \
  tests/unit/test_files.py \
  tests/unit/test_render.py
git commit -m "feat: preserve selected server bind paths"
```

Expected: one commit containing materialization, rendering, and their unit
regressions.

---

### Task 3: 串联 CLI 并验证重复解压不覆盖服务器数据

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py:431-436`
- Modify: `tests/e2e/test_package_flow.py:1-83`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/unit/test_artifact.py:60-72`
- Modify: `tests/fixtures/e2e-multi/compose.yaml`
- Create: `tests/fixtures/e2e-multi/data/local.db`

**Interfaces:**
- Consumes: `PackagePlan.files: tuple[FileAssignment, ...]`.
- Calls: `materialize_files(state.inspection.files, plan.files, payload)`.
- Produces: unchanged package JSON `server_paths: list[str]`.
- Produces: unchanged manifest `server_paths`, derived from `KEEP_SERVER_PATH`.

- [ ] **Step 1: Update model construction sites for the required kind**

In `tests/unit/test_artifact.py`, add this field to the existing
`FileAssignment`:

```python
                kind="bind",
```

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_artifact.py -v
```

Expected: PASS after the model call site is compatible.

- [ ] **Step 2: Add a data bind to the end-to-end fixture**

Append this volume to the `web` service in
`tests/fixtures/e2e-multi/compose.yaml`:

```yaml
      - type: bind
        source: ./data
        target: /app/data
```

Create `tests/fixtures/e2e-multi/data/local.db` with:

```text
developer-data-must-not-reach-server
```

Import `_file_question_id` in `tests/e2e/test_package_flow.py`:

```python
from docker_package_app.questions import _file_question_id
```

Add both explicit decisions to the first test's answer values after the project
has been copied:

```python
                    _file_question_id(str((project / "config").resolve())): "copy",
                    _file_question_id(str((project / "data").resolve())): (
                        "keep_server_path"
                    ),
```

- [ ] **Step 3: Extend the end-to-end assertions for archive and overwrite behavior**

In `test_multi_service_package_is_complete()`, add:

```python
    assert output["server_paths"] == [str((project / "data").resolve())]
```

Inside the archive assertion block add:

```python
        names = set(bundle.getnames())
        assert not any(
            name == "data"
            or name.startswith("data/")
            or name == "files/data"
            or name.startswith("files/data/")
            for name in names
        )
        volumes = compose["services"]["web"]["volumes"]
        assert volumes[0]["source"] == "./files/config"
        assert volumes[1]["source"] == "./data"
        manifest = json.load(bundle.extractfile("manifest.json"))
        assert manifest["server_paths"] == [str((project / "data").resolve())]
```

After closing the archive, simulate the user's manual same-directory extraction:

```python
    deployment = tmp_path / "server-deployment"
    (deployment / "data").mkdir(parents=True)
    (deployment / "data/server.db").write_text(
        "existing-test-data",
        encoding="utf-8",
    )
    (deployment / "files/config").mkdir(parents=True)
    (deployment / "files/config/app.ini").write_text(
        "old-config",
        encoding="utf-8",
    )

    with tarfile.open(archive_path, "r:gz") as bundle:
        bundle.extractall(deployment)

    assert (
        deployment / "data/server.db"
    ).read_text(encoding="utf-8") == "existing-test-data"
    assert not (deployment / "data/local.db").exists()
    assert (
        deployment / "files/config/app.ini"
    ).read_text(encoding="utf-8") != "old-config"
```

- [ ] **Step 4: Run the end-to-end test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/e2e/test_package_flow.py::test_multi_service_package_is_complete \
  -v
```

Expected: FAIL before the CLI wiring change because the package path still
passes a path-keyed decision mapping to `materialize_files()`.

- [ ] **Step 5: Wire the confirmed file assignments into packaging**

In `_perform_package()` in `cli.py`, remove:

```python
    decisions = {item.resolved_path: item.action for item in plan.files}
```

Replace the materialization call with:

```python
    materialized = materialize_files(
        state.inspection.files,
        plan.files,
        payload,
    )
```

No package-stage logic may reread the answer file or choose a new action.

- [ ] **Step 6: Run end-to-end and artifact tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_artifact.py \
  tests/e2e/test_package_flow.py::test_multi_service_package_is_complete \
  -v
```

Expected: all selected tests PASS; the server marker remains unchanged and the
copied config is replaced.

- [ ] **Step 7: Add an integration regression proving decisions affect the plan hash**

Add to `tests/integration/test_cli.py`:

```python
def test_bind_decision_is_required_and_changes_plan_hash(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "bind-plan"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "local.db").write_text("local", encoding="utf-8")
    (project / "compose.yaml").write_text(
        """services:
  app:
    image: busybox:1.36
    volumes:
      - ./data:/data
""",
        encoding="utf-8",
    )

    hashes: dict[str, str] = {}
    plans: dict[str, dict[str, object]] = {}
    question_id = _file_question_id(str(data.resolve()))
    for decision in ("copy", "keep_server_path"):
        inspected = cli("inspect", str(project), "--json")
        assert inspected.returncode == 0, inspected.stderr
        inspection = json.loads(inspected.stdout)
        question = next(
            item
            for item in inspection["questions"]
            if item["id"] == question_id
        )
        assert question["default"] == "copy"

        missing_answers = project / f"missing-{decision}.json"
        missing_answers.write_text(
            json.dumps(
                {"values": {"image.app.decision": "registry.intra/app:1"}}
            ),
            encoding="utf-8",
        )
        missing_answers.chmod(0o600)
        missing = cli(
            "plan",
            str(project),
            "--run-id",
            inspection["run_id"],
            "--answers",
            str(missing_answers),
            "--non-interactive",
            "--json",
        )
        assert missing.returncode == 10
        assert question_id in missing.stderr

        answers = project / f"answers-{decision}.json"
        answers.write_text(
            json.dumps(
                {
                    "values": {
                        "image.app.decision": "registry.intra/app:1",
                        question_id: decision,
                    }
                }
            ),
            encoding="utf-8",
        )
        answers.chmod(0o600)
        planned = cli(
            "plan",
            str(project),
            "--run-id",
            inspection["run_id"],
            "--answers",
            str(answers),
            "--non-interactive",
            "--json",
        )
        assert planned.returncode == 0, planned.stderr
        body = json.loads(planned.stdout)
        hashes[decision] = body["plan_hash"]
        plans[decision] = body["plan"]["files"][0]

    assert hashes["copy"] != hashes["keep_server_path"]
    assert plans["copy"]["action"] == "copy"
    assert plans["copy"]["payload_path"] == "files/data"
    assert plans["keep_server_path"]["action"] == "keep_server_path"
    assert plans["keep_server_path"]["payload_path"] is None
```

Add this import:

```python
from docker_package_app.questions import _file_question_id
```

- [ ] **Step 8: Run the integration regression**

```bash
uv run --project skills/package-docker-app pytest \
  tests/integration/test_cli.py::test_bind_decision_is_required_and_changes_plan_hash \
  -v
```

Expected: PASS, proving at the CLI boundary that non-interactive callers must
answer and the persisted file assignment changes the confirmation hash. The
underlying production behavior was developed from the failing unit tests in
Task 1; this step adds cross-component coverage rather than a new behavior.

- [ ] **Step 9: Run all file-workflow tests**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py \
  tests/unit/test_questions.py \
  tests/unit/test_planning.py \
  tests/unit/test_files.py \
  tests/unit/test_render.py \
  tests/unit/test_artifact.py \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py \
  -v
```

Expected: all tests PASS.

- [ ] **Step 10: Commit the CLI and workflow regression**

Run:

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_artifact.py \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py \
  tests/fixtures/e2e-multi/compose.yaml \
  tests/fixtures/e2e-multi/data/local.db
git commit -m "test: prove preserved binds survive redeploy"
```

Expected: one commit containing the CLI wiring and full workflow regression.

---

### Task 4: 更新 skill 契约并完成全量验证

**Files:**
- Modify: `skills/package-docker-app/SKILL.md:21-31, 65-80`
- Modify: `tests/unit/test_skill_contract.py`
- Modify: `docs/superpowers/specs/2026-07-29-bind-mount-server-data-preservation-design.md`

**Interfaces:**
- Consumes: the completed CLI behavior from Tasks 1-3.
- Produces: a user-facing Chinese contract requiring explicit bind decisions and complete plan disclosure.
- Produces: no runtime API changes.

- [ ] **Step 1: Write the failing skill contract test**

Add to `tests/unit/test_skill_contract.py`:

```python
def test_skill_requires_explicit_bind_copy_or_server_preservation(
    skill_text: str,
) -> None:
    required = (
        "每个 bind mount",
        "`copy`",
        "`keep_server_path`",
        "`abort`",
        "不进入归档",
        "保留原始挂载路径",
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

Expected: FAIL because `SKILL.md` does not yet define per-bind decisions or
repeat-extraction behavior.

- [ ] **Step 3: Update the skill invariants and workflow**

Add these invariants after the existing bind permission rule in `SKILL.md`:

```markdown
- 每个 bind mount 都必须在计划前明确决定：项目内路径可选 `copy`（复制本机内容）、`keep_server_path`（保留服务器现有路径）或 `abort`（中止）；项目外路径只允许 `keep_server_path` 或 `abort`。项目内 bind 默认值为 `copy`，但答案文件仍必须包含最终决定。
- 选择 `keep_server_path` 时，本机 source 及其内容不得进入归档，部署 Compose 必须保留原始挂载路径；CLI 不得创建、清空、修改该服务器路径或改变其权限。普通覆盖式重复解压不会覆盖归档中不存在的保留路径。
```

Extend workflow Step 3 with:

```markdown
对每个 bind mount 显示使用服务、原始 source、解析路径、项目内外位置、估算大小、完整默认值和中文选项含义。项目内路径接受 `copy`、`keep_server_path`、`abort`；项目外路径接受 `keep_server_path`、`abort`。不得根据 `data`、`uploads` 等目录名自动猜测。
```

Keep workflow Step 7's complete plan requirement and ensure its file clause is:

```markdown
复制文件和保留的服务器路径；选择 `keep_server_path` 的路径必须明确标记为不进入归档并保留原始 Compose source。
```

- [ ] **Step 4: Run skill contract tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Review the design clarification and implementation diff**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; only the intended skill contract, test, and
already-reviewed design clarification remain uncommitted.

- [ ] **Step 6: Run formatting checks**

Run:

```bash
uv run --project skills/package-docker-app ruff format --check \
  skills/package-docker-app/scripts tests
```

Expected: exit 0 with no files requiring formatting.

Run:

```bash
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
```

Expected: exit 0 with no diagnostics.

- [ ] **Step 7: Run the complete automated suite**

Run:

```bash
uv run --project skills/package-docker-app pytest -v
```

Expected: all non-environmental tests PASS; Docker smoke tests remain skipped
unless `RUN_DOCKER_SMOKE=1`.

- [ ] **Step 8: Validate the skill structure**

Run:

```bash
uv run --project skills/package-docker-app python \
  /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
```

Expected: validation succeeds with no frontmatter, naming, or structure errors.

- [ ] **Step 9: Commit the contract and clarified design**

Run:

```bash
git add \
  skills/package-docker-app/SKILL.md \
  tests/unit/test_skill_contract.py \
  docs/superpowers/specs/2026-07-29-bind-mount-server-data-preservation-design.md
git commit -m "docs: define bind mount preservation workflow"
```

Expected: one documentation and contract-test commit.

- [ ] **Step 10: Perform final verification after all commits**

Run:

```bash
uv run --project skills/package-docker-app pytest -q
git diff --check
git status --short
```

Expected: pytest exits 0, `git diff --check` prints nothing, and the worktree is
clean.
