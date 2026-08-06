# Compose Bind Resolution and Server Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `docker-package-app` 使用最终环境答案解析 Compose bind source，并让 Docker Manage Server 的宿主机端口可通过环境变量配置，从而生成使用 `/data/docker-manage-server` 和 `6308:8000/tcp` 的正确离线包。

**Architecture:** Inspect 保留未插值 Compose 以发现变量和原始表达式；plan 使用最终答案再次调用 Docker Compose，得到文件依赖的有效路径，并把有效路径写入计划。Package 依据计划中的有效路径决定复制或保留服务器路径，同时在部署 Compose 中保留原始变量表达式。Server 继续让宿主机和容器共享同一个绝对数据路径，只把宿主机发布端口改为可配置值。

**Tech Stack:** Python 3.11、pytest、Pydantic、Docker Compose CLI、PyYAML、Docker Manage 打包 CLI。

## Global Constraints

- `docker_manage_server` 的宿主机 bind source、容器 target 和容器内 `DATA_DIR` 必须是同一个绝对路径。
- 本次服务器数据路径为 `/data/docker-manage-server`。
- 本次端口映射为宿主机 `6308` 到容器 `8000/tcp`。
- 目标镜像平台为 `linux/amd64`。
- 项目外 bind 只允许 `keep_server_path` 或 `abort`；其内容不得进入归档。
- 项目内 `keep_server_path` 继续使用 `./files/` 下对应的稳定项目相对路径，且内容不得进入归档。
- 任何变量解析、路径匹配或计划校验错误必须发生在 Docker build、pull、save 之前。
- 部署 Compose、manifest 和最终结果不得包含开发电脑绝对路径。
- 保留现有问题 JSON、答案 JSON、`PackagePlan` 和 manifest schema 版本。

---

### Task 1: 让 ComposeDocument 支持使用显式环境进行插值

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/compose.py`
- Modify: `tests/unit/test_compose.py`
- Modify: `tests/integration/test_real_compose.py`

**Interfaces:**
- `ComposeDocument.load(..., environment: Mapping[str, str] | None = None, interpolate: bool = False) -> ComposeDocument`
- 默认 `interpolate=False`，现有 inspect 与 package 行为保持不变。
- `interpolate=True` 时移除 `--no-interpolate`，并把 `environment` 传给 `CommandRunner.run(..., env=environment)`。

- [ ] **Step 1: 写失败的单元测试**

在 `tests/unit/test_compose.py` 的 `ComposeRunner` 增加 `kwargs` 记录，并添加：

```python
class ComposeRunner:
    def __init__(self, document: dict) -> None:
        self.document = document
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def run(self, argv, **kwargs):
        call = list(argv)
        self.calls.append(call)
        self.kwargs.append(kwargs)
        return CommandResult(call, 0, json.dumps(self.document), "")


def test_compose_can_interpolate_with_explicit_environment(tmp_path: Path) -> None:
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {web: {image: demo:1}}\n", encoding="utf-8")
    runner = ComposeRunner(
        {
            "services": {
                "web": {
                    "image": "demo:1",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/data/docker-manage-server",
                            "target": "/data/docker-manage-server",
                        }
                    ],
                }
            }
        }
    )
    environment = {
        "DOCKER_MANAGE_DATA_DIR": "/data/docker-manage-server",
        "PWD": str(tmp_path),
    }

    document = ComposeDocument.load(
        tmp_path,
        [compose_path],
        [],
        runner,
        environment=environment,
        interpolate=True,
    )

    assert document.service("web")["volumes"][0]["source"] == (
        "/data/docker-manage-server"
    )
    assert "--no-interpolate" not in runner.calls[0]
    assert "--no-path-resolution" in runner.calls[0]
    assert runner.kwargs[0]["env"] == environment
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```bash
uv run pytest tests/unit/test_compose.py::test_compose_can_interpolate_with_explicit_environment -q
```

Expected: FAIL，原因是 `ComposeDocument.load()` 尚不接受 `environment` 和 `interpolate`。

- [ ] **Step 3: 实现最小 Compose 加载改动**

在 `compose.py` 中把导入和 `load` 签名改为：

```python
from collections.abc import Mapping, Sequence


@classmethod
def load(
    cls,
    project_root: Path,
    files: Sequence[Path],
    profiles: Sequence[str],
    runner: CommandRunner,
    *,
    environment: Mapping[str, str] | None = None,
    interpolate: bool = False,
) -> ComposeDocument:
    root = project_root.resolve()
    argv = ["docker", "compose", "--project-directory", str(root)]
    resolved_files = [Path(path).resolve() for path in files]
    for path in resolved_files:
        argv.extend(["-f", str(path)])
    for profile in profiles:
        argv.extend(["--profile", profile])
    argv.extend(["config", "--format", "json"])
    if not interpolate:
        argv.append("--no-interpolate")
    argv.append("--no-path-resolution")
    result = runner.run(argv, cwd=root, env=environment)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UsageError(
            "Docker Compose 返回了无效的 JSON",
            details=result.stdout,
        ) from exc
    if not isinstance(data, dict):
        raise UsageError("Docker Compose 配置必须是一个对象")
    return cls(root, data, resolved_files)
```

- [ ] **Step 4: 添加真实 Compose 插值测试**

在 `tests/integration/test_real_compose.py` 添加：

```python
@pytest.mark.skipif(
    not _has_docker_compose(),
    reason="Docker Compose is required for interpolation validation",
)
def test_real_compose_interpolates_bind_source_from_explicit_environment(
    tmp_path: Path,
) -> None:
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(
        "x-data-dir: &data-dir ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}\n"
        "services:\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        "    volumes:\n"
        "      - type: bind\n"
        "        source: *data-dir\n"
        "        target: *data-dir\n",
        encoding="utf-8",
    )

    document = ComposeDocument.load(
        tmp_path,
        [compose_path],
        [],
        CommandRunner(),
        environment={
            "DOCKER_MANAGE_DATA_DIR": "/data/docker-manage-server",
            "PWD": str(tmp_path),
        },
        interpolate=True,
    )

    volume = document.service("app")["volumes"][0]
    assert volume["source"] == "/data/docker-manage-server"
    assert volume["target"] == "/data/docker-manage-server"
```

- [ ] **Step 5: 运行 Task 1 测试**

Run:

```bash
uv run pytest tests/unit/test_compose.py tests/integration/test_real_compose.py -q
```

Expected: PASS；有 Docker Compose 时真实测试通过，否则只以明确原因跳过。

- [ ] **Step 6: 提交 Task 1**

```bash
git add skills/package-docker-app/scripts/docker_package_app/compose.py tests/unit/test_compose.py tests/integration/test_real_compose.py
git commit -m "feat: support resolved compose loading"
```

---

### Task 2: 用最终环境答案解析文件依赖并物化计划路径

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/files.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/planning.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/artifact.py`
- Modify: `tests/unit/test_files.py`
- Modify: `tests/unit/test_planning.py`
- Modify: `tests/unit/test_artifact.py`
- Modify: `tests/conftest.py`
- Modify: `tests/integration/fake_docker.py`
- Modify: `tests/integration/test_cli.py`

**Interfaces:**
- `discover_file_dependencies(..., resolved_compose: ComposeDocument | None = None) -> tuple[FileCandidate, ...]`
- `compose_file_environment(inspection: Inspection, answers: AnswerBook) -> dict[str, str]`
- `build_plan(..., resolved_files: Sequence[FileCandidate] | None = None) -> PackagePlan`
- `required_server_path(project_root: Path, resolved_path: str, original_value: str) -> str`
- 原始 `compose_value` 用于问题 ID 关联与部署 Compose rewrite key；最终 `resolved_path` 用于位置、安全和文件动作判定。

- [ ] **Step 1: 写文件发现与物化的失败测试**

在 `tests/unit/test_files.py` 添加：

```python
def test_resolved_compose_value_controls_bind_location(tmp_path: Path) -> None:
    raw_source = "${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}"
    raw = _compose(tmp_path, raw_source)
    resolved = _compose(tmp_path, "/data/docker-manage-server")

    candidate = next(
        item
        for item in discover_file_dependencies(
            raw,
            tmp_path,
            resolved_compose=resolved,
        )
        if item.kind == "bind"
    )

    assert candidate.compose_value == raw_source
    assert candidate.resolved_path == "/data/docker-manage-server"
    assert candidate.inside_project is False


def test_materialize_uses_assignment_resolved_external_path(
    tmp_path: Path,
) -> None:
    raw_source = "${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}"
    candidate = next(
        item
        for item in discover_file_dependencies(
            _compose(tmp_path, raw_source),
            tmp_path,
        )
        if item.kind == "bind"
    )
    assignment = FileAssignment(
        service=candidate.service,
        original_value=raw_source,
        resolved_path="/data/docker-manage-server",
        kind="bind",
        action=FileAction.KEEP_SERVER_PATH,
    )

    result = materialize_files(
        (candidate,),
        (assignment,),
        tmp_path / "payload",
        tmp_path,
    )

    assert result.rewrites == {}
    assert result.server_paths == ("/data/docker-manage-server",)
    assert not (tmp_path / "payload/files").exists()


def test_resolved_compose_requires_matching_volume_entries(tmp_path: Path) -> None:
    raw = _compose(tmp_path, "${DATA_DIR:-${PWD}/data}")
    resolved = ComposeDocument.from_data(
        tmp_path,
        {"services": {"web": {"image": "demo:1", "volumes": []}}},
    )

    with pytest.raises(PackageError, match="volume 无法一一对应"):
        discover_file_dependencies(
            raw,
            tmp_path,
            resolved_compose=resolved,
        )
```

同时从 `docker_package_app.errors` 导入 `PackageError`。

- [ ] **Step 2: 写计划与 manifest 的失败测试**

在 `tests/unit/test_planning.py` 添加：

```python
def test_resolved_external_bind_path_overrides_inspection_location(
    tmp_path: Path,
) -> None:
    inspection = _inspection(tmp_path)
    original = inspection.files[0]
    resolved = original.model_copy(
        update={
            "compose_value": original.compose_value,
            "resolved_path": "/data/docker-manage-server",
            "inside_project": False,
            "project_path": None,
            "estimated_size": 0,
        }
    )

    plan = build_plan(
        inspection,
        _answers(tmp_path, file_decision="keep_server_path"),
        app_name="demo",
        version="v1",
        platform="linux/amd64",
        resolved_files=(resolved,),
    )

    assignment = plan.files[0]
    assert assignment.original_value == "./config"
    assert assignment.resolved_path == "/data/docker-manage-server"
    assert assignment.action is FileAction.KEEP_SERVER_PATH
    assert assignment.payload_path is None


def test_compose_file_environment_rejects_conflicting_referenced_values(
    tmp_path: Path,
) -> None:
    inspection = _inspection(tmp_path)
    variable_file = inspection.files[0].model_copy(
        update={"compose_value": "${PORT}/config"}
    )
    inspection = inspection.model_copy(update={"files": (variable_file,)})

    with pytest.raises(
        PlanValidationError,
        match="Compose 插值变量 PORT.*最终值不一致",
    ):
        compose_file_environment(inspection, _answers(tmp_path))
```

同时从 `docker_package_app.planning` 导入 `compose_file_environment`。

在 `tests/unit/test_artifact.py` 添加：

```python
def test_manifest_records_resolved_path_for_interpolated_external_bind(
    tmp_path: Path,
) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    plan = _plan(tmp_path).model_copy(
        update={
            "files": (
                FileAssignment(
                    service="web",
                    original_value="${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}",
                    resolved_path="/data/docker-manage-server",
                    kind="bind",
                    action=FileAction.KEEP_SERVER_PATH,
                ),
            )
        }
    )
    metadata = ImageMetadata(
        reference="docker-manage/demo/web:v1",
        image_id="sha256:abc",
        repo_digests=(),
        os="linux",
        architecture="amd64",
        variant=None,
        size=123,
    )

    manifest = build_manifest(plan, [metadata], payload_root)

    assert manifest.server_paths == ("/data/docker-manage-server",)
```

- [ ] **Step 3: 运行失败测试**

Run:

```bash
uv run pytest tests/unit/test_files.py::test_resolved_compose_value_controls_bind_location tests/unit/test_files.py::test_materialize_uses_assignment_resolved_external_path tests/unit/test_files.py::test_resolved_compose_requires_matching_volume_entries tests/unit/test_planning.py::test_resolved_external_bind_path_overrides_inspection_location tests/unit/test_planning.py::test_compose_file_environment_rejects_conflicting_referenced_values tests/unit/test_artifact.py::test_manifest_records_resolved_path_for_interpolated_external_bind -q
```

Expected: FAIL，分别因为缺少 `resolved_compose`、有效计划文件和实际服务器路径支持。

- [ ] **Step 4: 实现原始与最终文件依赖配对**

在 `files.py` 中让发现函数接收已插值 Compose，并让 `_candidate` 使用独立的有效路径：

```python
def discover_file_dependencies(
    compose: ComposeDocument,
    project_root: Path,
    *,
    resolved_compose: ComposeDocument | None = None,
) -> tuple[FileCandidate, ...]:
    project = project_root.resolve()
    final = resolved_compose or compose
    found: list[FileCandidate] = []

    if final.services() != compose.services():
        raise PackageError("Compose 插值前后的服务集合不一致")

    for service in compose.services():
        raw_config = compose.service(service)
        final_config = final.service(service)
        raw_volumes = raw_config.get("volumes", ())
        final_volumes = final_config.get("volumes", ())
        if isinstance(raw_volumes, list):
            if not isinstance(final_volumes, list) or len(final_volumes) != len(raw_volumes):
                raise PackageError(f"服务 {service} 的 Compose volume 无法一一对应")
            for index, raw_volume in enumerate(raw_volumes):
                raw_source = _bind_source(raw_volume)
                if raw_source is None:
                    continue
                final_source = _bind_source(final_volumes[index])
                if final_source is None:
                    raise PackageError(
                        f"服务 {service} 的 bind source 插值结果无效：{raw_source}"
                    )
                found.append(
                    _candidate(
                        project,
                        service,
                        raw_source,
                        "bind",
                        effective_path=final_source,
                    )
                )

        for kind in ("config", "secret"):
            references = raw_config.get(f"{kind}s", ())
            if not isinstance(references, list):
                continue
            raw_definitions = compose.data.get(f"{kind}s", {})
            final_definitions = final.data.get(f"{kind}s", {})
            if not isinstance(raw_definitions, dict) or not isinstance(final_definitions, dict):
                continue
            for reference in references:
                name = reference.get("source") if isinstance(reference, dict) else reference
                raw_definition = raw_definitions.get(name) if isinstance(name, str) else None
                final_definition = final_definitions.get(name) if isinstance(name, str) else None
                raw_source = (
                    raw_definition.get("file")
                    if isinstance(raw_definition, dict)
                    else None
                )
                final_source = (
                    final_definition.get("file")
                    if isinstance(final_definition, dict)
                    else None
                )
                if isinstance(raw_source, str):
                    if not isinstance(final_source, str):
                        raise PackageError(
                            f"{kind} {name} 的文件路径插值结果无效"
                        )
                    found.append(
                        _candidate(
                            project,
                            service,
                            raw_source,
                            kind,
                            effective_path=final_source,
                        )
                    )

    unique: dict[tuple[str, str, str, str], FileCandidate] = {}
    for item in found:
        unique[(item.service, item.kind, item.compose_value, item.resolved_path)] = item
    return tuple(unique[key] for key in sorted(unique))


def _candidate(
    project_root: Path,
    service: str,
    raw_path: str,
    kind: str,
    *,
    effective_path: str | None = None,
) -> FileCandidate:
    source = Path(effective_path or raw_path).expanduser()
    if not source.is_absolute():
        source = project_root / source
    resolved = source.resolve()
    inside = resolved.is_relative_to(project_root)
    project_path = None
    if inside:
        relative = resolved.relative_to(project_root)
        project_path = relative.as_posix() if relative.parts else "project"
    return FileCandidate(
        service=service,
        compose_value=raw_path,
        resolved_path=str(resolved),
        kind=kind,
        inside_project=inside,
        project_path=project_path,
        estimated_size=_path_size(resolved),
    )
```

- [ ] **Step 5: 实现最终环境与计划文件解析**

在 `planning.py` 添加：

```python
COMPOSE_VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def compose_file_environment(
    inspection: Inspection,
    answers: AnswerBook,
) -> dict[str, str]:
    referenced = {
        match.group(1)
        for item in inspection.files
        for match in COMPOSE_VARIABLE_PATTERN.finditer(item.compose_value)
    }
    environment = {"PWD": inspection.project_root}
    for name in sorted(referenced - {"PWD"}):
        values = {
            answers.values[f"env.{item.service}.{item.name}"]
            for item in inspection.env
            if item.name == name
        }
        if len(values) > 1:
            raise PlanValidationError(
                f"Compose 插值变量 {name} 在多个服务中的最终值不一致"
            )
        if values:
            environment[name] = values.pop()
    return environment
```

把 `build_plan` 增加 `resolved_files` 关键字参数，并让 `_build_files` 用原始候选的问题
ID、最终候选的位置：

```python
def build_plan(
    inspection: Inspection,
    answers: AnswerBook,
    *,
    app_name: str,
    version: str,
    platform: str,
    resolved_files: Sequence[FileCandidate] | None = None,
) -> PackagePlan:
    _validate_identity(app_name, version, platform)
    questions = {item.id: item for item in build_questions(inspection)}
    for question_id, question in questions.items():
        if question.kind == "port_host":
            expose_id = f"{question_id.removesuffix('.host')}.expose"
            if answers.values.get(expose_id) == "no":
                continue
        if question_id not in answers.values:
            raise AnswerRequired(f"缺少答案：{question_id}")

    effective_files = tuple(resolved_files or inspection.files)
    environment = _build_environment(inspection, answers)
    build_args = tuple(
        BuildArgAssignment(
            service=item.service,
            name=item.name,
            value=answers.values[f"buildarg.{item.service}.{item.name}"],
        )
        for item in sorted(
            inspection.build_args,
            key=lambda value: (value.service, value.name),
        )
    )
    ports = _build_ports(inspection, answers)
    images = _build_images(inspection, answers, app_name, version, platform)
    files = _build_files(inspection, answers, effective_files)
    copied_input_bytes = _copied_input_bytes(effective_files, files)
    unknown = tuple(
        sorted(
            item.final_image
            for item in images
            if item.action in {ImageAction.BUILD, ImageAction.PACKAGE}
        )
    )
    return PackagePlan(
        run_id=inspection.run_id,
        project_root=inspection.project_root,
        app_name=app_name,
        version=version,
        compose_project_name=app_name,
        platform=platform,
        environment=environment,
        ports=ports,
        images=images,
        files=files,
        build_args=build_args,
        disk=DiskEstimate(
            known_input_bytes=copied_input_bytes,
            free_bytes=inspection.free_disk_bytes,
            unknown_components=unknown,
        ),
    )


def _file_identity(item: FileCandidate) -> tuple[str, str, str]:
    return item.service, item.kind, item.compose_value


def _build_files(
    inspection: Inspection,
    answers: AnswerBook,
    resolved_files: Sequence[FileCandidate],
) -> tuple[FileAssignment, ...]:
    root = Path(inspection.project_root).resolve()
    effective = {_file_identity(item): item for item in resolved_files}
    originals = {_file_identity(item): item for item in inspection.files}
    if effective.keys() != originals.keys():
        raise PlanValidationError("Compose 插值前后的文件依赖无法一一对应")

    assignments: list[FileAssignment] = []
    for identity in sorted(originals):
        original = originals[identity]
        item = effective[identity]
        path = Path(item.resolved_path).resolve()
        if original.kind == "bind" or not original.inside_project:
            decision = answers.values[
                _file_question_id(str(Path(original.resolved_path).resolve()))
            ]
            if decision == "abort":
                raise PlanValidationError(f"已因路径 {path} 中止打包")
            action = FileAction(decision)
        else:
            action = FileAction.COPY

        if action is FileAction.COPY and not path.is_relative_to(root):
            raise PlanValidationError(f"无法复制项目目录之外的路径：{path}")

        payload_path = None
        if action is FileAction.COPY:
            relative = path.relative_to(root)
            project_path = relative if relative.parts else Path("project")
            payload_path = (Path("files") / project_path).as_posix()
        assignments.append(
            FileAssignment(
                service=original.service,
                original_value=original.compose_value,
                resolved_path=str(path),
                kind=original.kind,
                action=action,
                payload_path=payload_path,
            )
        )
    return tuple(assignments)
```

- [ ] **Step 6: 在 CLI plan 阶段加载已插值 Compose**

在 `cli.py` 从 `planning` 导入 `compose_file_environment`，并在 `_perform_plan` 中用
同一组 Compose 文件分别加载原始和已插值文档：

```python
from docker_package_app.planning import build_plan, compose_file_environment


def _perform_plan(args: argparse.Namespace, paths: WorkPaths) -> dict[str, Any]:
    state = load_model(paths.state, RunState)
    if state.stage is not Stage.INSPECTED or state.inspection is None:
        raise UsageError(f"运行 {state.run_id} 尚未准备好进行规划")
    questions = build_questions(state.inspection)
    provided = _load_answers(args.answers)
    answers = _resolve_answers(
        questions,
        provided,
        non_interactive=args.non_interactive or args.json,
    )
    runner = CommandRunner()
    compose_files = [
        _resolve_project_path(paths.project_root, value)
        for value in state.inspection.compose_files
    ]
    raw_compose = ComposeDocument.load(
        paths.project_root,
        compose_files,
        state.inspection.profiles,
        runner,
    )
    resolved_compose = ComposeDocument.load(
        paths.project_root,
        compose_files,
        state.inspection.profiles,
        runner,
        environment=compose_file_environment(state.inspection, answers),
        interpolate=True,
    )
    resolved_files = discover_file_dependencies(
        raw_compose,
        paths.project_root,
        resolved_compose=resolved_compose,
    )
    default_app, default_version = default_identity(paths.project_root, runner)
    plan = build_plan(
        state.inspection,
        answers,
        app_name=args.app_name or default_app,
        version=args.version or default_version,
        platform=args.platform,
        resolved_files=resolved_files,
    )
    plan_hash = _plan_hash(plan)
    updated = _transition(
        state,
        Stage.PLANNED,
        plan=plan,
        plan_hash=plan_hash,
    )
    atomic_write_model(paths.state, updated)
    return {
        "stage": Stage.PLANNED.value,
        "run_id": state.run_id,
        "plan_hash": plan_hash,
        "plan": plan.model_dump(mode="json"),
    }
```

- [ ] **Step 7: 让文件物化与 manifest 使用最终路径**

在 `files.py` 把 identity 改为不包含旧解析路径：

```python
FileIdentity = tuple[str, str, str]


def _candidate_identity(candidate: FileCandidate) -> FileIdentity:
    return candidate.service, candidate.kind, candidate.compose_value


def _assignment_identity(assignment: FileAssignment) -> FileIdentity:
    return assignment.service, assignment.kind, assignment.original_value
```

在 `materialize_files` 循环中以 assignment 的路径判断位置，并添加服务器路径辅助函数：

```python
def required_server_path(
    project_root: Path,
    resolved_path: str,
    original_value: str,
) -> str:
    root = project_root.resolve()
    resolved = Path(resolved_path).resolve()
    if resolved.is_relative_to(root):
        return deployment_source(root, resolved_path, original_value)
    if "${" in original_value:
        return str(resolved)
    return original_value
```

将 `materialize_files` 中 `for candidate ...` 的动作主体替换为：

```python
    for candidate in sorted(candidates, key=lambda item: item.resolved_path):
        assignment = assignment_by_identity.get(_candidate_identity(candidate))
        if assignment is None:
            raise AnswerRequired(
                "文件依赖缺少已确认决定："
                f"{candidate.service}/{candidate.kind}/{candidate.compose_value}"
            )
        action = assignment.action
        source = Path(assignment.resolved_path).resolve()
        inside_project = source.is_relative_to(project)

        if action is FileAction.KEEP_SERVER_PATH:
            server_source = required_server_path(
                project,
                assignment.resolved_path,
                assignment.original_value,
            )
            server_paths.add(server_source)
            if candidate.kind == "bind" and inside_project:
                rewrites[_rewrite_key(candidate)] = server_source
            continue

        if not inside_project or not assignment.payload_path:
            raise PackageError(
                f"无法复制项目目录之外的路径：{assignment.resolved_path}"
            )
        if not source.exists():
            raise PackageError(f"本地 Compose 依赖不存在：{source}")
        destination = payload / assignment.payload_path
        if not destination.resolve(strict=False).is_relative_to(files_root):
            raise PackageError(f"制品载荷路径不安全：{assignment.payload_path}")
        files_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if assignment.resolved_path not in copied_sources:
            _copy_dependency(source, destination)
            copied_sources.add(assignment.resolved_path)
            copied_bytes += _path_size(source)
        if candidate.kind == "bind":
            bind_destinations.add(destination)
        rewrites[_rewrite_key(candidate)] = deployment_source(
            project,
            assignment.resolved_path,
            assignment.original_value,
        )
```

在 `artifact.py` 用 `required_server_path` 替换原来的 `deployment_source` 导入与调用：

```python
from docker_package_app.files import required_server_path

# build_manifest(...)
server_paths=tuple(
    sorted(
        {
            required_server_path(
                Path(plan.project_root),
                item.resolved_path,
                item.original_value,
            )
            for item in plan.files
            if item.action is FileAction.KEEP_SERVER_PATH
        }
    )
),
```

- [ ] **Step 8: 添加 CLI 回归测试**

在 `tests/integration/test_cli.py` 添加一个使用 fake Compose 两次返回值的测试前，扩展
`CliRunner` 与 `fake_docker.py`：当命令包含 `--no-interpolate` 时返回
`FAKE_DOCKER_COMPOSE_CONFIG`，否则优先返回
`FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG`。然后添加：

在 `tests/conftest.py` 清理可选 fake 变量：

```python
for name in (
    "FAKE_DOCKER_EXIT",
    "FAKE_DOCKER_STDERR",
    "FAKE_DOCKER_INSPECT",
    "FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG",
):
    self.base_env.pop(name, None)
```

在 `tests/integration/fake_docker.py` 替换 Compose config 分支：

```python
elif args and args[0] == "compose" and "config" in args:
    variable = (
        "FAKE_DOCKER_COMPOSE_CONFIG"
        if "--no-interpolate" in args
        else "FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG"
    )
    fallback = os.environ.get("FAKE_DOCKER_COMPOSE_CONFIG", '{"services": {}}')
    print(os.environ.get(variable, fallback))
```

在 `tests/integration/test_cli.py` 添加：

```python
def test_plan_resolves_interpolated_bind_from_final_environment(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "interpolated-bind"
    project.mkdir()
    (project / "compose.yaml").write_text(
        """services:
  app:
    image: busybox:1.36
    environment:
      DATA_DIR: ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
    volumes:
      - type: bind
        source: ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
        target: ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
""",
        encoding="utf-8",
    )
    inspected = cli("inspect", str(project), "--json")
    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    file_question = next(
        item for item in body["questions"] if item["kind"] == "file"
    )
    answers = project / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.app.DATA_DIR": "/data/docker-manage-server",
                    "env.app.DOCKER_MANAGE_DATA_DIR": "/data/docker-manage-server",
                    "image.app.decision": "registry.intra/busybox:1.36",
                    file_question["id"]: "keep_server_path",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)
    resolved = {
        "services": {
            "app": {
                "image": "busybox:1.36",
                "environment": {"DATA_DIR": "/data/docker-manage-server"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/data/docker-manage-server",
                        "target": "/data/docker-manage-server",
                    }
                ],
            }
        }
    }

    planned = cli(
        "plan",
        str(project),
        "--run-id",
        body["run_id"],
        "--answers",
        str(answers),
        "--non-interactive",
        "--json",
        env={"FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG": json.dumps(resolved)},
    )

    assert planned.returncode == 0, planned.stderr
    file_plan = json.loads(planned.stdout)["plan"]["files"][0]
    assert file_plan["resolved_path"] == "/data/docker-manage-server"
    assert file_plan["action"] == "keep_server_path"
    assert file_plan["payload_path"] is None
```

- [ ] **Step 9: 运行 Task 2 定向测试**

Run:

```bash
uv run pytest tests/unit/test_files.py tests/unit/test_planning.py tests/unit/test_artifact.py tests/integration/test_cli.py -q
```

Expected: PASS，且无 warning 或 error。

- [ ] **Step 10: 运行打包器完整测试**

Run:

```bash
uv run pytest -q
```

Expected: 全部 PASS；依赖真实 Docker 的测试只允许以既有条件明确跳过。

- [ ] **Step 11: 提交 Task 2**

```bash
git add skills/package-docker-app/scripts/docker_package_app/files.py skills/package-docker-app/scripts/docker_package_app/planning.py skills/package-docker-app/scripts/docker_package_app/cli.py skills/package-docker-app/scripts/docker_package_app/artifact.py tests/conftest.py tests/integration/fake_docker.py tests/integration/test_cli.py tests/unit/test_files.py tests/unit/test_planning.py tests/unit/test_artifact.py
git commit -m "fix: resolve bind paths from final env"
```

---

### Task 3: 配置 Docker Manage Server 宿主机端口

**Files:**
- Modify: `/Users/wenjuhao/code/python/docker_manage_server/compose.yaml`
- Modify: `/Users/wenjuhao/code/python/docker_manage_server/tests/integration/test_compose_mount.py`
- Modify: `/Users/wenjuhao/code/python/docker_manage_server/README.md`

**Interfaces:**
- `DOCKER_MANAGE_SERVER_PORT` 控制宿主机发布端口，默认 `8000`。
- 容器端口固定为 `8000/tcp`。
- `DOCKER_MANAGE_DATA_DIR` 继续同时作为 host source、container target 和 `DATA_DIR`。

- [ ] **Step 1: 扩展 server Compose 失败测试**

把 `tests/integration/test_compose_mount.py` 的测试改为：

```python
@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_compose_uses_one_absolute_data_path_and_configurable_host_port(
    tmp_path,
):
    data_dir = (tmp_path / "docker-manage-data").resolve()
    environment = os.environ.copy()
    environment["DOCKER_MANAGE_DATA_DIR"] = str(data_dir)
    environment["DOCKER_MANAGE_SERVER_PORT"] = "6308"

    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    server = config["services"]["server"]
    assert server["environment"]["DATA_DIR"] == str(data_dir)
    data_mounts = [
        mount
        for mount in server["volumes"]
        if mount.get("target") == str(data_dir)
    ]
    assert data_mounts == [
        {"type": "bind", "source": str(data_dir), "target": str(data_dir)}
    ]
    assert server["ports"] == [
        {
            "mode": "ingress",
            "target": 8000,
            "published": "6308",
            "protocol": "tcp",
        }
    ]
```

- [ ] **Step 2: 运行测试并确认端口断言失败**

Run:

```bash
uv run pytest tests/integration/test_compose_mount.py -q
```

Expected: FAIL，实际 published 仍为 `8000`。

- [ ] **Step 3: 修改 Compose 端口声明**

在 `compose.yaml` 中仅替换端口行：

```yaml
ports:
  - "${DOCKER_MANAGE_SERVER_PORT:-8000}:8000"
```

数据目录 anchor、environment 和 volume 不改。

- [ ] **Step 4: 更新 README 启动示例**

把服务器示例改为：

```bash
export DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
export DOCKER_MANAGE_SERVER_PORT=6308
mkdir -p "$DOCKER_MANAGE_DATA_DIR"
docker compose up --build -d
curl --fail --retry 10 --retry-all-errors --retry-delay 1 \
  "http://localhost:${DOCKER_MANAGE_SERVER_PORT}/api/health"
```

并明确：`DOCKER_MANAGE_SERVER_PORT` 只控制宿主机端口，容器内服务仍监听
`8000/tcp`，默认宿主机端口为 `8000`。

- [ ] **Step 5: 运行 server 定向测试**

Run:

```bash
uv run pytest tests/integration/test_compose_mount.py -q
```

Expected: PASS。

- [ ] **Step 6: 运行 server 完整测试**

Run:

```bash
uv run pytest -q
```

Expected: 全部 PASS；Docker 不可用时仅相关测试按既有条件跳过。

- [ ] **Step 7: 提交 Task 3**

```bash
git add compose.yaml README.md tests/integration/test_compose_mount.py
git commit -m "feat: configure server host port"
```

---

### Task 4: 交叉验证并重新生成离线包

**Files:**
- Verify: `/Users/wenjuhao/code/python/docker_manage`
- Verify: `/Users/wenjuhao/code/python/docker_manage_server`
- Generate: `/Users/wenjuhao/code/python/docker_manage_server/.docker-manage/dist/` 下由新 server Git 版本命名的 `.tar.gz` 归档

**Interfaces:**
- 使用 `package-docker-app` 技能规定的同一 `run_id`、答案文件和 `plan_hash` 流程。
- 打包前必须展示完整计划并再次取得用户明确确认。

- [ ] **Step 1: 运行两个项目的完整验证**

Run in `/Users/wenjuhao/code/python/docker_manage`:

```bash
uv run pytest -q
```

Run in `/Users/wenjuhao/code/python/docker_manage_server`:

```bash
uv run pytest -q
```

Expected: 两个项目全部 PASS；仅环境依赖测试可按已声明条件跳过。

- [ ] **Step 2: 重新 inspect server 项目**

```bash
package_inspection_json="$(uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app \
  docker-package-app inspect \
  /Users/wenjuhao/code/python/docker_manage_server --json)"
package_run_id="$(printf '%s' "$package_inspection_json" | jq -r '.run_id')"
package_answers_path="/Users/wenjuhao/code/python/docker_manage_server/.docker-manage/work/${package_run_id}/answers.json"
```

确认 `package_run_id` 非空，按 CLI 顺序展示 `package_inspection_json` 中的全部问题。

- [ ] **Step 3: 创建权限为 0600 的完整答案文件**

使用 `apply_patch` 在 `$package_answers_path` 创建以下完整 JSON，然后运行
`chmod 600 "$package_answers_path"`：

```json
{
  "values": {
    "env.server.COMPOSE_TIMEOUT_SECONDS": "1800",
    "env.server.DATA_DIR": "/data/docker-manage-server",
    "env.server.DOCKER_HOST": "unix:///var/run/docker.sock",
    "env.server.DOCKER_MANAGE_DATA_DIR": "/data/docker-manage-server",
    "env.server.DOCKER_MANAGE_SERVER_PORT": "6308",
    "env.server.PIP_ROOT_USER_ACTION": "ignore",
    "env.server.PYTHONDONTWRITEBYTECODE": "1",
    "env.server.PYTHONUNBUFFERED": "1",
    "port.server.8000/tcp.expose": "yes",
    "port.server.8000/tcp.host": "6308",
    "file.5a6d24bf5482629a.decision": "keep_server_path",
    "file.450617753fd5fa2f.decision": "keep_server_path"
  }
}
```

- [ ] **Step 4: 运行 plan 并核对有效路径**

```bash
package_plan_json="$(uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app \
  docker-package-app plan \
  /Users/wenjuhao/code/python/docker_manage_server \
  --run-id "$package_run_id" \
  --answers "$package_answers_path" \
  --non-interactive --json)"
package_plan_hash="$(printf '%s' "$package_plan_json" | jq -r '.plan_hash')"
```

Expected plan:

```text
platform=linux/amd64
port=6308:8000/tcp
DATA_DIR=/data/docker-manage-server
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
data resolved_path=/data/docker-manage-server
data action=keep_server_path
data payload_path=null
```

计划不得出现：

```text
./files/${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}
/Users/wenjuhao/code/python/docker_manage_server/${DOCKER_MANAGE_DATA_DIR...}
```

向用户展示完整计划和精确 `plan_hash`，等待明确确认。

- [ ] **Step 5: 使用用户确认的精确哈希执行 package**

```bash
package_result_json="$(uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app \
  docker-package-app package \
  /Users/wenjuhao/code/python/docker_manage_server \
  --run-id "$package_run_id" \
  --answers "$package_answers_path" \
  --confirm-plan-hash "$package_plan_hash" \
  --non-interactive --json)"
package_archive_path="$(printf '%s' "$package_result_json" | jq -r '.archive')"
```

Expected: `stage=packaged`，CLI 返回归档路径、大小、SHA-256、镜像和服务器路径。

- [ ] **Step 6: 验证归档内容与校验和**

Run:

```bash
shasum -a 256 "$package_archive_path"
tar -tzf "$package_archive_path"
tar -xOf "$package_archive_path" compose.yaml
tar -xOf "$package_archive_path" .env
tar -xOf "$package_archive_path" manifest.json
```

Expected:

```text
compose published port=6308, target=8000/tcp
.env DATA_DIR=/data/docker-manage-server
.env DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
manifest server_paths=[/data/docker-manage-server, /var/run/docker.sock]
archive does not contain data directory contents
reported SHA-256 equals shasum output
```

- [ ] **Step 7: 最终状态检查**

Run in both repositories:

```bash
git status --short --branch
```

Expected: 只允许既有 `.docker-manage/` 运行产物保持未跟踪；所有实现和测试修改均已
按任务提交，不得混入用户无关改动。
