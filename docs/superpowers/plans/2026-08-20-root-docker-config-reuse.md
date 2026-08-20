# Root Docker Configuration Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate missing Dockerfile/Compose files in the project root once, then let subsequent packaging runs discover and reuse them without supplement regeneration.

**Architecture:** Extend supplement path validation to accept only standard root-level Docker configuration paths while retaining the legacy `.docker-manage/generated/` path. Update the model-facing skill to make root files the default one-time output and keep existing root files immutable during packaging. Add unit, integration, and skill-contract coverage for root generation and automatic reuse.

**Tech Stack:** Python 3.11, Pydantic, pytest, Markdown Agent Skill, uv

**Spec:** `docs/superpowers/specs/2026-08-20-root-docker-config-reuse-design.md`

## Global Constraints

- Existing project Dockerfile/Compose files are read-only and must never be overwritten.
- New root paths are limited to standard Dockerfile and Compose filenames.
- Legacy `.docker-manage/generated/` supplement paths remain supported.
- No changes to answer JSON, snapshot, plan-hash, Docker, or archive protocols.
- No `gh` or other platform CLI usage; ordinary Git operations use local `git`.
- Preserve the pre-existing untracked `.docker-manage/` content and do not stage it.

---

### Task 1: Add failing coverage for root-generated configuration

**Files:**
- Modify: `tests/unit/test_supplement.py`
- Modify: `tests/e2e/test_model_supplement_flow.py`
- Modify: `tests/unit/test_skill_contract.py`

**Interfaces:**
- Consumes: current supplement validation, CLI inspect flow, and `skills/package-docker-app/SKILL.md`.
- Produces: tests that fail until root-level generation and documentation are implemented.

- [ ] **Step 1: Add the unit test for an allowed root Dockerfile**

Add a test that creates a root `Dockerfile`, writes a supplement referencing `Dockerfile`, and asserts `load_supplement` succeeds.

- [ ] **Step 2: Add the unit test for an allowed root Compose file**

Add a test that creates a root `compose.yaml`, writes a supplement referencing `compose.yaml`, and asserts `load_supplement` succeeds.

- [ ] **Step 3: Add the unit test for rejecting non-standard root paths**

Add a test that references `README.md` as a Dockerfile and asserts `SupplementValidationError`.

- [ ] **Step 4: Update the e2e model supplement scenario to cover root generation**

Change the generated files in the scenario to root `Dockerfile` and `compose.yaml`, make Compose reference `Dockerfile`, and assert a fresh `inspect` without `--supplement` discovers them.

- [ ] **Step 5: Add skill-contract assertions**

Require the skill text to mention project-root generation, missing-only creation, no overwrite of existing project files, and later reuse without supplement.

- [ ] **Step 6: Run focused tests and verify RED**

Run:

```bash
uv run --project skills/package-docker-app pytest -q \
  tests/unit/test_supplement.py \
  tests/e2e/test_model_supplement_flow.py \
  tests/unit/test_skill_contract.py
```

Expected: the new root-path and skill-contract assertions fail; existing legacy tests may still pass. Fix only test setup errors before proceeding.

- [ ] **Step 7: Commit the failing tests**

```bash
git add tests/unit/test_supplement.py tests/e2e/test_model_supplement_flow.py tests/unit/test_skill_contract.py
git commit -m "test: cover root Docker config reuse"
```

### Task 2: Implement safe root-path validation

**Files:**
- Modify: `skills/package-docker-app/scripts/docker_package_app/supplement.py`
- Modify: `skills/package-docker-app/scripts/docker_package_app/cli.py`
- Test: `tests/unit/test_supplement.py`

**Interfaces:**
- Consumes: supplement `GeneratedFile.kind/path`, project root, and legacy generated root.
- Produces: one shared validator that accepts legacy generated paths and standard root Docker paths while rejecting arbitrary paths.

- [ ] **Step 1: Add a shared generated-file path validator**

Define a helper in `supplement.py` that resolves a supplement path and accepts it when either:

```python
candidate.is_relative_to(generated_root)
```

or it is a direct child of `project_root` with a kind-specific standard name: `Dockerfile`/`Dockerfile.*` for `dockerfile`, or the standard Compose/override names for `compose`.

Require the resolved candidate to be a regular file and raise `SupplementValidationError` with the offending path otherwise.

- [ ] **Step 2: Use the validator from `load_supplement`**

Replace the generated-root-only check with the shared validator, preserving environment source and service validation.

- [ ] **Step 3: Use the same validator from CLI preloading**

Import the helper in `cli.py` and replace `_preload_supplement`’s generated-root-only validation, so the initial file-selection phase accepts the same paths as the full supplement loader.

- [ ] **Step 4: Run focused unit tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest -q tests/unit/test_supplement.py
```

Expected: root acceptance and rejection tests pass, and legacy generated-root tests remain green.

- [ ] **Step 5: Commit the validator implementation**

```bash
git add skills/package-docker-app/scripts/docker_package_app/supplement.py skills/package-docker-app/scripts/docker_package_app/cli.py tests/unit/test_supplement.py
git commit -m "feat: allow safe root Docker supplement files"
```

### Task 3: Update the model-facing skill contract

**Files:**
- Modify: `skills/package-docker-app/SKILL.md`
- Test: `tests/unit/test_skill_contract.py`

**Interfaces:**
- Consumes: the root-path CLI behavior from Task 2.
- Produces: Chinese instructions that generate missing root Docker files once, never overwrite existing files, and reuse them on subsequent packaging.

- [ ] **Step 1: Replace the blanket generated-directory rule**

State that existing project files remain read-only, but model supplementation may create only missing standard `Dockerfile`/Compose files in the project root; retain `.docker-manage/generated/` as a legacy-compatible location.

- [ ] **Step 2: Update the model supplement workflow**

Require generated Compose to reference the root Dockerfile, prohibit overwriting files that existed before the current run, and explain that a later inspect discovers root files without `--supplement`.

- [ ] **Step 3: Keep coding ownership explicit**

Document that changes to dependencies, system commands, environment variables, ports, or startup commands are maintained by the AI coding phase; packaging reads and validates the root files and does not silently rewrite them.

- [ ] **Step 4: Run skill-contract tests and verify GREEN**

Run:

```bash
uv run --project skills/package-docker-app pytest -q tests/unit/test_skill_contract.py
```

Expected: all contract assertions pass, including the existing safety and repeat-package assertions.

- [ ] **Step 5: Commit the skill change**

```bash
git add skills/package-docker-app/SKILL.md tests/unit/test_skill_contract.py
git commit -m "docs: make root Docker files reusable"
```

### Task 4: Verify the end-to-end behavior and repository quality

**Files:**
- Verify: `skills/package-docker-app/scripts/docker_package_app/`
- Verify: `tests/`
- Verify: `skills/package-docker-app/SKILL.md`

**Interfaces:**
- Consumes: all implementation commits from Tasks 1–3.
- Produces: fresh evidence that root generation, legacy compatibility, automatic rediscovery, and the full suite work together.

- [ ] **Step 1: Run the model-supplement e2e tests**

```bash
uv run --project skills/package-docker-app pytest -q tests/e2e/test_model_supplement_flow.py
```

Expected: root generation and next-run discovery pass, and legacy generated-directory compatibility remains green.

- [ ] **Step 2: Run the full test suite**

```bash
uv run --project skills/package-docker-app pytest -q
```

Expected: zero failures; record the pass/skip counts.

- [ ] **Step 3: Run lint, skill validation, and diff checks**

```bash
uv run --project skills/package-docker-app ruff check skills/package-docker-app/scripts tests
python /Users/wenjuhao/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/package-docker-app
git diff --check
```

Expected: all commands exit `0`, with no whitespace errors.

- [ ] **Step 4: Review the final diff and preserve unrelated files**

```bash
git status --short
git diff main...HEAD --stat
git diff main...HEAD -- skills/package-docker-app/SKILL.md skills/package-docker-app/scripts/docker_package_app/supplement.py skills/package-docker-app/scripts/docker_package_app/cli.py tests
```

Confirm `.docker-manage/` from the original checkout is not staged or changed.
