# Stable Bind Deployment Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让项目内 bind mount 无论本次选择复制还是保留服务器数据，部署 Compose 都稳定指向同一个 `./files/...` 地址，并彻底避免开发电脑绝对路径进入部署制品。

**Architecture:** Compose 检查阶段通过 `--no-path-resolution` 保留 Compose 中的路径表达；文件发现阶段仍在内部解析绝对路径用于安全检查。文件物化阶段为项目内 bind 统一生成 `./files/<项目相对路径>` rewrite，`copy` 才复制内容，`keep_server_path` 只记录部署依赖。manifest 和 CLI 输出复用同一部署 source 规则。

**Tech Stack:** Python 3.12、Docker Compose v2、PyYAML、Pydantic、pytest、Ruff

## Global Constraints

- 原始项目文件不得被修改。
- 项目内 bind 的稳定部署 source 必须为 `./files/<项目相对路径>`。
- `copy` 与 `keep_server_path` 只决定本次归档是否包含目录内容，不得改变项目内 bind 的部署 source。
- 项目外 bind 不得复制，选择 `keep_server_path` 时保留原始 Compose source。
- 部署 Compose、manifest 和 CLI 结果不得包含 Docker Compose 自动解析出的开发电脑绝对路径。
- named volume、Compose config、Compose secret、端口、环境变量和镜像处理规则保持不变。

---

### Task 1: Preserve Compose Path Expressions

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/compose.py`
- Test: `tests/unit/test_compose.py`
- Test: `tests/integration/test_real_compose.py`

**Interfaces:**
- Consumes: `ComposeDocument.load(project_root, files, profiles, runner)`
- Produces: 加载后仍包含原始相对 bind source 的 `ComposeDocument`

- [ ] **Step 1: Write the failing unit and real Compose tests**

在 `tests/unit/test_compose.py` 的期望命令末尾加入：

```python
"--no-interpolate",
"--no-path-resolution",
```

新增 `tests/integration/test_real_compose.py`：

```python
from pathlib import Path
import shutil

import pytest
from docker_package_app.command import CommandRunner
from docker_package_app.compose import ComposeDocument


@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker CLI is unavailable",
)
def test_real_compose_keeps_relative_bind_source(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(
        "services:\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        "    volumes:\n"
        "      - ./data:/app/data\n",
        encoding="utf-8",
    )

    document = ComposeDocument.load(
        tmp_path,
        [compose_path],
        [],
        CommandRunner(),
    )

    assert document.service("app")["volumes"][0]["source"] == "./data"
```

- [ ] **Step 2: Run tests to verify the unit test fails**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_compose.py tests/integration/test_real_compose.py -q
```

Expected: unit test fails because `--no-path-resolution` is absent; the real Compose test reports an absolute source.

- [ ] **Step 3: Add the path-preservation flag**

In `ComposeDocument.load`, build the config suffix as:

```python
argv.extend(
    [
        "config",
        "--format",
        "json",
        "--no-interpolate",
        "--no-path-resolution",
    ]
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_compose.py tests/integration/test_real_compose.py -q
```

Expected: all selected tests pass, or only the real Docker Compose test skips when the CLI is unavailable.

- [ ] **Step 5: Commit**

```bash
git add skills/package-docker-app/scripts/docker_package_app/compose.py tests/unit/test_compose.py tests/integration/test_real_compose.py
git commit -m "fix: preserve compose path expressions"
```

### Task 2: Keep Stable Project Bind Deployment Sources

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/files.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/artifact.py`
- Test: `tests/unit/test_files.py`
- Test: `tests/unit/test_artifact.py`
- Test: `tests/e2e/test_package_flow.py`

**Interfaces:**
- Consumes: `FileCandidate.project_path`, `FileAssignment`, `PackagePlan.project_root`
- Produces: `deployment_source(project_root, resolved_path, original_value) -> str`
- Produces: `FileMaterialization.rewrites` and `server_paths` using deployment-side paths

- [ ] **Step 1: Change the file and end-to-end assertions first**

Update the kept-project-bind assertions:

```python
assert not (tmp_path / "payload/files/data").exists()
assert result.rewrites == {
    ("web", "bind", "./data"): "./files/data",
}
assert result.server_paths == ("./files/data",)
assert result.copied_bytes == 0
```

Update the same-source assertion:

```python
assert result.rewrites[("web", "bind", "./shared.ini")] == "./files/shared.ini"
```

In the end-to-end test, prepare the existing server data under
`deployment / "files/data"` and assert:

```python
assert output["server_paths"] == ["./files/data"]
assert volumes[1]["source"] == "./files/data"
assert manifest["server_paths"] == ["./files/data"]
assert (
    deployment / "files/data/server.db"
).read_text(encoding="utf-8") == "existing-test-data"
assert not (deployment / "files/data/local.db").exists()
```

Add a manifest unit test with a project-internal kept assignment and assert its
`server_paths` value is `["./files/data"]`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_files.py tests/unit/test_artifact.py tests/e2e/test_package_flow.py::test_multi_service_package_is_complete -q
```

Expected: failures show the kept bind has no rewrite and local absolute paths remain in `server_paths`.

- [ ] **Step 3: Implement one deployment source rule**

Add to `files.py`:

```python
def deployment_source(
    project_root: Path,
    resolved_path: str,
    original_value: str,
) -> str:
    root = project_root.resolve()
    resolved = Path(resolved_path).resolve()
    if resolved.is_relative_to(root):
        relative = resolved.relative_to(root)
        project_path = relative if relative.parts else Path("project")
        return f"./files/{project_path.as_posix()}"
    return original_value
```

In `materialize_files`, handle `KEEP_SERVER_PATH` as:

```python
def materialize_files(
    candidates: Sequence[FileCandidate],
    assignments: Sequence[FileAssignment],
    payload_root: Path,
    project_root: Path,
) -> FileMaterialization:
    ...

if action is FileAction.KEEP_SERVER_PATH:
    server_source = deployment_source(
        project_root,
        candidate.resolved_path,
        candidate.compose_value,
    )
    server_paths.add(server_source)
    if candidate.kind == "bind" and candidate.inside_project:
        rewrites[_rewrite_key(candidate)] = server_source
    continue
```

Pass `paths.project_root` from `_perform_package`; pass `tmp_path` from each
unit test. The project root remains explicit and is never reconstructed from
candidate strings.

For copied dependencies, use the same helper result for rewrites:

```python
rewrites[_rewrite_key(candidate)] = deployment_source(
    project_root,
    candidate.resolved_path,
    candidate.compose_value,
)
```

In `artifact.py`, import `deployment_source` and build manifest server paths:

```python
server_paths=tuple(
    sorted(
        {
            deployment_source(
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

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_files.py tests/unit/test_artifact.py tests/e2e/test_package_flow.py::test_multi_service_package_is_complete -q
```

Expected: all selected tests pass and neither archive assertions nor manifest values contain the local project root.

- [ ] **Step 5: Commit**

```bash
git add skills/package-docker-app/scripts/docker_package_app/files.py skills/package-docker-app/scripts/docker_package_app/artifact.py skills/package-docker-app/scripts/docker_package_app/cli.py tests/unit/test_files.py tests/unit/test_artifact.py tests/e2e/test_package_flow.py
git commit -m "fix: keep stable bind deployment paths"
```

### Task 3: Update the Skill Contract and Run Full Verification

**Files:**
- Modify: `skills/package-docker-app/SKILL.md`
- Modify: `tests/unit/test_skill_contract.py`
- Modify: `docs/superpowers/specs/2026-07-29-bind-mount-server-data-preservation-design.md`

**Interfaces:**
- Consumes: stable deployment path behavior from Tasks 1 and 2
- Produces: documented and executable skill contract

- [ ] **Step 1: Update the contract assertion**

Require the skill text to include these phrases:

```python
"稳定部署路径",
"`./files/`",
"不得进入归档",
"不得包含开发电脑的绝对路径",
```

- [ ] **Step 2: Run the contract test to verify it fails**

Run:

```bash
uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -q
```

Expected: failure reports the missing new contract phrases.

- [ ] **Step 3: Update the skill instructions**

Replace the project-internal `keep_server_path` rule with:

```markdown
- 选择 `keep_server_path` 时，本机 source 及其内容不得进入归档。项目内 bind 的部署 Compose 必须继续使用与 `copy` 相同的稳定部署路径 `./files/<项目相对路径>`；项目外 bind 保留原始 source。部署 Compose、manifest 和结果输出不得包含 Docker Compose 自动解析出的开发电脑绝对路径。
```

Update the workflow plan display instruction so it explicitly shows both
“是否携带内容” and the stable deployment source.

- [ ] **Step 4: Run complete verification**

Run:

```bash
uv run --project skills/package-docker-app pytest -q
uv run --project skills/package-docker-app ruff check skills/package-docker-app/scripts tests
python /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/package-docker-app
```

Expected: all tests pass, Ruff reports no issues, and skill validation prints
`Skill is valid!`.

- [ ] **Step 5: Commit**

```bash
git add skills/package-docker-app/SKILL.md tests/unit/test_skill_contract.py docs/superpowers/specs/2026-07-29-bind-mount-server-data-preservation-design.md docs/superpowers/plans/2026-07-29-stable-bind-deployment-paths.md
git commit -m "docs: define stable bind deployment paths"
```
