# Compose Interpolated Port Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Compose interpolation operators in IPv4 short port syntax from being misparsed as port delimiters, then prove a user-selected `8322 -> 8000/tcp` mapping packages and validates successfully.

**Architecture:** Keep Compose input un-interpolated during inspection, but replace the raw `rsplit(":", 2)` with a small scanner that splits only colons outside `${...}`. Continue treating non-literal host-port expressions as unknown so the existing `port_host` question supplies the final published port; leave planning and rendering unchanged.

**Tech Stack:** Python 3.11+, Pydantic 2, PyYAML 6, pytest 8, Docker Compose v2+, Ruff

## Global Constraints

- Work directly on the current `main` branch as explicitly authorized by the user.
- Support `CONTAINER`, `HOST:CONTAINER`, and `IPV4:HOST:CONTAINER` short syntax only; do not add IPv6-specific parsing.
- Do not implement Compose variable evaluation or extract defaults from `${VAR:-default}`.
- Protect `:-`, `:?`, and `:+` interpolation operators from delimiter splitting.
- Preserve the existing long-syntax model, planning questions, and deployment rendering contract.
- Use TDD: both regression surfaces must fail before production code changes.

---

### Task 1: Fix interpolation-aware short port parsing and verify the package workflow

**Files:**
- Create: `tests/unit/test_port_parsing.py`
- Modify: `tests/e2e/test_package_flow.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py:643-671`

**Interfaces:**
- Consumes: `_compose_port(service: str, raw: object) -> PortCandidate | None` and the existing `CliRunner` fake-Docker package workflow.
- Produces: `_split_short_port(value: str) -> tuple[str, ...] | None`, returning one to three top-level IPv4 short-syntax fields without splitting colons inside `${...}`.

- [ ] **Step 1: Add direct unit regressions for interpolation operators and IPv4 literals**

Create `tests/unit/test_port_parsing.py` with:

```python
import pytest

from docker_package_app.cli import _compose_port


@pytest.mark.parametrize(
    "raw",
    (
        "${PDF_TRANS_WEB_PORT:-8000}:8000",
        "${PDF_TRANS_WEB_PORT:?error}:8000",
        "${PDF_TRANS_WEB_PORT:+8322}:8000",
    ),
)
def test_compose_port_ignores_colons_inside_interpolation(raw: str) -> None:
    candidate = _compose_port("web", raw)

    assert candidate is not None
    assert candidate.container_port == 8000
    assert candidate.protocol == "tcp"
    assert candidate.host_ip is None
    assert candidate.host_port is None


@pytest.mark.parametrize(
    ("raw", "host_ip", "host_port"),
    (
        ("8322:8000", None, 8322),
        ("127.0.0.1:8322:8000", "127.0.0.1", 8322),
    ),
)
def test_compose_port_preserves_ipv4_short_syntax(
    raw: str,
    host_ip: str | None,
    host_port: int,
) -> None:
    candidate = _compose_port("web", raw)

    assert candidate is not None
    assert candidate.container_port == 8000
    assert candidate.protocol == "tcp"
    assert candidate.host_ip == host_ip
    assert candidate.host_port == host_port
```

- [ ] **Step 2: Add the inspect-to-package regression with real Compose validation**

In `tests/e2e/test_package_flow.py`, add `subprocess` and `pytest` imports, the availability helper, and this test:

```python
def _has_docker_compose() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    result = subprocess.run(
        [docker, "compose", "version"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    not _has_docker_compose(),
    reason="Docker Compose is required for generated Compose validation",
)
def test_interpolated_host_port_packages_user_selected_mapping(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    project = tmp_path / "interpolated-port-app"
    project.mkdir()
    (project / "Dockerfile").write_text(
        "FROM scratch\nEXPOSE 8000\n",
        encoding="utf-8",
    )
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (project / "compose.yaml").write_text(
        """services:
  web:
    build: .
    ports:
      - "${PDF_TRANS_WEB_PORT:-8000}:8000"
""",
        encoding="utf-8",
    )
    answers = project / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "values": {
                    "env.web.PDF_TRANS_WEB_PORT": "8000",
                    "port.web.8000/tcp.expose": "yes",
                    "port.web.8000/tcp.host": "8322",
                }
            }
        ),
        encoding="utf-8",
    )
    answers.chmod(0o600)

    result = cli(
        "run",
        str(project),
        "--non-interactive",
        "--answers",
        str(answers),
        "--app-name",
        "interpolated-port",
        "--version",
        "v1",
        "--json",
        env={"FAKE_DOCKER_INSPECT": json.dumps([_metadata("sha256:web")])},
    )

    assert result.returncode == 0, result.stderr
    archive_path = Path(json.loads(result.stdout)["archive"])
    validation_dir = tmp_path / "compose-validation"
    validation_dir.mkdir()
    with tarfile.open(archive_path, "r:gz") as bundle:
        compose_text = bundle.extractfile("compose.yaml").read().decode("utf-8")
        env_text = bundle.extractfile(".env").read().decode("utf-8")

    compose = yaml.safe_load(compose_text)
    assert compose["services"]["web"]["ports"] == [
        {"target": 8000, "published": 8322, "protocol": "tcp"}
    ]
    assert "host_ip: ${PDF_TRANS_WEB_PORT" not in compose_text

    compose_path = validation_dir / "compose.yaml"
    env_path = validation_dir / ".env"
    compose_path.write_text(compose_text, encoding="utf-8")
    env_path.write_text(env_text, encoding="utf-8")
    docker = shutil.which("docker")
    assert docker is not None
    validated = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(compose_path),
            "config",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert validated.returncode == 0, validated.stderr
```

- [ ] **Step 3: Run both regression surfaces and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest \
  tests/unit/test_port_parsing.py \
  tests/e2e/test_package_flow.py::test_interpolated_host_port_packages_user_selected_mapping \
  -v
```

Expected: the three interpolation unit cases fail because `host_ip` is `${PDF_TRANS_WEB_PORT`, and the end-to-end case fails because real `docker compose config` rejects the truncated interpolation in `host_ip`.

- [ ] **Step 4: Implement the minimal interpolation-aware IPv4 splitter**

In `skills/package-docker-app/scripts/docker_package_app/cli.py`, replace `parts = value.rsplit(":", 2)` with a checked helper call and add the helper after `_compose_port()`:

```python
    parts = _split_short_port(value)
    if parts is None:
        return None
    target = _integer_port(parts[-1])
```

```python
def _split_short_port(value: str) -> tuple[str, ...] | None:
    parts: list[str] = []
    start = 0
    interpolation_depth = 0
    index = 0
    while index < len(value):
        if value.startswith("${", index):
            interpolation_depth += 1
            index += 2
            continue
        character = value[index]
        if character == "}" and interpolation_depth:
            interpolation_depth -= 1
        elif character == ":" and interpolation_depth == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    if interpolation_depth:
        return None
    parts.append(value[start:])
    return tuple(parts) if 1 <= len(parts) <= 3 else None
```

Do not change `_integer_port()`, planning, questions, or rendering.

- [ ] **Step 5: Run the focused regressions and verify GREEN**

Run the same command from Step 3.

Expected: `6 passed` with no failures; the generated Compose passes the real `docker compose config` call embedded in the end-to-end test.

- [ ] **Step 6: Run the complete quality gate**

Run:

```bash
uv run --project skills/package-docker-app ruff check \
  skills/package-docker-app/scripts tests
uv run --project skills/package-docker-app pytest \
  tests/unit tests/integration tests/e2e \
  -v --cov=docker_package_app --cov-report=term-missing --cov-fail-under=85
uv run --with pyyaml python \
  /Users/wenjuhao/.codex-company/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/package-docker-app
git diff --check
```

Expected: Ruff exits zero, the full non-smoke suite passes with at least 85% coverage, skill validation reports success, and `git diff --check` emits no output.

- [ ] **Step 7: Commit the tested fix on `main`**

```bash
git add \
  skills/package-docker-app/scripts/docker_package_app/cli.py \
  tests/unit/test_port_parsing.py \
  tests/e2e/test_package_flow.py
git commit -m "fix: parse interpolated compose host ports"
```
