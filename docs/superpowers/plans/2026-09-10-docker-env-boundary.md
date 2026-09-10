# Docker Environment Boundary Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This task explicitly requires inline execution in the current workspace; do not create a branch, worktree, or subagent.

**Goal:** Prevent image-internal Dockerfile `ENV` values such as `PATH` from becoming deployment configuration while preserving explicit Compose/application variables and making generated environment files safe for both Compose and `docker run`.

**Architecture:** Keep the existing inspect → questions → plan → render → snapshot pipeline and question IDs unchanged. At the scanner boundary, treat source reads and Compose `environment`/`env_file`/other interpolation as deployment declarations, while dropping candidates that originate only from Dockerfile `ENV`. Centralize raw env-file writing and render unsafe values literally in Compose so the shared `.env` remains compatible with Docker's non-interpolating `--env-file` parser.

**Tech Stack:** Python 3.11+, pytest, Pydantic models, PyYAML, python-dotenv only for reading existing env files, Docker Compose CLI for integration validation.

**Spec:** User request in the current conversation; no separate design document.

## Global Constraints

- Preserve existing CLI commands, question IDs, plan hash computation, state transitions, current configuration snapshot schema, and archive layout.
- Do not change project Dockerfiles, Compose files, source files, or install new tools.
- Do not expose secrets; regression assertions use synthetic values and names only.
- Keep explicit Compose environment configuration even when the same name appears in a Dockerfile `ENV` instruction.
- Run `uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app pytest -q` before completion.

### Task 1: Define the Dockerfile ENV deployment boundary

**Files:**
- Modify: `tests/unit/test_envscan.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/envscan.py`

**Interfaces:**
- Preserve `scan_environment(...) -> tuple[EnvCandidate, ...]` and all `EnvCandidate`/`Inspection` schemas.
- Add no new CLI-visible fields or question IDs.

- [x] **Step 1: Write failing scanner tests**

Add tests proving a Dockerfile-only `ENV PATH=...` is absent, while a same-name Compose `environment` or `env_file` declaration remains present and keeps the Compose value.

- [x] **Step 2: Run the focused tests and verify the expected failure**

Run `uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app pytest -q tests/unit/test_envscan.py`; the existing scanner behavior must expose the Dockerfile-only variable before implementation.

- [x] **Step 3: Implement the smallest source-aware filter**

Collect source/Compose candidates separately from Dockerfile candidates. Merge all non-Dockerfile candidates; append Dockerfile candidates only for `(service, name)` keys absent from the non-Dockerfile set. This drops Dockerfile-only image defaults without changing source reads, Compose environment values, env-file values, or Compose interpolation candidates.

- [x] **Step 4: Run the focused tests and verify they pass**

Run the same focused command and confirm both new tests and existing scanner tests pass.

### Task 2: Lock the end-to-end PATH exclusion and explicit business variable behavior

**Files:**
- Modify: `tests/unit/test_planning.py`
- Modify: `tests/unit/test_render.py`
- Modify: `tests/unit/test_current_config.py`
- Modify: `tests/e2e/test_package_flow.py`

**Interfaces:**
- Continue building plans from `Inspection` and answers through `build_plan(...)`.
- Keep `render_deployment(...)`, `write_deployment(...)`, and `write_current_environment(...)` signatures compatible.

- [x] **Step 1: Add failing plan/render/snapshot regressions**

Cover that an inspection produced from a Dockerfile with only `ENV PATH=...` creates no environment question or plan assignment; generated Compose and deployment `.env` contain no `PATH`; a stale `.docker-manage/.env` PATH line is not carried into the next current snapshot; and a Compose-declared business variable still appears in the plan and generated Compose.

- [x] **Step 2: Run the focused regressions and verify failures identify the missing behavior**

Run the named tests individually. Failures should be limited to the new expectations, not test setup errors.

- [x] **Step 3: Implement only the scanner-to-plan/render plumbing needed by the tests**

Rely on the Task 1 candidate boundary for normal PATH exclusion; keep plan construction and question IDs unchanged. Ensure render starts from the plan environment only, and current snapshots are rewritten from the current assignments rather than merged with historical keys.

- [x] **Step 4: Run the focused regressions and verify they pass**

Run the focused unit/e2e tests and confirm existing ports, mounts, images, and business environment assertions remain green.

### Task 3: Make generated env files Docker-compatible without changing value semantics

**Files:**
- Create: `skills/package-docker-app/scripts/docker_package_app/envfile.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/render.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/current_config.py`
- Modify: `tests/unit/test_render.py`
- Modify: `tests/unit/test_current_config.py`

**Interfaces:**
- Add a small shared writer used by deployment and current snapshots; keep their public functions unchanged.
- Keep `dotenv_values` for reading legacy/current snapshots so existing quoted snapshots remain compatible.

- [x] **Step 1: Add failing serialization tests**

Use synthetic values containing spaces, colons, Chinese text, `#`, quotes, and `${OTHER}`. Assert the emitted env file has no shell-style wrapper quotes, preserves the exact raw value for Docker env-file parsing, and that generated Compose still receives the exact unsafe value by using a literal YAML environment value when Compose interpolation would reinterpret it.

- [x] **Step 2: Run the serialization tests and verify they fail against `quote_mode="always"`**

Run `uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app pytest -q tests/unit/test_render.py tests/unit/test_current_config.py`; the current output should show quoted values and Compose interpolation of `${...}`.

- [x] **Step 3: Implement the shared raw writer and safe Compose rendering rule**

Write sorted `NAME=value` lines without adding single/double quotes, reject newline-containing values because `docker run --env-file` cannot represent them losslessly, and render values containing `$`, quotes, backslashes, comment-significant `#`, or boundary whitespace literally in Compose. Keep safe values on the existing `${artifact_name}` path so current environment naming and Compose semantics remain stable.

- [x] **Step 4: Run serialization tests and verify both deployment and snapshot paths pass**

Confirm legacy quoted snapshots still parse through `dotenv_values`, while newly written files contain exact raw synthetic values and no accidental interpolation markers.

### Task 4: Full verification and compatibility audit

**Files:**
- Modify: only files required by test failures found during verification.

- [x] **Step 1: Run the full skill test suite**

Run `uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app pytest -q`.

- [x] **Step 2: Run available lint/type checks**

Run `uv run --project /Users/wenjuhao/code/python/docker_manage/skills/package-docker-app ruff check skills/package-docker-app/scripts tests` and report if no type checker is configured.

- [x] **Step 3: Inspect the final diff and verify the requirement checklist**

Check that PATH is absent from inspect questions, plans, generated Compose environment, deployment `.env`, and `.docker-manage/.env` unless explicitly declared by Compose; check question IDs, plan hashes, stage values, snapshots, port/mount/image handling, and archive tests remain unchanged.
