# Port Snapshot and Bind Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `package-docker-app` 在成功打包后复用端口映射，并使归档中的 bind mount 目录为 `0777`、普通文件为 `0666`。

**Architecture:** 保留 `.docker-manage/.env` 的环境变量职责，新增版本化 `.docker-manage/ports.json` 保存最终 `PackagePlan.ports`。检查阶段把匹配的端口快照附加到 `PortCandidate`，问题生成器优先使用当前值；成功归档后通过统一配置写入入口更新环境变量和端口快照并在失败时回滚。bind 权限只在载荷副本上递归规范化，不跟随符号链接，也不放宽 Compose `configs` 和 `secrets`。

**Tech Stack:** Python 3.11+、Pydantic 2、python-dotenv 1.1、pytest 8、Ruff 0.12、Docker Compose v2

## Global Constraints

- `.docker-manage/.env` 继续只保存最近一次完整成功打包使用的环境变量。
- `.docker-manage/ports.json` 保存最近一次完整成功打包使用的端口暴露状态和宿主机端口。
- 端口身份固定为 `service + container_port + protocol`。
- `ports.json` 权限固定为 `0600`，`.docker-manage` 目录保持 `0700`。
- 只匹配本次检查已经发现的端口；未知、已删除或协议不同的快照条目不得生成新问题。
- 当前端口选择优先于 Compose 或 Dockerfile 声明值，但提示必须同时显示两者。
- 只有最终归档创建并验证成功后才能更新 `.env` 和 `ports.json`。
- `inspect`、`plan`、`--dry-run`、等待模型补充和失败任务不得更新当前配置。
- bind source 副本的目录递归设为 `0777`，普通文件递归设为 `0666`。
- 不跟随 bind 副本中的符号链接，不修改链接目标。
- Compose `configs`、`secrets`、named volumes、原项目文件和保留的服务器路径权限不得改变。
- 保持答案 JSON、问题 ID、命令参数、计划哈希和归档目录结构兼容。
- 使用 TDD；每项生产行为必须先看到对应测试因功能缺失而失败。

---

## File Map

- `skills/package-docker-app/scripts/docker_package_app/models.py`
  定义可序列化的当前端口选择，并让旧 `Inspection` 状态向后兼容。
- `skills/package-docker-app/scripts/docker_package_app/current_config.py`
  负责读取、校验、匹配和事务式写入 `.env` 与 `ports.json`。
- `skills/package-docker-app/scripts/docker_package_app/questions.py`
  把当前端口选择转换为问题默认值和中文提示。
- `skills/package-docker-app/scripts/docker_package_app/cli.py`
  在 inspect 阶段附加当前端口，在 package 成功路径统一写入当前配置。
- `skills/package-docker-app/scripts/docker_package_app/files.py`
  只对复制后的 bind mount 内容规范化权限。
- `skills/package-docker-app/SKILL.md`
  记录端口快照和 bind 权限的模型边界与工作流契约。
- `tests/unit/test_models.py`
  验证旧状态兼容和端口当前值往返序列化。
- `tests/unit/test_current_config.py`
  验证端口快照读取、校验、稳定写入和回滚。
- `tests/unit/test_questions.py`
  验证端口当前值的提示和默认选择。
- `tests/integration/test_cli.py`
  验证 inspect/plan 数据流和失败、dry-run 不改快照。
- `tests/unit/test_files.py`
  验证 bind、config、secret 和符号链接权限边界。
- `tests/e2e/test_package_flow.py`
  验证成功打包后的端口复用和归档 tar mode。
- `tests/unit/test_skill_contract.py`
  验证 skill 文本包含新的强制契约。

---

### Task 1: Port Snapshot Model and Reader

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/models.py:43-53`
- Modify: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/unit/test_current_config.py`

**Interfaces:**
- Produces: `CurrentPortSelection(exposed: bool, host_port: int | None)`.
- Produces: `PortCandidate.current: CurrentPortSelection | None`.
- Produces: `attach_current_ports(project_root: Path, candidates: Sequence[PortCandidate]) -> tuple[PortCandidate, ...]`.
- Produces: `CURRENT_PORTS_RELATIVE = Path(".docker-manage/ports.json")`.

- [ ] **Step 1: Write failing model compatibility tests**

Add the imports and test to `tests/unit/test_models.py`:

```python
from docker_package_app.models import CurrentPortSelection, PortCandidate


def test_port_candidate_current_selection_is_optional_and_round_trips() -> None:
    old = PortCandidate.model_validate(
        {"service": "web", "container_port": 8000, "host_port": 8080}
    )
    assert old.current is None

    current = CurrentPortSelection(exposed=True, host_port=8322)
    restored = PortCandidate.model_validate_json(
        PortCandidate(
            service="web",
            container_port=8000,
            host_port=8080,
            current=current,
        ).model_dump_json()
    )

    assert restored.current == current
```

- [ ] **Step 2: Run the model test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py::test_port_candidate_current_selection_is_optional_and_round_trips \
  -v
```

Expected: FAIL because `CurrentPortSelection` and `PortCandidate.current` do not exist.

- [ ] **Step 3: Add the current port model**

In `models.py`, add `model_validator` to the Pydantic imports and define:

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CurrentPortSelection(StrictModel):
    exposed: bool
    host_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def validate_host_port(self) -> "CurrentPortSelection":
        if self.exposed and self.host_port is None:
            raise ValueError("已暴露端口必须包含主机端口")
        if not self.exposed and self.host_port is not None:
            raise ValueError("未暴露端口不能包含主机端口")
        return self
```

Extend `PortCandidate`:

```python
class PortCandidate(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None
    host_port: int | None = Field(default=None, ge=1, le=65535)
    current: CurrentPortSelection | None = None
```

- [ ] **Step 4: Run the model test and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Write failing port snapshot reader tests**

Add these imports and tests to `tests/unit/test_current_config.py`:

```python
import json

from docker_package_app.current_config import (
    CURRENT_PORTS_RELATIVE,
    attach_current_ports,
)
from docker_package_app.models import PortCandidate


def test_attach_current_ports_matches_identity_and_ignores_unknown(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / CURRENT_PORTS_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ports": [
                    {
                        "service": "web",
                        "container_port": 8000,
                        "protocol": "tcp",
                        "exposed": True,
                        "host_port": 8322,
                    },
                    {
                        "service": "removed",
                        "container_port": 9000,
                        "protocol": "tcp",
                        "exposed": False,
                        "host_port": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    candidates = (
        PortCandidate(service="web", container_port=8000, host_port=8080),
        PortCandidate(service="web", container_port=8000, protocol="udp"),
    )

    attached = attach_current_ports(tmp_path, candidates)

    assert attached[0].current is not None
    assert attached[0].current.exposed is True
    assert attached[0].current.host_port == 8322
    assert attached[1].current is None
    assert len(attached) == len(candidates)


@pytest.mark.parametrize(
    "body",
    (
        "{",
        '{"schema_version":2,"ports":[]}',
        (
            '{"schema_version":1,"ports":['
            '{"service":"web","container_port":8000,"protocol":"tcp",'
            '"exposed":true,"host_port":null}]}'
        ),
    ),
)
def test_attach_current_ports_rejects_invalid_snapshot(
    tmp_path: Path,
    body: str,
) -> None:
    snapshot = tmp_path / CURRENT_PORTS_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(body, encoding="utf-8")

    with pytest.raises(UsageError, match="当前端口快照"):
        attach_current_ports(
            tmp_path,
            (PortCandidate(service="web", container_port=8000),),
        )
```

- [ ] **Step 6: Run the reader tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py::test_attach_current_ports_matches_identity_and_ignores_unknown \
  tests/unit/test_current_config.py::test_attach_current_ports_rejects_invalid_snapshot \
  -v
```

Expected: FAIL because `CURRENT_PORTS_RELATIVE` and `attach_current_ports` do not exist.

- [ ] **Step 7: Implement the strict snapshot reader**

Add these imports and private models to `current_config.py`:

```python
import json
from typing import Literal

from pydantic import ValidationError

from docker_package_app.models import (
    CurrentPortSelection,
    PortCandidate,
    StrictModel,
)

CURRENT_PORTS_RELATIVE = Path(".docker-manage/ports.json")


class _PortSnapshotEntry(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    exposed: bool
    host_port: int | None


class _PortSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    ports: tuple[_PortSnapshotEntry, ...] = ()
```

Add the reader:

```python
def attach_current_ports(
    project_root: Path,
    candidates: Sequence[PortCandidate],
) -> tuple[PortCandidate, ...]:
    snapshot = project_root.resolve() / CURRENT_PORTS_RELATIVE
    if not snapshot.exists():
        return tuple(candidates)
    if not snapshot.is_file():
        raise UsageError(f"当前端口快照不是普通文件：{snapshot}")
    try:
        body = _PortSnapshot.model_validate_json(snapshot.read_text(encoding="utf-8"))
        selections = {
            (item.service, item.container_port, item.protocol): CurrentPortSelection(
                exposed=item.exposed,
                host_port=item.host_port,
            )
            for item in body.ports
        }
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise UsageError(f"当前端口快照无效 {snapshot}：{exc}") from exc

    return tuple(
        candidate.model_copy(
            update={
                "current": selections.get(
                    (
                        candidate.service,
                        candidate.container_port,
                        candidate.protocol,
                    )
                )
            }
        )
        for candidate in candidates
    )
```

Before building `selections`, reject duplicate identity keys:

```python
    identities = [
        (item.service, item.container_port, item.protocol)
        for item in body.ports
    ]
    if len(identities) != len(set(identities)):
        raise UsageError(f"当前端口快照包含重复端口：{snapshot}")
```

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_models.py \
  tests/unit/test_current_config.py \
  -v
```

Expected: all selected tests PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/models.py \
  skills/package-docker-app/scripts/docker_package_app/current_config.py \
  tests/unit/test_models.py \
  tests/unit/test_current_config.py
git commit -m "feat: read current port snapshot"
```

---

### Task 2: Port Defaults Through Inspect and Plan

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/questions.py:59-89`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py:20-28,360-370`
- Modify: `tests/unit/test_questions.py`
- Modify: `tests/integration/test_cli.py`

**Interfaces:**
- Consumes: `PortCandidate.current`.
- Consumes: `attach_current_ports(project_root, candidates)`.
- Produces: unchanged question IDs `port.<service>.<container>/<protocol>.expose` and `.host`.

- [ ] **Step 1: Write failing question-default tests**

Add `CurrentPortSelection` and `PortCandidate` to the imports in
`tests/unit/test_questions.py`, then add:

```python
def test_current_port_selection_precedes_declared_mapping() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/project",
        stage=Stage.INSPECTED,
        ports=(
            PortCandidate(
                service="web",
                container_port=8000,
                host_port=8080,
                current=CurrentPortSelection(exposed=True, host_port=8322),
            ),
            PortCandidate(
                service="worker",
                container_port=9000,
                host_port=9090,
                current=CurrentPortSelection(exposed=False, host_port=None),
            ),
        ),
    )

    questions = {item.id: item for item in build_questions(inspection)}

    web_expose = questions["port.web.8000/tcp.expose"]
    web_host = questions["port.web.8000/tcp.host"]
    worker_expose = questions["port.worker.9000/tcp.expose"]
    assert web_expose.default == "yes"
    assert web_host.default == "8322"
    assert worker_expose.default == "no"
    assert "当前配置：已暴露，主机端口 8322" in web_expose.prompt
    assert "声明映射：主机端口 8080" in web_expose.prompt
    assert "当前配置：不暴露" in worker_expose.prompt
```

- [ ] **Step 2: Run the question test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py::test_current_port_selection_precedes_declared_mapping \
  -v
```

Expected: FAIL because questions still derive defaults only from `host_port`.

- [ ] **Step 3: Make question generation prefer the current selection**

Replace the port loop's question construction in `questions.py` with:

```python
        current = port.current
        declared_exposed = port.host_port is not None
        exposed = current.exposed if current is not None else declared_exposed
        host_port = (
            current.host_port
            if current is not None and current.exposed
            else port.host_port or port.container_port
        )
        current_text = ""
        if current is not None:
            current_text = (
                f" 当前配置：已暴露，主机端口 {current.host_port}；"
                if current.exposed
                else " 当前配置：不暴露；"
            )
        declared_text = (
            f"声明映射：主机端口 {port.host_port}。"
            if declared_exposed
            else "声明映射：不暴露。"
        )
        questions.append(
            Question(
                id=f"{prefix}.expose",
                kind="port_expose",
                prompt=(
                    f"是否暴露 {port.service} 的容器端口 "
                    f"{port.container_port}/{port.protocol}？"
                    f"{current_text}{declared_text}（yes=是，no=否）"
                ),
                default="yes" if exposed else "no",
                choices=("yes", "no"),
            )
        )
        questions.append(
            Question(
                id=f"{prefix}.host",
                kind="port_host",
                prompt=(
                    f"设置 {port.service}:{port.container_port}/{port.protocol} "
                    "对应的主机端口"
                ),
                default=str(host_port),
            )
        )
```

- [ ] **Step 4: Run the question suite and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_questions.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Write a failing inspect-to-plan integration test**

Add to `tests/integration/test_cli.py`:

```python
def test_inspect_and_plan_use_current_port_snapshot(
    cli: CliRunner,
    compose_project: Path,
) -> None:
    snapshot = compose_project / ".docker-manage/ports.json"
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ports": [
                    {
                        "service": "web",
                        "container_port": 8000,
                        "protocol": "tcp",
                        "exposed": True,
                        "host_port": 8322,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    inspected = cli("inspect", str(compose_project), "--json")

    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    port = body["ports"][0]
    assert port["current"] == {"exposed": True, "host_port": 8322}
    questions = {item["id"]: item for item in body["questions"]}
    assert questions["port.web.8000/tcp.expose"]["default"] == "yes"
    assert questions["port.web.8000/tcp.host"]["default"] == "8322"

    answers = compose_project / "current-port-answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PORT": "8000",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8322",
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
    assert json.loads(planned.stdout)["plan"]["ports"][0]["host_port"] == 8322
```

- [ ] **Step 6: Run the integration test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/integration/test_cli.py::test_inspect_and_plan_use_current_port_snapshot \
  -v
```

Expected: FAIL because inspect has not attached `ports.json`.

- [ ] **Step 7: Attach current ports during inspect**

Update the `current_config` import in `cli.py`:

```python
from docker_package_app.current_config import (
    attach_current_ports,
    attach_current_values,
    write_current_environment,
)
```

Change the final inspection update to:

```python
    inspection = inspection.model_copy(
        update={
            "env": attach_current_values(project, inspection.env),
            "ports": attach_current_ports(project, inspection.ports),
        }
    )
```

Keep calling `write_current_environment` until Task 3 introduces
`write_current_configuration`; do not change package success behavior in this
task.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py::test_inspect_and_plan_use_current_port_snapshot \
  -v
```

Expected: all selected tests PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/questions.py \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_questions.py \
  tests/integration/test_cli.py
git commit -m "feat: reuse current port defaults"
```

---

### Task 3: Successful Port Persistence and Snapshot Rollback

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py:474-485`
- Modify: `tests/unit/test_current_config.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/e2e/test_package_flow.py`

**Interfaces:**
- Produces: `write_current_ports(project_root: Path, assignments: Sequence[PortAssignment]) -> Path`.
- Produces: `write_current_configuration(project_root: Path, environment: Sequence[EnvAssignment], ports: Sequence[PortAssignment]) -> tuple[Path, Path]`.
- Consumes: final `PackagePlan.environment` and `PackagePlan.ports`.

- [ ] **Step 1: Write failing deterministic port writer test**

Add `stat`, `PortAssignment`, and `write_current_ports` imports in
`tests/unit/test_current_config.py`, then add:

```python
def test_write_current_ports_is_complete_sorted_and_private(
    tmp_path: Path,
) -> None:
    path = write_current_ports(
        tmp_path,
        (
            PortAssignment(
                service="worker",
                container_port=9000,
                exposed=False,
            ),
            PortAssignment(
                service="web",
                container_port=8000,
                exposed=True,
                host_port=8322,
            ),
        ),
    )

    assert path == tmp_path / ".docker-manage/ports.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "ports": [
            {
                "service": "web",
                "container_port": 8000,
                "protocol": "tcp",
                "exposed": True,
                "host_port": 8322,
            },
            {
                "service": "worker",
                "container_port": 9000,
                "protocol": "tcp",
                "exposed": False,
                "host_port": None,
            },
        ],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
```

- [ ] **Step 2: Run the writer test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py::test_write_current_ports_is_complete_sorted_and_private \
  -v
```

Expected: FAIL because `write_current_ports` does not exist.

- [ ] **Step 3: Implement the atomic port writer**

Add `PortAssignment` to the imports in `current_config.py` and add:

```python
def write_current_ports(
    project_root: Path,
    assignments: Sequence[PortAssignment],
) -> Path:
    target = project_root.resolve() / CURRENT_PORTS_RELATIVE
    entries = tuple(
        _PortSnapshotEntry(
            service=item.service,
            container_port=item.container_port,
            protocol=item.protocol,
            exposed=item.exposed,
            host_port=item.host_port,
        )
        for item in sorted(
            assignments,
            key=lambda value: (
                value.service,
                value.container_port,
                value.protocol,
            ),
        )
    )
    body = _PortSnapshot(ports=entries).model_dump_json(indent=2) + "\n"
    return _atomic_write_snapshot(target, body)
```

Add a reusable atomic text writer for the new JSON snapshot and rollback:

```python
def _atomic_write_snapshot(target: Path, body: str) -> Path:
    temporary: Path | None = None
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
        temporary = None
        return target
    except OSError as exc:
        raise PackageError(f"无法更新当前配置快照 {target}：{exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
```

Leave `write_current_environment` on its existing python-dotenv path so sorted
keys, dotenv quoting, `0600`, `fsync`, and `os.replace` remain unchanged.

- [ ] **Step 4: Run current-config tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_current_config.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Write failing rollback test**

Add to `tests/unit/test_current_config.py`:

```python
def test_configuration_write_restores_environment_when_ports_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".docker-manage/.env"
    ports_path = tmp_path / ".docker-manage/ports.json"
    env_path.parent.mkdir()
    env_path.write_text("PORT='last-good'\n", encoding="utf-8")
    ports_path.write_text('{"schema_version":1,"ports":[]}\n', encoding="utf-8")

    def fail_ports(_root: Path, _ports: object) -> Path:
        raise PackageError("ports failed")

    monkeypatch.setattr(
        "docker_package_app.current_config.write_current_ports",
        fail_ports,
    )

    with pytest.raises(PackageError, match="ports failed"):
        write_current_configuration(
            tmp_path,
            (
                EnvAssignment(
                    service="web",
                    container_name="PORT",
                    artifact_name="PORT",
                    value="next",
                ),
            ),
            (
                PortAssignment(
                    service="web",
                    container_port=8000,
                    exposed=True,
                    host_port=8322,
                ),
            ),
        )

    assert env_path.read_text(encoding="utf-8") == "PORT='last-good'\n"
    assert ports_path.read_text(encoding="utf-8") == (
        '{"schema_version":1,"ports":[]}\n'
    )
```

- [ ] **Step 6: Run rollback test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_current_config.py::test_configuration_write_restores_environment_when_ports_fail \
  -v
```

Expected: FAIL because `write_current_configuration` does not exist.

- [ ] **Step 7: Implement the coordinated write and restore**

Add to `current_config.py`:

```python
def write_current_configuration(
    project_root: Path,
    environment: Sequence[EnvAssignment],
    ports: Sequence[PortAssignment],
) -> tuple[Path, Path]:
    root = project_root.resolve()
    targets = (
        root / CURRENT_ENV_RELATIVE,
        root / CURRENT_PORTS_RELATIVE,
    )
    previous = {
        target: (
            target.read_bytes() if target.exists() else None,
            target.stat().st_mode & 0o777 if target.exists() else None,
        )
        for target in targets
    }
    try:
        env_path = write_current_environment(root, environment)
        ports_path = write_current_ports(root, ports)
        return env_path, ports_path
    except PackageError as original:
        failures: list[str] = []
        for target, (body, mode) in previous.items():
            try:
                if body is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write_snapshot(
                        target,
                        body.decode("utf-8"),
                    )
                    if mode is not None:
                        target.chmod(mode)
            except (OSError, PackageError, UnicodeDecodeError) as restore_error:
                failures.append(f"{target}: {restore_error}")
        if failures:
            raise PackageError(
                f"{original}；当前配置恢复失败，请人工检查："
                + "；".join(failures)
            ) from original
        raise
```

Both snapshot formats are UTF-8 text, so restoration uses their exact previous
text content. Keep the original error when restoration succeeds.

- [ ] **Step 8: Run current-config tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_current_config.py -v
```

Expected: all tests PASS.

- [ ] **Step 9: Write failing end-to-end persistence assertions**

Extend `test_successful_package_becomes_next_inspection_current_config` in
`tests/e2e/test_package_flow.py`:

```python
    ports_snapshot = compose_project / ".docker-manage/ports.json"
    assert json.loads(ports_snapshot.read_text(encoding="utf-8"))["ports"] == [
        {
            "service": "web",
            "container_port": 8000,
            "protocol": "tcp",
            "exposed": True,
            "host_port": 8080,
        }
    ]

    inspected_body = json.loads(inspected.stdout)
    questions = {item["id"]: item for item in inspected_body["questions"]}
    assert questions["port.web.8000/tcp.expose"]["default"] == "yes"
    assert questions["port.web.8000/tcp.host"]["default"] == "8080"
    assert "当前配置" in questions["port.web.8000/tcp.expose"]["prompt"]
```

In both `test_dry_run_stops_before_docker_mutations` and
`test_failed_package_does_not_replace_current_environment` in
`tests/integration/test_cli.py`, add this setup immediately after the existing
`.env` setup:

```python
    ports_snapshot = compose_project / ".docker-manage/ports.json"
    ports_snapshot.write_text(
        '{"schema_version":1,"ports":[]}\n',
        encoding="utf-8",
    )
    previous_ports = ports_snapshot.read_text(encoding="utf-8")
```

Add this assertion after each test's existing `.env` snapshot assertion:

```python
    assert ports_snapshot.read_text(encoding="utf-8") == previous_ports
```

- [ ] **Step 10: Run persistence tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/e2e/test_package_flow.py::test_successful_package_becomes_next_inspection_current_config \
  tests/integration/test_cli.py::test_dry_run_stops_before_docker_mutations \
  tests/integration/test_cli.py::test_failed_package_does_not_replace_current_environment \
  -v
```

Expected: the end-to-end test FAILS because package success still writes only
`.env`; the unchanged-snapshot failure tests remain green.

- [ ] **Step 11: Use the coordinated writer only after archive success**

In `cli.py`, replace:

```python
    write_current_environment(paths.project_root, plan.environment)
```

with:

```python
    write_current_configuration(
        paths.project_root,
        plan.environment,
        plan.ports,
    )
```

Replace the old direct `write_current_environment` import with
`write_current_configuration`.

- [ ] **Step 12: Run persistence tests and verify GREEN**

Run the command from Step 10.

Expected: all selected tests PASS.

- [ ] **Step 13: Commit Task 3**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/current_config.py \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_current_config.py \
  tests/integration/test_cli.py \
  tests/e2e/test_package_flow.py
git commit -m "feat: persist successful port snapshot"
```

---

### Task 4: World-Writable Bind Payload Permissions

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/files.py:60-100,165-183`
- Modify: `tests/unit/test_files.py`
- Modify: `tests/e2e/test_package_flow.py`

**Interfaces:**
- Produces: `_make_bind_writable(path: Path) -> None`.
- Consumes: materialized bind destinations under `payload/files`.
- Leaves config and secret materialization behavior unchanged.

- [ ] **Step 1: Write failing unit tests for recursive modes and boundaries**

Add `stat` to `tests/unit/test_files.py`, then add:

```python
def test_copied_bind_is_world_readable_and_writable_recursively(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config"
    nested = source / "nested"
    nested.mkdir(parents=True)
    file_path = nested / "app.ini"
    file_path.write_text("mode=prod\n", encoding="utf-8")
    source.chmod(0o750)
    nested.chmod(0o700)
    file_path.chmod(0o600)
    candidate = discover_file_dependencies(
        _compose(tmp_path, "./config"),
        tmp_path,
    )[0]

    materialize_files(
        (candidate,),
        {candidate.resolved_path: FileAction.COPY},
        tmp_path / "payload",
    )

    copied = tmp_path / "payload/files/config"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o777
    assert stat.S_IMODE((copied / "nested").stat().st_mode) == 0o777
    assert stat.S_IMODE((copied / "nested/app.ini").stat().st_mode) == 0o666


def test_bind_permission_normalization_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    outside.chmod(0o600)
    copied_bind = tmp_path / "payload/files/bind"
    copied_bind.mkdir(parents=True)
    (copied_bind / "link").symlink_to(outside)

    _make_bind_writable(copied_bind)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert (copied_bind / "link").is_symlink()


def test_copied_config_permissions_are_preserved(tmp_path: Path) -> None:
    config_source = tmp_path / "app.ini"
    config_source.write_text("secret=true\n", encoding="utf-8")
    config_source.chmod(0o600)
    candidates = tuple(
        item
        for item in discover_file_dependencies(
            _compose(tmp_path, "./unused-bind"),
            tmp_path,
        )
        if item.kind == "config"
    )

    materialize_files(
        candidates,
        {item.resolved_path: FileAction.COPY for item in candidates},
        tmp_path / "payload",
    )

    assert stat.S_IMODE(
        (tmp_path / "payload/files/app.ini").stat().st_mode
    ) == 0o600
```

Import the private `_make_bind_writable` helper in this focused unit test. Use
the existing `_compose()` helper, which includes `./app.ini` as a Compose
config, for the config boundary test.

In `test_multi_service_package_is_complete` in
`tests/e2e/test_package_flow.py`, add these assertions inside the existing open
tar block:

```python
        assert bundle.getmember("files/config").mode == 0o777
        assert bundle.getmember("files/config/app.ini").mode == 0o666
```

- [ ] **Step 2: Run permission tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_files.py::test_copied_bind_is_world_readable_and_writable_recursively \
  tests/unit/test_files.py::test_bind_permission_normalization_does_not_follow_symlinks \
  tests/unit/test_files.py::test_copied_config_permissions_are_preserved \
  tests/e2e/test_package_flow.py::test_multi_service_package_is_complete \
  -v
```

Expected: the recursive bind mode and tar-member assertions FAIL because copied
permissions are currently preserved. The symlink and config boundary assertions
remain green.

- [ ] **Step 3: Normalize only copied bind destinations**

In `materialize_files`, add a destination set before the loop:

```python
    bind_destinations: set[Path] = set()
```

After each copied candidate receives `destination`, record bind paths:

```python
        if candidate.kind == "bind":
            bind_destinations.add(destination)
```

After the candidate loop and before returning, normalize all bind destinations:

```python
    for destination in sorted(bind_destinations):
        _make_bind_writable(destination)
```

Add:

```python
def _make_bind_writable(path: Path) -> None:
    try:
        if path.is_symlink():
            return
        if path.is_file():
            path.chmod(0o666)
            return
        if not path.is_dir():
            raise PackageError(f"bind mount 副本不是普通文件或目录：{path}")
        for root, directories, files in os.walk(path, followlinks=False):
            current = Path(root)
            current.chmod(0o777)
            for name in directories:
                child = current / name
                if not child.is_symlink():
                    child.chmod(0o777)
            for name in files:
                child = current / name
                if not child.is_symlink():
                    if not child.is_file():
                        raise PackageError(
                            f"bind mount 副本包含非普通文件：{child}"
                        )
                    child.chmod(0o666)
    except OSError as exc:
        raise PackageError(f"无法设置 bind mount 副本权限 {path}：{exc}") from exc
```

The set is applied after all copies, so a source referenced both as a config
and a bind is writable because it is genuinely mounted as a bind, independent
of discovery order.

- [ ] **Step 4: Run file and end-to-end permission tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_files.py \
  tests/e2e/test_package_flow.py::test_multi_service_package_is_complete \
  -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/files.py \
  tests/unit/test_files.py \
  tests/e2e/test_package_flow.py
git commit -m "feat: normalize copied bind permissions"
```

---

### Task 5: Skill Contract and Full Verification

**Files:**
- Modify: `skills/package-docker-app/SKILL.md`
- Modify: `tests/unit/test_skill_contract.py`

**Interfaces:**
- Documents: `.docker-manage/ports.json` ownership and successful-write timing.
- Documents: current versus declared port values.
- Documents: bind directory `0777`, regular file `0666`, and original-file immutability.

- [ ] **Step 1: Write a failing skill contract test**

Add to `tests/unit/test_skill_contract.py`:

```python
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
```

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_skill_contract.py::test_skill_uses_current_ports_and_writable_bind_copies \
  -v
```

Expected: FAIL because the skill does not mention `ports.json` or permission
modes.

- [ ] **Step 3: Update the skill invariants and workflow**

Add these invariants after the existing `.env` rules in `SKILL.md`:

```markdown
- `<project>/.docker-manage/ports.json` 表示最近一次完整成功打包使用的项目级当前端口配置；不得从历史 `state.json` 推断当前端口。
- 模型不得直接编辑 `.docker-manage/ports.json`。只有随附 CLI 可以在完整成功打包后与 `.env` 一起更新该文件。
- 对复制进归档的 bind mount 副本递归设置权限：目录权限为 `0777`，普通文件权限为 `0666`。不得跟随符号链接，不得修改原项目文件权限、Compose `configs`、`secrets` 或保留的服务器路径权限。
```

Extend workflow Step 3:

```markdown
端口问题同时显示完整的当前端口配置和声明端口映射，并优先采用当前端口配置。当前配置与声明映射不同不算冲突；用户回复 `默认` 时采用问题中的当前默认值。
```

Replace the package-success snapshot sentence in Step 8 with:

```markdown
CLI 只有完整成功打包后才会一起更新 `.docker-manage/.env` 和 `.docker-manage/ports.json`。`inspect`、`plan`、`--dry-run`、等待模型补充和失败任务不得改变当前环境变量或当前端口配置快照。
```

Keep the plan-confirmation requirement that lists both exposed and omitted
port mappings.

- [ ] **Step 4: Run skill contract tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run formatting and static checks**

Run:

```bash
uv run --project skills/package-docker-app ruff format --check \
  skills/package-docker-app/scripts tests
```

Expected: exit 0 and no files needing formatting.

Run:

```bash
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
```

Expected: exit 0 with no diagnostics.

- [ ] **Step 6: Run the complete automated test suite**

Run:

```bash
uv run --project skills/package-docker-app pytest -v
```

Expected: all non-environmental tests PASS; Docker smoke tests remain skipped
unless `RUN_DOCKER_SMOKE=1`.

- [ ] **Step 7: Validate the skill structure**

Run:

```bash
uv run --project skills/package-docker-app python \
  /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
```

Expected: validation succeeds with no frontmatter, naming, or structure errors.

- [ ] **Step 8: Review final diff and commit**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: only the intended skill, CLI, test, and planning files are changed;
`git diff --check` prints nothing.

Commit:

```bash
git add skills/package-docker-app/SKILL.md tests/unit/test_skill_contract.py
git commit -m "docs: describe port snapshots and bind modes"
```

- [ ] **Step 9: Final verification after all commits**

Run:

```bash
uv run --project skills/package-docker-app pytest -q
```

Expected: exit 0.

Run:

```bash
git status --short
```

Expected: no uncommitted changes.
