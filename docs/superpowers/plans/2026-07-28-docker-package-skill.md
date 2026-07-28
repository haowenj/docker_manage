# Docker Application Packaging Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Codex skill and deterministic Python CLI that inspect a local project, collect deployment values, build platform-specific Docker images, and produce a validated Docker Manage deployment archive without modifying existing project files.

**Architecture:** `SKILL.md` delegates all deterministic work to a versioned Python state machine. The CLI owns discovery, Compose transformation, prompts, Docker commands, local-file collection, checksums, and archive creation; the model only supplies schema-validated Docker files or source-analysis facts when fixed scanners report ambiguity.

**Tech Stack:** Python 3.11+, `uv`, Pydantic 2, PyYAML 6, python-dotenv 1, pytest, Docker Engine, Docker Compose v2, Docker Buildx

## Global Constraints

- Keep all skill source under `skills/package-docker-app/`; initialize it with the official skill creator script.
- Never modify an existing target-project Dockerfile, Compose file, env file, `.dockerignore`, `.gitignore`, or business source file.
- Store generated target-project state only under `.docker-manage/{generated,work,dist}` and force `.docker-manage/` out of build contexts and copied directory mounts.
- Treat Python CLI state as authoritative; model output is untrusted structured input and must pass schema plus path validation.
- Ignore hardcoded application configuration; collect only explicit environment-variable reads and Docker/Compose declarations.
- Show all defaults, including secrets, in plaintext as approved for the trusted single-admin network.
- Use `linux/amd64` as the default target platform and support one target platform per archive.
- Use argument-vector subprocess calls only; never execute model or user text through a shell.
- Final deployment Compose files contain no `build:` keys and every service has an `image:` key.
- Write state, `.env`, and artifacts with user-only permissions; create final archives by atomic rename after full verification.
- Keep Docker Manage server APIs, upload, deployment, LDAP, rollback, and named-volume data outside this plan.

## File Map

```text
.gitignore                                      Repository-only ignores
skills/package-docker-app/
├── SKILL.md                                    Agent workflow and model boundary
├── agents/openai.yaml                          Skill UI metadata
├── pyproject.toml                              Runtime and test dependencies
├── uv.lock                                     Reproducible dependency lock
├── references/model-supplement.schema.json     Allowed model response contract
└── scripts/docker_package_app/
    ├── __init__.py                             CLI and artifact version constants
    ├── models.py                               Pydantic domain/state models
    ├── errors.py                               Stable errors and exit codes
    ├── workspace.py                            Secure directories and atomic JSON
    ├── command.py                              Shell-free command abstraction
    ├── discovery.py                            Preflight and Docker-file discovery
    ├── compose.py                              Structured Compose load/classification
    ├── envscan.py                              Deterministic env discovery and merge
    ├── questions.py                            Prompt rendering and answer parsing
    ├── planning.py                             Immutable package-plan construction
    ├── supplement.py                           Model schema and path validation
    ├── files.py                                Bind/config/secret collection
    ├── docker.py                               Build, pull, inspect, save operations
    ├── render.py                               Deployment Compose and dotenv output
    ├── artifact.py                             Manifest, hashes, archive verification
    └── cli.py                                  Subcommands and state transitions
tests/
├── conftest.py                                 Shared project and runner fixtures
├── unit/                                       One test module per core module
├── integration/                                Fake-Docker and CLI process tests
├── fixtures/                                   Minimal project inputs
└── smoke/test_real_docker.py                   Opt-in daemon-backed smoke test
```

---

### Task 1: Initialize the Skill and Define Versioned Domain Contracts

**Required Skills:** Read and follow `skill-creator` before running `init_skill.py`; use `superpowers:test-driven-development` for every behavior added in this plan.

**Files:**
- Create: `.gitignore`
- Create: `skills/package-docker-app/SKILL.md`
- Create: `skills/package-docker-app/agents/openai.yaml`
- Create: `skills/package-docker-app/pyproject.toml`
- Create: `skills/package-docker-app/uv.lock`
- Create: `skills/package-docker-app/scripts/docker_package_app/__init__.py`
- Create: `skills/package-docker-app/scripts/docker_package_app/models.py`
- Create: `skills/package-docker-app/scripts/docker_package_app/errors.py`
- Create: `tests/unit/test_models.py`

**Interfaces:**
- Produces: `Stage`, `ImageAction`, `FileAction`, candidate models, assignment models, `Inspection`, `Question`, `AnswerBook`, `PackagePlan`, and `RunState`.
- Produces: exit codes `EXIT_OK=0`, `EXIT_RUNTIME=1`, `EXIT_USAGE=2`, `EXIT_ANSWERS_REQUIRED=10`, `EXIT_MODEL_REQUIRED=20`.

- [ ] **Step 1: Initialize the official skill structure and dependency project**

Run:

```bash
python3 /Users/wenjuhao/.codex-company/skills/.system/skill-creator/scripts/init_skill.py package-docker-app \
  --path skills \
  --resources scripts,references \
  --interface 'display_name=Package Docker App' \
  --interface 'short_description=Build portable Docker deployment archives' \
  --interface 'default_prompt=Use $package-docker-app to inspect this project and build a portable Docker deployment archive.'
```

Create `.gitignore` with `.DS_Store`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.coverage`, and `htmlcov/`. Create this complete `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=80,<81"]
build-backend = "setuptools.build_meta"

[project]
name = "docker-package-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.11,<3",
  "PyYAML>=6,<7",
  "python-dotenv>=1.1,<2",
]

[project.scripts]
docker-package-app = "docker_package_app.cli:main"

[dependency-groups]
dev = [
  "pytest>=8,<9",
  "pytest-cov>=6,<7",
  "ruff>=0.12,<1",
]

[tool.setuptools]
package-dir = {"" = "scripts"}

[tool.setuptools.packages.find]
where = ["scripts"]

[tool.pytest.ini_options]
addopts = "-ra"
testpaths = ["../../tests"]
```

Run: `uv lock --project skills/package-docker-app`

Expected: `uv.lock` is created without modifying system Python.

- [ ] **Step 2: Write failing model-contract tests**

```python
from pydantic import ValidationError
import pytest

from docker_package_app.models import EnvCandidate, Inspection, SourceRef, Stage


def test_inspection_round_trips_with_schema_version() -> None:
    inspection = Inspection(
        run_id="run-1",
        project_root="/workspace/app",
        stage=Stage.INSPECTED,
        env=(EnvCandidate(service="web", name="PORT", sources=(SourceRef(path="app.py", line=8),)),),
    )
    restored = Inspection.model_validate_json(inspection.model_dump_json())
    assert restored == inspection
    assert restored.schema_version == 1


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SourceRef(path="app.py", line=8, command="rm -rf data")
```

- [ ] **Step 3: Run the tests and verify the missing package failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_models.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'docker_package_app.models'`.

- [ ] **Step 4: Implement immutable Pydantic contracts and stable errors**

Use one strict base model and string enums:

```python
from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage(str, Enum):
    INSPECTED = "inspected"
    NEEDS_MODEL = "needs_model"
    PLANNED = "planned"
    CONFIRMED = "confirmed"
    BUILT = "built"
    EXPORTED = "exported"
    VERIFIED = "verified"
    PACKAGED = "packaged"
    FAILED = "failed"


class ImageAction(str, Enum):
    BUILD = "build"
    PACKAGE = "package"
    REUSE = "reuse"


class FileAction(str, Enum):
    COPY = "copy"
    KEEP_SERVER_PATH = "keep_server_path"


class SourceRef(StrictModel):
    path: str
    line: int | None = Field(default=None, ge=1)


class DefaultValue(StrictModel):
    value: str
    source: SourceRef


class EnvCandidate(StrictModel):
    service: str
    name: str
    defaults: tuple[DefaultValue, ...] = ()
    sources: tuple[SourceRef, ...] = ()


class BuildArgCandidate(StrictModel):
    service: str
    name: str
    default: str | None = None
    source: SourceRef


class PortCandidate(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None
    host_port: int | None = Field(default=None, ge=1, le=65535)


class ImageCandidate(StrictModel):
    service: str
    image: str | None = None
    has_build: bool = False


class FileCandidate(StrictModel):
    service: str
    compose_value: str
    resolved_path: str
    kind: Literal["bind", "config", "secret"]
    inside_project: bool
    estimated_size: int = Field(ge=0)


class EnvAssignment(StrictModel):
    service: str
    container_name: str
    artifact_name: str
    value: str


class PortAssignment(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    exposed: bool
    host_ip: str | None = None
    host_port: int | None = Field(default=None, ge=1, le=65535)


class ImageAssignment(StrictModel):
    service: str
    original_image: str | None = None
    final_image: str
    action: ImageAction
    platform: str


class FileAssignment(StrictModel):
    service: str
    original_value: str
    resolved_path: str
    action: FileAction
    payload_path: str | None = None


class BuildArgAssignment(StrictModel):
    service: str
    name: str
    value: str


class DiskEstimate(StrictModel):
    known_input_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    unknown_components: tuple[str, ...] = ()


class Inspection(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    project_root: str
    stage: Stage
    free_disk_bytes: int = Field(default=0, ge=0)
    dockerfiles: tuple[str, ...] = ()
    compose_files: tuple[str, ...] = ()
    env: tuple[EnvCandidate, ...] = ()
    build_args: tuple[BuildArgCandidate, ...] = ()
    ports: tuple[PortCandidate, ...] = ()
    images: tuple[ImageCandidate, ...] = ()
    files: tuple[FileCandidate, ...] = ()
    model_reasons: tuple[str, ...] = ()


class Question(StrictModel):
    id: str
    kind: Literal["env", "build_arg", "port_expose", "port_host", "image", "file", "choice", "confirm"]
    prompt: str
    default: str | None = None
    required: bool = True
    choices: tuple[str, ...] = ()


class AnswerBook(StrictModel):
    values: dict[str, str]


class PackagePlan(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    project_root: str
    app_name: str
    version: str
    compose_project_name: str
    platform: str = "linux/amd64"
    profiles: tuple[str, ...] = ()
    environment: tuple[EnvAssignment, ...] = ()
    ports: tuple[PortAssignment, ...] = ()
    images: tuple[ImageAssignment, ...] = ()
    files: tuple[FileAssignment, ...] = ()
    build_args: tuple[BuildArgAssignment, ...] = ()
    disk: DiskEstimate
    questions: tuple[Question, ...] = ()


class RunState(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    stage: Stage
    inspection: Inspection | None = None
    plan: PackagePlan | None = None
    plan_hash: str | None = None
    archive: str | None = None
```

In `errors.py`, define `PackageError(message, stage, hint=None)` and the five exit-code constants. In `__init__.py`, set `CLI_VERSION = "0.1.0"` and `ARTIFACT_SCHEMA_VERSION = 1`.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_models.py -v`

Expected: `2 passed`.

```bash
git add .gitignore skills/package-docker-app tests/unit/test_models.py
git commit -m "feat: scaffold docker packaging skill contracts"
```

### Task 2: Secure Workspace and Atomic State Files

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/workspace.py`
- Create: `tests/unit/test_workspace.py`

**Interfaces:**
- Consumes: Pydantic `StrictModel` from Task 1.
- Produces: `WorkPaths.create(project_root: Path, run_id: str) -> WorkPaths`.
- Produces: `atomic_write_model(path: Path, value: BaseModel) -> None`, `load_model(path: Path, model_type: type[T]) -> T`, `cleanup_run(paths: WorkPaths) -> None`.

- [ ] **Step 1: Write failing permission, atomicity, and cleanup tests**

```python
import stat
from pathlib import Path

from docker_package_app.models import Inspection, Stage
from docker_package_app.workspace import WorkPaths, atomic_write_model, cleanup_run, load_model


def test_workspace_is_private_and_keeps_generated_files(tmp_path: Path) -> None:
    paths = WorkPaths.create(tmp_path, "run-1")
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    assert paths.ignore_file.read_text() == "*\n!.gitignore\n"
    (paths.generated / "Dockerfile").write_text("FROM scratch\n")
    atomic_write_model(paths.state, Inspection(run_id="run-1", project_root=str(tmp_path), stage=Stage.INSPECTED))
    assert load_model(paths.state, Inspection).stage is Stage.INSPECTED
    assert stat.S_IMODE(paths.state.stat().st_mode) == 0o600
    cleanup_run(paths)
    assert not paths.run.exists()
    assert (paths.generated / "Dockerfile").exists()
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_workspace.py -v`

Expected: import fails for `docker_package_app.workspace`.

- [ ] **Step 3: Implement workspace paths and same-directory atomic replacement**

```python
@dataclass(frozen=True)
class WorkPaths:
    project_root: Path
    root: Path
    generated: Path
    work: Path
    run: Path
    dist: Path
    state: Path
    ignore_file: Path

    @classmethod
    def create(cls, project_root: Path, run_id: str) -> "WorkPaths":
        root = project_root.resolve() / ".docker-manage"
        generated, work, dist = root / "generated", root / "work", root / "dist"
        run = work / run_id
        for directory in (root, generated, work, run, dist):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        ignore_file = root / ".gitignore"
        if not ignore_file.exists():
            ignore_file.write_text("*\n!.gitignore\n", encoding="utf-8")
        return cls(project_root.resolve(), root, generated, work, run, dist, run / "state.json", ignore_file)
```

Implement `atomic_write_model` with `tempfile.NamedTemporaryFile(dir=path.parent, delete=False)`, `flush`, `os.fsync`, `chmod(0o600)`, and `os.replace`. Implement `load_model` with `model_type.model_validate_json`. `cleanup_run` may only call `shutil.rmtree(paths.run)` after verifying `paths.run.parent == paths.work` and `paths.work.parent == paths.root`.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_workspace.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/workspace.py tests/unit/test_workspace.py
git commit -m "feat: add secure packaging workspace"
```

### Task 3: Shell-Free Command Runner, Preflight, and File Discovery

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/command.py`
- Create: `skills/package-docker-app/scripts/docker_package_app/discovery.py`
- Create: `tests/unit/test_command.py`
- Create: `tests/unit/test_discovery.py`

**Interfaces:**
- Produces: `CommandRunner.run(argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None, check: bool = True) -> CommandResult`.
- Produces: `preflight(project_root: Path, runner: CommandRunner) -> PreflightReport`, including `free_disk_bytes` from `shutil.disk_usage`.
- Produces: `discover_docker_files(project_root: Path) -> DockerFileCandidates` and `default_identity(project_root: Path, runner: CommandRunner, now: datetime) -> tuple[str, str]`.

- [ ] **Step 1: Write failing tests for argv safety and deterministic candidates**

```python
def test_runner_never_uses_shell(monkeypatch):
    seen = {}
    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CommandRunner().run(["docker", "version"])
    assert result.stdout == "ok"
    assert seen["argv"] == ["docker", "version"]
    assert seen["kwargs"].get("shell") is not True


def test_discovery_returns_ambiguity_instead_of_guessing(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "Dockerfile.worker").write_text("FROM scratch\n")
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    result = discover_docker_files(tmp_path)
    assert result.dockerfiles == ("Dockerfile", "Dockerfile.worker")
    assert result.requires_dockerfile_choice is True
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_command.py tests/unit/test_discovery.py -v`

Expected: both modules are missing.

- [ ] **Step 3: Implement command and discovery boundaries**

`CommandRunner` must call `subprocess.run(list(argv), text=True, capture_output=True, cwd=cwd, env=merged_env, check=False)` and raise `PackageError` with a sanitized argv summary when `check=True` and the return code is nonzero.

`preflight` must execute these exact probes:

```python
PROBES = (
    ("docker daemon", ["docker", "version", "--format", "{{.Client.Version}}|{{.Server.Version}}"]),
    ("compose v2", ["docker", "compose", "version"]),
    ("buildx", ["docker", "buildx", "version"]),
)
```

Discovery must sort case-sensitive paths for `Dockerfile`, `Dockerfile.*`, `docker-compose.yml`, `docker-compose.yaml`, `compose.yml`, `compose.yaml`, and matching override files. It must ignore `.git`, `.docker-manage`, virtual environments, `node_modules`, and `dist`. Parse declared Compose profile names and return them as explicit candidates; never enable a profile without a selected `--profile`. Normalize app names to lowercase Docker-name characters and obtain the default version with `git rev-parse --short HEAD`; use `now.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")` when Git fails. `preflight` must capture `shutil.disk_usage(project_root).free` for plan display and later capacity checks.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_command.py tests/unit/test_discovery.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/command.py skills/package-docker-app/scripts/docker_package_app/discovery.py tests/unit/test_command.py tests/unit/test_discovery.py
git commit -m "feat: add deterministic project preflight"
```

### Task 4: Structured Compose Loading and Service Classification

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/compose.py`
- Create: `tests/unit/test_compose.py`
- Create: `tests/fixtures/multi-compose/compose.yaml`
- Create: `tests/fixtures/multi-compose/compose.override.yaml`

**Interfaces:**
- Consumes: `CommandRunner`.
- Produces: `ComposeDocument.load(project_root: Path, files: Sequence[Path], profiles: Sequence[str], runner: CommandRunner) -> ComposeDocument`.
- Produces: `ComposeDocument.services()`, `build_services()`, `image_services()`, and `dump(path: Path)`.

- [ ] **Step 1: Add a merged multi-service fixture and failing classification test**

Create `compose.yaml`:

```yaml
services:
  web:
    build:
      context: .
      dockerfile: Dockerfile
    image: example/web:dev
    environment:
      LOG_LEVEL: info
  worker:
    build: ./worker
  redis:
    image: redis:7
```

Create `compose.override.yaml`:

```yaml
services:
  web:
    environment:
      LOG_LEVEL: debug
```

```python
def test_compose_classifies_build_before_image(fake_compose_runner, fixture_dir):
    document = ComposeDocument.load(
        fixture_dir,
        [fixture_dir / "compose.yaml", fixture_dir / "compose.override.yaml"],
        [],
        fake_compose_runner,
    )
    assert document.build_services() == ("web", "worker")
    assert document.image_services() == ("redis",)
    assert document.data["services"]["web"]["environment"]["LOG_LEVEL"] == "debug"
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_compose.py -v`

Expected: missing `docker_package_app.compose`.

- [ ] **Step 3: Implement Compose merge through Compose v2 and YAML output**

Build this argv without a shell:

```python
argv = ["docker", "compose", "--project-directory", str(project_root)]
for file in files:
    argv.extend(["-f", str(file)])
for profile in profiles:
    argv.extend(["--profile", profile])
argv.extend(["config", "--format", "json", "--no-interpolate"])
```

Parse stdout with `json.loads`, validate that top-level `services` is a mapping, and preserve the merged dict. Treat any service containing `build` as a build service even when it also has `image`. Treat only services without `build` and with a nonempty `image` as image services. Write final Compose with `yaml.safe_dump(data, sort_keys=False, allow_unicode=True)` and immediately parse it again to prove structural validity.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_compose.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/compose.py tests/unit/test_compose.py tests/fixtures/multi-compose
git commit -m "feat: parse and classify compose projects"
```

### Task 5: Deterministic Environment Variable Discovery

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/envscan.py`
- Create: `tests/unit/test_envscan.py`
- Create: `tests/fixtures/env-project/`

**Interfaces:**
- Consumes: `ComposeDocument`, service source roots, selected Dockerfiles.
- Produces: `scan_environment(project_root: Path, service_roots: Mapping[str, Path], compose: ComposeDocument, dockerfiles: Mapping[str, Path]) -> tuple[EnvCandidate, ...]`.
- Produces: `scan_build_args(dockerfiles: Mapping[str, Path]) -> tuple[BuildArgCandidate, ...]`.
- Produces: `merge_defaults(candidates: Iterable[EnvCandidate]) -> tuple[EnvCandidate, ...]`.

- [ ] **Step 1: Write failing cross-language and conflict tests**

Create fixtures containing Python `os.getenv("PORT", "8000")`, Node `process.env.API_KEY`, Java `${LOG_LEVEL:info}`, Go `os.Getenv("CACHE_URL")`, `.env.example`, Dockerfile `ENV`, and Compose interpolation.

```python
def test_scanner_collects_only_explicit_env_reads(env_project):
    found = scan_environment(env_project.root, env_project.roots, env_project.compose, env_project.dockerfiles)
    keys = {(item.service, item.name) for item in found}
    assert keys >= {("web", "PORT"), ("web", "API_KEY"), ("worker", "CACHE_URL")}
    assert ("web", "HARDCODED_URL") not in keys


def test_conflicting_defaults_keep_all_sources(env_project):
    port = next(item for item in scan_environment(**env_project.args) if item.name == "PORT")
    assert {default.value for default in port.defaults} == {"8000", "8080"}


def test_required_dockerfile_arg_is_collected(env_project):
    args = scan_build_args(env_project.dockerfiles)
    assert [(item.name, item.default) for item in args] == [("PRIVATE_INDEX_URL", None), ("APP_MODE", "prod")]
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_envscan.py -v`

Expected: missing env scanner.

- [ ] **Step 3: Implement bounded scanners and exact merge rules**

Use compiled patterns only for explicit APIs:

```python
PATTERNS = {
    ".py": (r'os\.getenv\(["\']([A-Za-z_][A-Za-z0-9_]*)["\'](?:,\s*["\']([^"\']*)["\'])?',
            r'os\.environ(?:\.get\()?\s*\[?["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
    ".js": (r'process\.env\.([A-Za-z_][A-Za-z0-9_]*)', r'process\.env\[["\']([A-Za-z_][A-Za-z0-9_]*)["\']\]'),
    ".ts": (r'process\.env\.([A-Za-z_][A-Za-z0-9_]*)', r'process\.env\[["\']([A-Za-z_][A-Za-z0-9_]*)["\']\]'),
    ".java": (r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}',),
    ".go": (r'os\.(?:Getenv|LookupEnv)\(["`]([A-Za-z_][A-Za-z0-9_]*)["`]\)',),
    ".rb": (r'ENV\[["\']([A-Za-z_][A-Za-z0-9_]*)["\']\]',),
    ".php": (r'getenv\(["\']([A-Za-z_][A-Za-z0-9_]*)["\']\)',),
}
```

Skip `.git`, `.docker-manage`, `.venv`, `venv`, `node_modules`, `dist`, `build`, files over 2 MiB, and binary files. Parse env files with `dotenv_values`, Compose mappings/lists structurally, and Dockerfile `ENV key=value` plus `ARG name[=default]` with `shlex`. Deduplicate identical `(service, name, default, source)` records; never discard distinct defaults. Sort by service then variable name for stable prompts. Keep build args separate from runtime env; required build args get `Question(kind="build_arg")` and never enter deployment `.env` unless the same name is also an explicit runtime variable.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_envscan.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/envscan.py tests/unit/test_envscan.py tests/fixtures/env-project
git commit -m "feat: discover explicit runtime environment variables"
```

### Task 6: Questions, Defaults, Ports, Images, and Immutable Plans

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/questions.py`
- Create: `skills/package-docker-app/scripts/docker_package_app/planning.py`
- Create: `tests/unit/test_questions.py`
- Create: `tests/unit/test_planning.py`

**Interfaces:**
- Produces: `build_questions(inspection: Inspection) -> tuple[Question, ...]`.
- Produces: `parse_answer(question: Question, raw: str, *, chat_mode: bool) -> str`.
- Produces: `build_plan(inspection: Inspection, answers: AnswerBook, *, app_name: str, version: str, platform: str) -> PackagePlan`.

- [ ] **Step 1: Write failing interaction and collision tests**

```python
def test_default_and_explicit_empty_rules():
    with_default = Question(id="env.web.PORT", kind="env", prompt="PORT", default="8000")
    assert parse_answer(with_default, "默认", chat_mode=True) == "8000"
    assert parse_answer(with_default, "", chat_mode=False) == "8000"
    required = Question(id="env.web.API_KEY", kind="env", prompt="API_KEY")
    assert parse_answer(required, "<EMPTY>", chat_mode=True) == ""
    with pytest.raises(AnswerRequired):
        parse_answer(required, "", chat_mode=False)


def test_same_container_name_gets_service_prefixed_artifact_keys(inspection, answers):
    plan = build_plan(inspection, answers, app_name="demo", version="abc1234", platform="linux/amd64")
    env_values = {item.artifact_name: item.value for item in plan.environment}
    assert env_values["WEB_PORT"] == "8000"
    assert env_values["WORKER_PORT"] == "9000"


def test_duplicate_host_port_is_rejected(inspection_with_port_collision, answers):
    with pytest.raises(PlanValidationError, match="host port"):
        build_plan(inspection_with_port_collision, answers, app_name="demo", version="v1", platform="linux/amd64")
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_questions.py tests/unit/test_planning.py -v`

Expected: question modules are missing.

- [ ] **Step 3: Implement stable question IDs and plan validation**

Generate questions in this order: identity/platform/profile choices, conflicting or missing env values, build args, port exposure, host ports, third-party image decision, external file decision, final confirmation. Use IDs `env.<service>.<name>`, `buildarg.<service>.<name>`, `port.<service>.<container>/<protocol>.expose`, `port.<service>.<container>/<protocol>.host`, `image.<service>.decision`, and `file.<sha256-of-resolved-path>.decision`.

For image answers, accept the exact keyword `打包` or a Docker image reference matching lowercase/registry path syntax plus optional tag/digest. `打包` retains the original image and sets action `PACKAGE`; another valid reference sets action `REUSE`. Build services always receive `docker-manage/<app>/<service>:<version>`. Validate platform with `^linux/[a-z0-9_]+(?:/[a-z0-9_]+)?$` and reject duplicate `(host_ip or "0.0.0.0", host_port, protocol)` tuples.

When two services use the same container variable with different values, expose artifact keys `<NORMALIZED_SERVICE>_<VARIABLE>` and preserve the original container variable in the Compose mapping. If the values are identical, keep the unprefixed artifact key. Populate `PackagePlan.disk` with preflight free bytes, known local-file bytes, and unknown image/build components so the confirmation view never presents an unknown estimate as exact.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_questions.py tests/unit/test_planning.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/questions.py skills/package-docker-app/scripts/docker_package_app/planning.py tests/unit/test_questions.py tests/unit/test_planning.py
git commit -m "feat: build validated interactive packaging plans"
```

### Task 7: Validate the Model Supplement Boundary

**Files:**
- Create: `skills/package-docker-app/references/model-supplement.schema.json`
- Create: `skills/package-docker-app/scripts/docker_package_app/supplement.py`
- Create: `tests/unit/test_supplement.py`

**Interfaces:**
- Produces: `load_supplement(path: Path, project_root: Path, generated_root: Path, service_names: Collection[str]) -> ModelSupplement`.
- Produces: `merge_supplement(inspection: Inspection, supplement: ModelSupplement) -> Inspection`.

- [ ] **Step 1: Write failing schema and path-escape tests**

```python
def test_valid_supplement_adds_explicit_env(tmp_path, inspection):
    payload = {
        "schema_version": 1,
        "generated_files": [],
        "environment": [{"service": "web", "name": "API_URL", "default": None, "path": "app.py", "line": 4}],
        "ambiguities": [],
    }
    path = tmp_path / "supplement.json"
    path.write_text(json.dumps(payload))
    supplement = load_supplement(path, tmp_path, tmp_path / ".docker-manage/generated", {"web"})
    assert supplement.environment[0].name == "API_URL"


def test_generated_file_must_stay_in_generated_root(tmp_path, inspection):
    payload = valid_payload(generated_files=[{"kind": "dockerfile", "path": "../../Dockerfile"}])
    with pytest.raises(SupplementValidationError, match="generated"):
        load_payload(payload, tmp_path, {"web"})
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_supplement.py -v`

Expected: missing supplement module.

- [ ] **Step 3: Add the closed JSON schema and Pydantic validation**

The schema must set `additionalProperties: false` at every object level, allow generated-file kinds `dockerfile` and `compose`, and allow only:

```json
{
  "schema_version": 1,
  "generated_files": [{"kind": "dockerfile", "path": ".docker-manage/generated/Dockerfile"}],
  "environment": [{"service": "web", "name": "PORT", "default": "8000", "path": "app.py", "line": 8}],
  "ambiguities": [{"id": "startup.web", "prompt": "Choose the web startup command", "choices": ["uvicorn app:app", "python app.py"]}]
}
```

Validate generated paths with `Path.resolve().is_relative_to(generated_root.resolve())`, source paths with `is_relative_to(project_root.resolve())`, service names against the selected Compose services, env names against `^[A-Za-z_][A-Za-z0-9_]*$`, and lines as positive integers. Reject symlink escapes after resolution. Merge only validated env candidates and generated-file locations; ambiguities become `Question(kind="choice")` objects and never become command arguments.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_supplement.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/references/model-supplement.schema.json skills/package-docker-app/scripts/docker_package_app/supplement.py tests/unit/test_supplement.py
git commit -m "feat: validate model-generated project facts"
```

### Task 8: Collect and Rewrite Local File Dependencies

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/files.py`
- Create: `tests/unit/test_files.py`

**Interfaces:**
- Produces: `discover_file_dependencies(compose: ComposeDocument, project_root: Path) -> tuple[FileCandidate, ...]`.
- Produces: `materialize_files(candidates: Sequence[FileCandidate], decisions: Mapping[str, FileAction], payload_root: Path) -> FileMaterialization`.
- Produces: `FileMaterialization.rewrites: Mapping[str, str]` and `.server_paths: tuple[str, ...]`.

- [ ] **Step 1: Write failing project-boundary, symlink, and recursion tests**

```python
def test_project_relative_bind_is_copied_without_tool_state(tmp_path, compose_document):
    source = tmp_path / "config"
    source.mkdir()
    (source / "app.ini").write_text("mode=prod\n")
    (source / ".docker-manage").mkdir()
    (source / ".docker-manage/old.tar.gz").write_bytes(b"large")
    candidates = discover_file_dependencies(compose_document, tmp_path)
    result = materialize_files(candidates, {candidates[0].resolved_path: FileAction.COPY}, tmp_path / "payload")
    assert (tmp_path / "payload/files/config/app.ini").exists()
    assert not (tmp_path / "payload/files/config/.docker-manage").exists()


def test_symlink_outside_project_requires_server_path_decision(tmp_path, compose_document):
    outside = tmp_path.parent / "shared-secret"
    outside.write_text("secret")
    (tmp_path / "secret-link").symlink_to(outside)
    candidate = discover_file_dependencies(compose_document, tmp_path)[0]
    assert candidate.inside_project is False
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_files.py -v`

Expected: missing file dependency module.

- [ ] **Step 3: Implement structured reference discovery and safe copying**

Handle short and long bind syntax, top-level `configs.*.file`, and `secrets.*.file`. Distinguish named volumes by the absence of path separators and `.`/`~` prefixes. Resolve paths against the original Compose project directory, use resolved paths for containment checks, and estimate directory sizes without following symlinks outside the project.

Copy files with `shutil.copy2`; copy directories with `shutil.copytree(..., symlinks=True, ignore=ignore_docker_manage)`. Before adding each symlink to the archive, resolve it and reject it unless the target remains inside the copied source root. Map copied paths under `files/<project-relative-path>` and return POSIX relative rewrites. `KEEP_SERVER_PATH` leaves the original absolute value and records it in `server_paths`; an external candidate without an explicit decision raises `AnswerRequired`.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_files.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/files.py tests/unit/test_files.py
git commit -m "feat: package compose file dependencies safely"
```

### Task 9: Docker Build, Pull, Inspect, and Save Engine

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/docker.py`
- Create: `tests/unit/test_docker.py`
- Create: `tests/integration/fake_docker.py`

**Interfaces:**
- Consumes: `CommandRunner`, `ComposeDocument`, `PackagePlan`, `WorkPaths`.
- Produces: `DockerEngine.build(compose_path: Path, services: Sequence[str], platform: str) -> tuple[str, ...]`.
- Produces: `pull(image: str, platform: str)`, `inspect(images: Sequence[str]) -> tuple[ImageMetadata, ...]`, `ensure_export_space(images: Sequence[ImageMetadata], file_bytes: int, output_dir: Path) -> None`, `save(images: Sequence[str], output: Path) -> None`.

- [ ] **Step 1: Write failing exact-command tests with a recording runner**

```python
def test_pull_and_save_use_argv_without_shell(recording_runner, tmp_path):
    engine = DockerEngine(recording_runner)
    engine.pull("redis:7", "linux/amd64")
    engine.save(["demo/web:v1", "redis:7"], tmp_path / "images.tar")
    assert recording_runner.argv == [
        ["docker", "pull", "--platform", "linux/amd64", "redis:7"],
        ["docker", "image", "save", "--output", str(tmp_path / "images.tar"), "demo/web:v1", "redis:7"],
    ]


def test_build_uses_temporary_compose_and_selected_services(recording_runner, tmp_path):
    DockerEngine(recording_runner).build(tmp_path / "build.compose.yaml", ["web", "worker"], "linux/amd64")
    assert recording_runner.argv[-1] == [
        "docker", "compose", "-f", str(tmp_path / "build.compose.yaml"), "build", "web", "worker"
    ]
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_docker.py -v`

Expected: missing Docker engine.

- [ ] **Step 3: Implement Docker operations and platform verification**

Before building, copy each selected Dockerfile byte-for-byte into the run directory and create `<name>.dockerignore` by concatenating existing project `.dockerignore`, the selected Dockerfile-specific ignore file, and final rules `\n.docker-manage/\n`. Rewrite only the temporary build Compose to point at that copied Dockerfile, set each build service `image` to its planned tag, and set `platform` to the target.

Use `docker compose -f <temporary-compose> build <services...>`, `docker pull --platform`, `docker image inspect --format '{{json .}}'`, and one `docker image save --output`. Parse inspect JSON into `ImageMetadata(reference, image_id, repo_digests, os, architecture, variant, size)` and require `os/architecture[/variant]` to match the plan. Before saving, query `shutil.disk_usage(output.parent).free` and require at least `2 * sum(image.size) + file_bytes + 268_435_456` bytes for the uncompressed image tar, gzip output, copied files, and a 256 MiB reserve. Raise a staged disk-space error showing required and free bytes when the check fails. If no images are packaged, do not call `docker image save`.

- [ ] **Step 4: Add fake executable integration coverage**

`tests/integration/fake_docker.py` must be executable, append `sys.argv[1:]` as JSON lines to `FAKE_DOCKER_LOG`, emit configurable inspect JSON, write a valid tar header for `image save`, and return the integer in `FAKE_DOCKER_EXIT`. Test that nonzero build and pull results raise `PackageError` with stage and hint while preserving the full stderr in the exception object but not interpolating it into shell text.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_docker.py tests/integration -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/docker.py tests/unit/test_docker.py tests/integration/fake_docker.py
git commit -m "feat: add deterministic docker image engine"
```

### Task 10: Render and Validate the Deployment Compose

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/render.py`
- Create: `tests/unit/test_render.py`

**Interfaces:**
- Produces: `render_deployment(base: ComposeDocument, plan: PackagePlan, file_rewrites: Mapping[str, str]) -> dict[str, Any]`.
- Produces: `write_deployment(compose_data: Mapping[str, Any], env_values: Mapping[str, str], output_dir: Path) -> tuple[Path, Path]`.
- Produces: `validate_deployment(compose_path: Path, env_path: Path, runner: CommandRunner) -> None`.

- [ ] **Step 1: Write a failing end-state test**

```python
def test_rendered_compose_is_deploy_only(base_compose, plan, tmp_path, recording_runner):
    rendered = render_deployment(base_compose, plan, {"./config": "./files/config"})
    assert all("build" not in service for service in rendered["services"].values())
    assert rendered["services"]["web"]["image"] == "docker-manage/demo/web:v1"
    assert rendered["services"]["web"]["environment"]["PORT"] == "${WEB_PORT}"
    assert rendered["services"]["web"]["volumes"][0]["source"] == "./files/config"
    compose_path, env_path = write_deployment(rendered, {"WEB_PORT": "8000", "API_KEY": "a b#c"}, tmp_path)
    validate_deployment(compose_path, env_path, recording_runner)
    assert "API_KEY='a b#c'" in env_path.read_text()
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_render.py -v`

Expected: missing render module.

- [ ] **Step 3: Implement deep-copy transformations and dotenv quoting**

Deep-copy the merged Compose dict. For every service, remove `build`, apply the planned image and platform, replace ports with normalized long syntax, replace runtime environment with container-name to `${ARTIFACT_NAME}` mappings, and apply file rewrites to bind/config/secret sources. Preserve networks, named volumes, health checks, dependencies, restart policy, profiles, labels, capabilities, and unrelated keys.

Write YAML through `yaml.safe_dump(sort_keys=False, allow_unicode=True)`. Write `.env` with `dotenv.set_key(path, key, value, quote_mode="always")` in sorted key order and chmod both files to `0o600`. Validate with:

```python
["docker", "compose", "--env-file", str(env_path), "-f", str(compose_path), "config"]
```

Reject validation warnings about unset variables as errors by scanning combined stdout/stderr for `variable is not set`.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_render.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/render.py tests/unit/test_render.py
git commit -m "feat: render validated deployment compose"
```

### Task 11: Manifest, Checksums, and Atomic Archive

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/artifact.py`
- Create: `tests/unit/test_artifact.py`

**Interfaces:**
- Produces: `build_manifest(plan: PackagePlan, image_metadata: Sequence[ImageMetadata], payload_root: Path) -> Manifest`.
- Produces: `write_checksums(payload_root: Path) -> Path`.
- Produces: `create_verified_archive(payload_root: Path, destination: Path) -> Path`.

- [ ] **Step 1: Write failing reproducibility, tamper, and traversal tests**

```python
def test_archive_contains_verified_payload(payload, tmp_path):
    destination = tmp_path / "demo-v1.tar.gz"
    result = create_verified_archive(payload, destination)
    assert result == destination
    assert result.exists()
    with tarfile.open(result, "r:gz") as archive:
        assert set(archive.getnames()) >= {"manifest.json", "compose.yaml", ".env", "checksums.sha256"}


def test_verifier_rejects_checksum_tampering(payload, tmp_path):
    checksums = write_checksums(payload)
    (payload / "compose.yaml").write_text("services: {}\n")
    with pytest.raises(ArtifactVerificationError, match="checksum"):
        verify_payload(payload, checksums)


def test_archive_rejects_symlink_escape(payload, tmp_path):
    (payload / "files/outside").symlink_to("../../outside")
    with pytest.raises(ArtifactVerificationError, match="symlink"):
        create_verified_archive(payload, tmp_path / "bad.tar.gz")
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_artifact.py -v`

Expected: missing artifact module.

- [ ] **Step 3: Implement streaming hashes and post-write verification**

Hash files in 1 MiB chunks. `manifest.json` records schema version, CLI version, identity, platform, timestamps, service image mapping, packaged image metadata, reused images, server paths, and payload file hashes excluding manifest/checksum files. `checksums.sha256` covers every regular payload file except itself, including `manifest.json`.

Walk files in sorted POSIX-path order. Reject absolute member names, `..` components, device files, FIFOs, and symlinks whose resolved target exits payload root. Write `<destination>.partial-<uuid>` with mode `0o600`, reopen it with `tarfile`, re-check every member and extracted stream hash, then use `os.replace` to publish the destination. Delete partial files on every exception.

- [ ] **Step 4: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_artifact.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/artifact.py tests/unit/test_artifact.py
git commit -m "feat: create verified docker deployment archives"
```

### Task 12: Wire the Versioned CLI State Machine

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/cli.py`
- Create: `tests/integration/test_cli.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: all CLI modules from Tasks 1-11.
- Produces: console command `docker-package-app` with `inspect`, `plan`, `package`, and `run` subcommands.
- Produces: machine-readable JSON on stdout and human diagnostics on stderr.

- [ ] **Step 1: Write failing subprocess tests for state and exit codes**

```python
def test_inspect_missing_docker_files_requests_model(cli, empty_project):
    result = cli("inspect", str(empty_project), "--json")
    assert result.returncode == 20
    body = json.loads(result.stdout)
    assert body["stage"] == "needs_model"
    assert set(body["model_reasons"]) == {"dockerfile_missing", "compose_missing"}


def test_noninteractive_plan_rejects_missing_answers(cli, compose_project):
    inspected = cli("inspect", str(compose_project), "--json")
    run_id = json.loads(inspected.stdout)["run_id"]
    result = cli("plan", str(compose_project), "--run-id", run_id, "--non-interactive", "--answers", "answers.json")
    assert result.returncode == 10
    assert "missing answer" in result.stderr.lower()


def test_dry_run_stops_before_docker_mutations(cli, compose_project, fake_docker_log):
    result = cli("run", str(compose_project), "--dry-run", "--non-interactive", "--answers", complete_answers(compose_project))
    assert result.returncode == 0
    assert all(call[0:2] not in (["docker", "pull"], ["docker", "image"]) for call in fake_docker_log())
```

- [ ] **Step 2: Verify failure**

Run: `uv run --project skills/package-docker-app pytest tests/integration/test_cli.py -v`

Expected: CLI entry point cannot import `docker_package_app.cli`.

- [ ] **Step 3: Implement argparse commands and transitions**

Define shared flags `project`, `--json`, `--run-id`, `--app-name`, `--version`, `--platform`, `--profile` (repeatable), `--compose-file` (repeatable), `--dockerfile`, `--supplement`, and `--keep-work`. `plan`, `package`, and `run` accept `--answers` and `--non-interactive`; `package` requires `--confirm-plan-hash`; `run` also accepts `--dry-run`.

Persist state after every transition with `atomic_write_model`. Permit only:

```python
ALLOWED_TRANSITIONS = {
    Stage.INSPECTED: {Stage.NEEDS_MODEL, Stage.PLANNED, Stage.FAILED},
    Stage.NEEDS_MODEL: {Stage.INSPECTED, Stage.FAILED},
    Stage.PLANNED: {Stage.CONFIRMED, Stage.FAILED},
    Stage.CONFIRMED: {Stage.BUILT, Stage.FAILED},
    Stage.BUILT: {Stage.EXPORTED, Stage.FAILED},
    Stage.EXPORTED: {Stage.VERIFIED, Stage.FAILED},
    Stage.VERIFIED: {Stage.PACKAGED, Stage.FAILED},
}
```

`inspect` creates a run and returns its `run_id`; `plan` and `package` must receive the same ID and load the existing state. `inspect` and `plan` preserve the run directory because another subcommand consumes it. `package` and the one-process `run` command clean it after success, failure, or cancellation unless `--keep-work` is explicitly passed. Reject a `--confirm-plan-hash` that differs from SHA-256 of the canonical stored plan JSON before any Docker mutation.

In terminal mode, call `input()` once per `Question`, print full defaults, parse blank as default, and re-prompt required blank values. In JSON mode, stdout must contain only one JSON document. Map `PackageError` subclasses to the five stable exit codes.

- [ ] **Step 4: Run CLI tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/integration/test_cli.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/scripts/docker_package_app/cli.py tests/conftest.py tests/integration/test_cli.py
git commit -m "feat: add docker packaging cli workflow"
```

### Task 13: Write the Codex Skill Workflow and Validate Metadata

**Required Skills:** Read and follow `skill-creator` and `superpowers:writing-skills` before editing the skill files in this task.

**Files:**
- Replace: `skills/package-docker-app/SKILL.md`
- Regenerate: `skills/package-docker-app/agents/openai.yaml`
- Create: `tests/unit/test_skill_contract.py`

**Interfaces:**
- Consumes: CLI exit codes and JSON contracts from Task 12.
- Produces: an invocable `$package-docker-app` workflow that never bypasses the CLI.

- [ ] **Step 1: Write failing skill-contract tests**

```python
def test_skill_declares_fixed_cli_and_model_boundary(skill_text):
    assert "uv run --project" in skill_text
    assert "EXIT_MODEL_REQUIRED=20" in skill_text
    assert ".docker-manage/generated" in skill_text
    assert "Do not modify existing project files" in skill_text
    assert "Do not run Docker build, pull, save, or archive commands directly" in skill_text


def test_skill_has_no_template_markers(skill_text):
    for marker in ("[TO" + "DO", "TO" + "DO:", "T" + "BD", "PLACE" + "HOLDER"):
        assert marker not in skill_text
```

- [ ] **Step 2: Verify the generated template fails the contract**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -v`

Expected: assertions fail because the initialized template lacks the workflow.

- [ ] **Step 3: Replace `SKILL.md` with the exact orchestration contract**

Use frontmatter containing only:

```yaml
---
name: package-docker-app
description: Inspect a local application, generate missing Dockerfile or Compose configuration without changing existing project files, collect environment variables and ports, build platform-specific Docker images, and create a verified Docker Manage deployment archive. Use when a user asks Codex to package, export, or prepare a local Docker or Docker Compose application for transfer to an offline server.
---
```

The body must instruct the agent to:

1. Resolve `SKILL_DIR` from the loaded skill and run `uv run --project "$SKILL_DIR" docker-package-app inspect <project> --json`; retain the returned `run_id`.
2. Treat exit `0` as inspected, `10` as answers required, `20` as model supplement required, and all other nonzero results as failures.
3. On exit `20`, read only the necessary source files, create missing files only under `.docker-manage/generated/`, write JSON matching `references/model-supplement.schema.json`, and rerun `inspect --run-id <run_id> --supplement <path>`.
4. Ask every returned question in order. In chat, accept the literal reply `默认`; never pretend a blank chat message was received.
5. Show full secret defaults as required by the approved trust model.
6. Run `plan --run-id <run_id> --answers <private-json> --json`, present the resulting plan and `plan_hash`, and require explicit confirmation.
7. Run `package --run-id <run_id> --answers <private-json> --confirm-plan-hash <hash> --json`; never run Docker build, pull, save, Compose mutation, or archive commands directly.
8. Report the final archive path, size, checksum, packaged images, reused server images, and server-path requirements.
9. Preserve all existing project files and never infer environment variables from hardcoded constants.

- [ ] **Step 4: Regenerate UI metadata and run validation**

Run:

```bash
uv run --with pyyaml python /Users/wenjuhao/.codex-company/skills/.system/skill-creator/scripts/generate_openai_yaml.py \
  skills/package-docker-app \
  --interface 'display_name=Package Docker App' \
  --interface 'short_description=Build portable Docker deployment archives' \
  --interface 'default_prompt=Use $package-docker-app to inspect this project and build a portable Docker deployment archive.'
```

Run:

```bash
uv run --with pyyaml python /Users/wenjuhao/.codex-company/skills/.system/skill-creator/scripts/quick_validate.py skills/package-docker-app
```

Expected: `Skill is valid!`

- [ ] **Step 5: Run tests and commit**

Run: `uv run --project skills/package-docker-app pytest tests/unit/test_skill_contract.py -v`

Expected: PASS.

```bash
git add skills/package-docker-app/SKILL.md skills/package-docker-app/agents/openai.yaml tests/unit/test_skill_contract.py
git commit -m "feat: add docker packaging codex skill"
```

### Task 14: End-to-End Fixtures, Real Docker Smoke Test, and Final Verification

**Files:**
- Create: `tests/fixtures/no-docker-python/app.py`
- Create: `tests/fixtures/no-docker-python/requirements.txt`
- Create: `tests/fixtures/scratch-compose/Dockerfile`
- Create: `tests/fixtures/scratch-compose/compose.yaml`
- Create: `tests/e2e/test_package_flow.py`
- Create: `tests/smoke/test_real_docker.py`

**Interfaces:**
- Consumes: installed CLI and skill from Tasks 1-13.
- Produces: repeatable non-Codex E2E proof and opt-in Docker daemon proof.

- [ ] **Step 1: Write the failing fake-Docker end-to-end test**

```python
def test_multi_service_package_is_complete(cli, multi_compose_project, complete_answers, fake_docker_env):
    result = cli(
        "run", str(multi_compose_project),
        "--non-interactive", "--answers", str(complete_answers),
        "--app-name", "demo", "--version", "v1", "--platform", "linux/amd64",
        "--json",
        env=fake_docker_env,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    archive = Path(output["archive"])
    with tarfile.open(archive, "r:gz") as bundle:
        compose = yaml.safe_load(bundle.extractfile("compose.yaml"))
        assert all("build" not in service for service in compose["services"].values())
        assert bundle.getmember("manifest.json").size > 0
        assert bundle.getmember("checksums.sha256").size > 0
```

- [ ] **Step 2: Implement fixture answer files and make E2E pass**

The multi-service answer file must choose different `PORT` values for `web` and `worker`, reuse a server Redis image, package one other third-party image, remove one host-port mapping, and copy one relative config file. Extend `tests/integration/fake_docker.py` only with deterministic responses needed by this flow.

Run: `uv run --project skills/package-docker-app pytest tests/e2e/test_package_flow.py -v`

Expected: PASS and an archive under the fixture copy's `.docker-manage/dist/`.

- [ ] **Step 3: Add an opt-in daemon-backed scratch-image smoke test**

Use this Dockerfile so the test does not require a registry pull:

```dockerfile
FROM scratch
LABEL io.docker-manage.smoke="true"
```

The test must skip unless `RUN_DOCKER_SMOKE=1`, require a reachable daemon, package `scratch-compose`, capture the image ID, run `docker image rm <generated-tag>`, run `docker image load --input <extracted-images.tar>`, assert the restored ID matches, and remove only the uniquely tagged smoke image in `finally`.

Run: `RUN_DOCKER_SMOKE=1 uv run --project skills/package-docker-app pytest tests/smoke/test_real_docker.py -v`

Expected with a running daemon: PASS. If Docker Desktop is stopped, first start it and rerun; do not treat a skipped or daemon-failed test as evidence that the smoke test passed.

- [ ] **Step 4: Run the complete static and automated verification suite**

Run:

```bash
uv run --project skills/package-docker-app ruff check skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app pytest tests/unit tests/integration tests/e2e -v --cov=docker_package_app --cov-report=term-missing --cov-fail-under=85
uv run --with pyyaml python /Users/wenjuhao/.codex-company/skills/.system/skill-creator/scripts/quick_validate.py skills/package-docker-app
git diff --check
```

Expected: Ruff exits 0, all non-smoke tests pass, coverage is at least 85%, skill validation prints `Skill is valid!`, and `git diff --check` is silent.

- [ ] **Step 5: Perform Codex behavior acceptance after linking the skill**

Link `skills/package-docker-app` into the active Codex skills directory without replacing an existing path. Start a fresh Codex conversation in a disposable copy of `tests/fixtures/no-docker-python` with this prompt:

```text
Use $package-docker-app to package this project for linux/amd64. Accept the displayed defaults, but stop before the final build confirmation and show me the plan.
```

Verify that Codex creates missing Docker files only under `.docker-manage/generated/`, uses the supplement schema, asks each variable/port question, and stops at confirmation without issuing Docker mutation commands. Remove only the disposable fixture copy after inspection.

- [ ] **Step 6: Commit the complete verification surface**

```bash
git add tests/fixtures tests/e2e tests/smoke
git commit -m "test: verify docker packaging workflow end to end"
```

## Completion Evidence

Before reporting implementation complete, record these outputs in the final handoff:

- `git log --oneline` showing one commit per task boundary.
- Non-smoke pytest count and coverage percentage.
- Real Docker smoke-test result, including Docker client/server versions and target platform.
- Skill validator output.
- Codex behavior acceptance observations.
- One generated archive path, SHA-256, packaged-image list, reused-image list, and server-path requirements.
