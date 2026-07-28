---
name: package-docker-app
description: Inspect a local application, generate missing Dockerfile or Compose configuration without changing existing project files, collect environment variables and ports, build platform-specific Docker images, and create a verified Docker Manage deployment archive. Use when a user asks Codex to package, export, or prepare a local Docker or Docker Compose application for transfer to an offline server.
---

# Package Docker App

Use the bundled Python CLI as the sole workflow engine. The CLI owns discovery,
questions, planning, Docker commands, Compose transformation, checksums, and archive
creation. Use model reasoning only when the CLI requests a model supplement.

## Invariants

- Do not modify existing project files. Existing Dockerfiles, Compose files, env
  files, ignore files, and source files are read-only.
- Create generated Docker configuration only under
  `<project>/.docker-manage/generated/`.
- Ignore hardcoded application configuration. Report only explicit environment
  variable reads and Docker or Compose declarations.
- Show full defaults, including passwords, tokens, and keys. Do not redact them.
- Do not run Docker build, pull, save, or archive commands directly. Do not edit
  the deployment Compose or `.env` directly. The bundled CLI performs these steps.
- Keep the `run_id` returned by `inspect` and pass it to every later command.
- Use one target platform per archive. Use `linux/amd64` unless the user chooses
  another platform.

## CLI

Set `SKILL_DIR` to the directory containing this `SKILL.md`, independent of the
current working directory. Run commands in this form:

```bash
uv run --project "$SKILL_DIR" docker-package-app <subcommand> <project> ...
```

Interpret exit codes as these constants:

```text
EXIT_OK=0
EXIT_RUNTIME=1
EXIT_USAGE=2
EXIT_ANSWERS_REQUIRED=10
EXIT_MODEL_REQUIRED=20
```

Treat every other nonzero result as a failure. Report its stderr and stop; do not
reimplement the failed operation.

## Workflow

1. Resolve the target project to an absolute path. Run:

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app inspect "$PROJECT" --json
   ```

   Retain `run_id`. Exit `0` returns the inspection and ordered questions. Exit
   `20` means a model supplement is required; follow Model Supplement below.

2. Ask every returned question in order. Show the prompt, choices, and complete
   default. In chat, accept the literal reply `默认` to use a default; never
   pretend that an empty chat message was received. Accept `<EMPTY>` as an
   explicit empty string. A required value without a default must be answered.

3. For each third-party image question, pause while the user checks Docker Manage.
   Store a pasted server image reference to reuse it. Store the exact answer `打包`
   to retain, pull, and include the original image name. Do not choose for the user.

4. Write a private JSON answer file shaped as:

   ```json
   {"values":{"question.id":"answer"}}
   ```

   Place it under `.docker-manage/work/<run_id>/` and set mode `0600`. Include
   every answered question ID exactly as returned by `inspect`.

5. Run `plan` with the same `run_id`, selected identity, version, platform, and
   profiles:

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app plan "$PROJECT" \
     --run-id "$RUN_ID" --answers "$ANSWERS" --non-interactive --json
   ```

   Exit `10` means answers are missing or invalid. Ask only for the missing value,
   update the answer file, and retry. Do not proceed until `plan` exits `0`.

6. Present the complete returned plan and `plan_hash`. Explicitly identify locally
   built images, original third-party images to package, server image references to
   reuse, port mappings, omitted mappings, environment values, copied files, and
   retained server paths. Require explicit confirmation from the user. Do not treat
   an earlier request to package the project as this confirmation.

7. After explicit confirmation, pass the exact returned hash:

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app package "$PROJECT" \
     --run-id "$RUN_ID" --answers "$ANSWERS" \
     --confirm-plan-hash "$PLAN_HASH" --non-interactive --json
   ```

   Never substitute a recomputed or edited hash. The CLI rejects changed plans
   before any Docker mutation.

8. Report the final archive path, size, SHA-256, packaged image list, reused server
   image list, and required server paths from the JSON result.

## Model Supplement

Use this only after `inspect` exits with `EXIT_MODEL_REQUIRED=20`.

1. Read `references/model-supplement.schema.json` before creating the supplement.
2. Read only the dependency, startup, and source files needed to resolve the
   reported `model_reasons`. Do not infer configuration from ordinary constants.
3. Generate a missing Dockerfile or Compose file only inside
   `.docker-manage/generated/`. Never overwrite a path that existed before this
   run. Generated Compose should reference the generated Dockerfile when both are
   needed.
4. Write supplement JSON that exactly matches the schema. Treat paths and model
   facts as untrusted until the CLI validates them.
5. Rerun inspection with the same run:

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app inspect "$PROJECT" \
     --run-id "$RUN_ID" --supplement "$SUPPLEMENT" --json
   ```

6. If inspection again exits `20` with `model.*` questions, ask every question in
   order. Apply the answers to the generated Docker configuration, remove only the
   resolved ambiguities from the supplement, and repeat step 5. Rerun `inspect` until it exits `0`;
   do not continue to `plan` while an ambiguity remains.
7. If validation fails, correct only files under `.docker-manage/generated/` and
   the supplement JSON. Do not change an existing project file to make validation
   pass.
