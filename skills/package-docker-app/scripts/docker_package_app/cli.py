from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from docker_package_app.artifact import (
    build_manifest,
    create_verified_archive,
    write_checksums,
)
from docker_package_app.command import CommandRunner
from docker_package_app.compose import ComposeDocument
from docker_package_app.discovery import (
    DockerFileCandidates,
    default_identity,
    discover_docker_files,
    preflight,
)
from docker_package_app.docker import DockerEngine, prepare_build_compose
from docker_package_app.envscan import scan_build_args, scan_environment
from docker_package_app.errors import (
    EXIT_ANSWERS_REQUIRED,
    EXIT_MODEL_REQUIRED,
    EXIT_OK,
    EXIT_RUNTIME,
    AnswerRequired,
    ModelRequired,
    PackageError,
    SupplementValidationError,
    UsageError,
)
from docker_package_app.files import discover_file_dependencies, materialize_files
from docker_package_app.models import (
    AnswerBook,
    ImageAction,
    ImageCandidate,
    Inspection,
    PackagePlan,
    PortCandidate,
    Question,
    RunState,
    Stage,
)
from docker_package_app.planning import build_plan
from docker_package_app.questions import build_questions, parse_answer
from docker_package_app.render import (
    render_deployment,
    validate_deployment,
    write_deployment,
)
from docker_package_app.supplement import (
    ModelSupplement,
    load_supplement,
    merge_supplement,
    supplement_questions,
)
from docker_package_app.workspace import (
    WorkPaths,
    atomic_write_model,
    cleanup_run,
    load_model,
)

ALLOWED_TRANSITIONS = {
    Stage.INSPECTED: {Stage.NEEDS_MODEL, Stage.PLANNED, Stage.FAILED},
    Stage.NEEDS_MODEL: {Stage.INSPECTED, Stage.FAILED},
    Stage.PLANNED: {Stage.CONFIRMED, Stage.FAILED},
    Stage.CONFIRMED: {Stage.BUILT, Stage.FAILED},
    Stage.BUILT: {Stage.EXPORTED, Stage.FAILED},
    Stage.EXPORTED: {Stage.VERIFIED, Stage.FAILED},
    Stage.VERIFIED: {Stage.PACKAGED, Stage.FAILED},
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        body, exit_code = _dispatch(args)
        if body is not None:
            _write_result(body, pretty=not args.json)
        return exit_code
    except PackageError as exc:
        _write_error(exc)
        return _exit_code(exc)
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return EXIT_RUNTIME


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docker-package-app")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "plan", "package", "run"):
        child = subparsers.add_parser(command)
        _add_shared_arguments(child)
        if command in {"plan", "package", "run"}:
            child.add_argument("--answers", type=Path)
            child.add_argument("--non-interactive", action="store_true")
        if command == "package":
            child.add_argument("--confirm-plan-hash")
        if command == "run":
            child.add_argument("--dry-run", action="store_true")
    return parser


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--app-name")
    parser.add_argument("--version")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--dockerfile")
    parser.add_argument("--supplement", type=Path)
    parser.add_argument("--keep-work", action="store_true")


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any] | None, int]:
    project = args.project.resolve()
    if args.command == "inspect":
        paths = _paths(project, args.run_id or uuid4().hex)
        return _perform_inspect(args, paths)
    if args.command == "plan":
        paths = _existing_paths(project, args.run_id)
        return _perform_plan(args, paths), EXIT_OK
    if args.command == "package":
        paths = _existing_paths(project, args.run_id)
        try:
            return _perform_package(args, paths), EXIT_OK
        except PackageError:
            _mark_failed(paths)
            raise
        finally:
            if not args.keep_work:
                cleanup_run(paths)
    if args.command == "run":
        paths = _paths(project, args.run_id or uuid4().hex)
        preserve = False
        try:
            inspected, exit_code = _perform_inspect(args, paths)
            if exit_code == EXIT_MODEL_REQUIRED:
                preserve = True
                return inspected, exit_code
            planned = _perform_plan(args, paths)
            if args.dry_run:
                return planned, EXIT_OK
            state = load_model(paths.state, RunState)
            args.confirm_plan_hash = state.plan_hash
            return _perform_package(args, paths), EXIT_OK
        except PackageError:
            _mark_failed(paths)
            raise
        finally:
            if not args.keep_work and not preserve:
                cleanup_run(paths)
    raise UsageError(f"unknown command: {args.command}")


def _perform_inspect(
    args: argparse.Namespace,
    paths: WorkPaths,
) -> tuple[dict[str, Any], int]:
    project = paths.project_root
    runner = CommandRunner()
    report = preflight(project, runner)
    candidates = discover_docker_files(project)
    preliminary = _preload_supplement(args.supplement, project, paths.generated)
    generated_compose = _generated_paths(preliminary, "compose", project)
    generated_dockerfiles = _generated_paths(preliminary, "dockerfile", project)

    compose_files = _select_compose_files(
        project,
        candidates,
        args.compose_file,
        generated_compose,
    )
    reasons: list[str] = []
    if not compose_files:
        reasons.append("compose_missing")
    known_dockerfiles = [project / value for value in candidates.dockerfiles]
    known_dockerfiles.extend(generated_dockerfiles)
    if args.dockerfile:
        known_dockerfiles.append(_resolve_project_path(project, args.dockerfile))
    if not compose_files and not known_dockerfiles:
        reasons.append("dockerfile_missing")

    if reasons:
        inspection = Inspection(
            run_id=paths.run.name,
            project_root=str(project),
            stage=Stage.NEEDS_MODEL,
            free_disk_bytes=report.free_disk_bytes,
            dockerfiles=tuple(_display_paths(project, known_dockerfiles)),
            compose_files=tuple(_display_paths(project, compose_files)),
            profiles=tuple(args.profile),
            model_reasons=tuple(sorted(set(reasons))),
        )
        _store_initial(paths, inspection)
        return _inspection_result(inspection, ()), EXIT_MODEL_REQUIRED

    compose = ComposeDocument.load(
        project,
        compose_files,
        args.profile,
        runner,
    )
    dockerfiles, missing_dockerfiles = _resolve_service_dockerfiles(
        compose,
        project,
        args.dockerfile,
        generated_dockerfiles,
    )
    if missing_dockerfiles:
        inspection = Inspection(
            run_id=paths.run.name,
            project_root=str(project),
            stage=Stage.NEEDS_MODEL,
            free_disk_bytes=report.free_disk_bytes,
            dockerfiles=tuple(_display_paths(project, dockerfiles.values())),
            compose_files=tuple(_display_paths(project, compose_files)),
            profiles=tuple(args.profile),
            model_reasons=("dockerfile_missing",),
        )
        _store_initial(paths, inspection)
        return _inspection_result(inspection, ()), EXIT_MODEL_REQUIRED

    service_roots = _service_roots(compose)
    inspection = Inspection(
        run_id=paths.run.name,
        project_root=str(project),
        stage=Stage.INSPECTED,
        free_disk_bytes=report.free_disk_bytes,
        dockerfiles=tuple(_display_paths(project, dockerfiles.values())),
        compose_files=tuple(_display_paths(project, compose_files)),
        profiles=tuple(args.profile),
        env=scan_environment(project, service_roots, compose, dockerfiles),
        build_args=scan_build_args(dockerfiles),
        ports=_discover_ports(compose, dockerfiles),
        images=tuple(
            ImageCandidate(
                service=service,
                image=(
                    str(compose.service(service).get("image"))
                    if compose.service(service).get("image")
                    else None
                ),
                has_build="build" in compose.service(service),
            )
            for service in compose.services()
        ),
        files=discover_file_dependencies(compose, project),
    )
    extra_questions: tuple[Question, ...] = ()
    if args.supplement:
        supplement = load_supplement(
            args.supplement,
            project,
            paths.generated,
            compose.services(),
        )
        inspection = merge_supplement(inspection, supplement)
        extra_questions = supplement_questions(supplement)
        if extra_questions:
            inspection = inspection.model_copy(
                update={
                    "stage": Stage.NEEDS_MODEL,
                    "model_reasons": tuple(
                        f"model_ambiguity:{item.id}"
                        for item in supplement.ambiguities
                    ),
                }
            )
            _store_initial(paths, inspection)
            return _inspection_result(inspection, extra_questions), EXIT_MODEL_REQUIRED
    questions = (*build_questions(inspection), *extra_questions)
    _store_initial(paths, inspection)
    return _inspection_result(inspection, questions), EXIT_OK


def _perform_plan(args: argparse.Namespace, paths: WorkPaths) -> dict[str, Any]:
    state = load_model(paths.state, RunState)
    if state.stage is not Stage.INSPECTED or state.inspection is None:
        raise UsageError(f"run {state.run_id} is not ready for planning")
    questions = build_questions(state.inspection)
    provided = _load_answers(args.answers)
    answers = _resolve_answers(
        questions,
        provided,
        non_interactive=args.non_interactive or args.json,
    )
    runner = CommandRunner()
    default_app, default_version = default_identity(paths.project_root, runner)
    plan = build_plan(
        state.inspection,
        answers,
        app_name=args.app_name or default_app,
        version=args.version or default_version,
        platform=args.platform,
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


def _perform_package(args: argparse.Namespace, paths: WorkPaths) -> dict[str, Any]:
    state = load_model(paths.state, RunState)
    if state.stage is not Stage.PLANNED or state.plan is None or state.inspection is None:
        raise UsageError(f"run {state.run_id} is not ready for packaging")
    expected_hash = _plan_hash(state.plan)
    if state.plan_hash != expected_hash or args.confirm_plan_hash != expected_hash:
        raise PackageError("plan hash confirmation does not match the stored plan")

    state = _transition(state, Stage.CONFIRMED)
    atomic_write_model(paths.state, state)
    runner = CommandRunner()
    compose = ComposeDocument.load(
        paths.project_root,
        [_resolve_project_path(paths.project_root, value) for value in state.inspection.compose_files],
        state.inspection.profiles,
        runner,
    )
    plan = state.plan
    payload = paths.run / "payload"
    payload.mkdir(mode=0o700, parents=True, exist_ok=False)

    decisions = {item.resolved_path: item.action for item in plan.files}
    materialized = materialize_files(state.inspection.files, decisions, payload)
    rendered = render_deployment(compose, plan, materialized.rewrites)
    env_values = {item.artifact_name: item.value for item in plan.environment}
    compose_path, env_path = write_deployment(rendered, env_values, payload)
    validate_deployment(compose_path, env_path, runner)

    engine = DockerEngine(runner)
    build_services = tuple(
        item.service for item in plan.images if item.action is ImageAction.BUILD
    )
    if build_services:
        build_compose = prepare_build_compose(compose, plan, paths.run / "build")
        engine.build(build_compose, build_services, plan.platform)
    for item in plan.images:
        if item.action is ImageAction.PACKAGE:
            engine.pull(item.final_image, plan.platform)

    packaged_references = tuple(
        dict.fromkeys(
            item.final_image
            for item in plan.images
            if item.action in {ImageAction.BUILD, ImageAction.PACKAGE}
        )
    )
    metadata = engine.inspect(packaged_references, plan.platform)
    state = _transition(state, Stage.BUILT)
    atomic_write_model(paths.state, state)

    engine.ensure_export_space(
        metadata,
        _regular_file_bytes(payload),
        payload,
    )
    engine.save(packaged_references, payload / "images.tar")
    state = _transition(state, Stage.EXPORTED)
    atomic_write_model(paths.state, state)

    manifest = build_manifest(plan, metadata, payload)
    atomic_write_model(payload / "manifest.json", manifest)
    write_checksums(payload)
    state = _transition(state, Stage.VERIFIED)
    atomic_write_model(paths.state, state)

    archive = create_verified_archive(
        payload,
        paths.dist / f"{plan.app_name}-{plan.version}.tar.gz",
    )
    state = _transition(state, Stage.PACKAGED, archive=str(archive))
    atomic_write_model(paths.state, state)
    return {
        "stage": Stage.PACKAGED.value,
        "run_id": state.run_id,
        "archive": str(archive),
        "size": archive.stat().st_size,
        "sha256": _file_sha256(archive),
        "packaged_images": [item.reference for item in metadata],
        "reused_images": [
            item.final_image for item in plan.images if item.action is ImageAction.REUSE
        ],
        "server_paths": list(materialized.server_paths),
    }


def _preload_supplement(
    path: Path | None,
    project: Path,
    generated_root: Path,
) -> ModelSupplement | None:
    if path is None:
        return None
    try:
        supplement = ModelSupplement.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise SupplementValidationError(f"invalid model supplement: {exc}") from exc
    generated = generated_root.resolve()
    for item in supplement.generated_files:
        candidate = _resolve_project_path(project, item.path)
        if not candidate.is_relative_to(generated) or not candidate.is_file():
            raise SupplementValidationError(
                f"generated file must exist under {generated}: {item.path}"
            )
    return supplement


def _generated_paths(
    supplement: ModelSupplement | None,
    kind: str,
    project: Path,
) -> list[Path]:
    if supplement is None:
        return []
    return [
        _resolve_project_path(project, item.path)
        for item in supplement.generated_files
        if item.kind == kind
    ]


def _select_compose_files(
    project: Path,
    candidates: DockerFileCandidates,
    requested: Sequence[str],
    generated: Sequence[Path],
) -> list[Path]:
    if requested:
        selected = [_resolve_project_path(project, value) for value in requested]
    elif generated:
        selected = list(generated)
    else:
        base = [
            project / value
            for value in candidates.compose_files
            if ".override." not in value
        ]
        if len(base) > 1:
            raise UsageError("multiple Compose files found; select with --compose-file")
        selected = base
        if base:
            selected.extend(
                project / value
                for value in candidates.compose_files
                if ".override." in value
            )
    for path in selected:
        if not path.is_file():
            raise UsageError(f"Compose file does not exist: {path}")
    return selected


def _resolve_service_dockerfiles(
    compose: ComposeDocument,
    project: Path,
    requested: str | None,
    generated: Sequence[Path],
) -> tuple[dict[str, Path], tuple[str, ...]]:
    selected = _resolve_project_path(project, requested) if requested else None
    fallback = selected or (generated[0] if len(generated) == 1 else None)
    dockerfiles: dict[str, Path] = {}
    missing: list[str] = []
    for service in compose.build_services():
        raw_build = compose.service(service)["build"]
        if isinstance(raw_build, dict) and isinstance(raw_build.get("dockerfile_inline"), str):
            continue
        build = {"context": raw_build} if isinstance(raw_build, str) else raw_build
        if not isinstance(build, dict):
            raise UsageError(f"invalid build configuration for service {service}")
        context = _resolve_project_path(project, str(build.get("context", ".")))
        value = build.get("dockerfile", "Dockerfile")
        dockerfile = Path(str(value))
        if not dockerfile.is_absolute():
            dockerfile = context / dockerfile
        dockerfile = dockerfile.resolve()
        if not dockerfile.is_file() and fallback is not None:
            dockerfile = fallback.resolve()
        if not dockerfile.is_file():
            missing.append(service)
        else:
            dockerfiles[service] = dockerfile
    return dockerfiles, tuple(missing)


def _service_roots(compose: ComposeDocument) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for service in compose.build_services():
        raw = compose.service(service)["build"]
        build = {"context": raw} if isinstance(raw, str) else raw
        context = build.get("context", ".") if isinstance(build, dict) else "."
        roots[service] = _resolve_project_path(compose.project_root, str(context))
    return roots


def _discover_ports(
    compose: ComposeDocument,
    dockerfiles: Mapping[str, Path],
) -> tuple[PortCandidate, ...]:
    found: dict[tuple[str, int, str], PortCandidate] = {}
    for service in compose.services():
        config = compose.service(service)
        ports = config.get("ports", ())
        if isinstance(ports, list):
            for raw in ports:
                candidate = _compose_port(service, raw)
                if candidate is not None:
                    found[(service, candidate.container_port, candidate.protocol)] = candidate
        exposed = config.get("expose", ())
        if isinstance(exposed, list):
            for raw in exposed:
                parsed = _container_port(str(raw))
                if parsed is not None:
                    port, protocol = parsed
                    found.setdefault(
                        (service, port, protocol),
                        PortCandidate(
                            service=service,
                            container_port=port,
                            protocol=protocol,
                        ),
                    )
    for service, dockerfile in dockerfiles.items():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("EXPOSE "):
                continue
            for raw in stripped[7:].split():
                parsed = _container_port(raw)
                if parsed is None:
                    continue
                port, protocol = parsed
                found.setdefault(
                    (service, port, protocol),
                    PortCandidate(
                        service=service,
                        container_port=port,
                        protocol=protocol,
                    ),
                )
    return tuple(found[key] for key in sorted(found))


def _compose_port(service: str, raw: object) -> PortCandidate | None:
    if isinstance(raw, dict):
        target = _integer_port(raw.get("target"))
        if target is None:
            return None
        return PortCandidate(
            service=service,
            container_port=target,
            protocol=str(raw.get("protocol", "tcp")),
            host_ip=str(raw["host_ip"]) if raw.get("host_ip") else None,
            host_port=_integer_port(raw.get("published")),
        )
    if not isinstance(raw, (str, int)):
        return None
    value, _, protocol = str(raw).partition("/")
    protocol = protocol or "tcp"
    parts = value.rsplit(":", 2)
    target = _integer_port(parts[-1])
    if target is None:
        return None
    host_port = _integer_port(parts[-2]) if len(parts) >= 2 else None
    host_ip = parts[0].strip("[]") if len(parts) == 3 else None
    return PortCandidate(
        service=service,
        container_port=target,
        protocol=protocol,
        host_ip=host_ip,
        host_port=host_port,
    )


def _container_port(raw: str) -> tuple[int, str] | None:
    value, _, protocol = raw.partition("/")
    port = _integer_port(value)
    if port is None:
        return None
    return port, protocol or "tcp"


def _integer_port(value: object) -> int | None:
    try:
        port = int(str(value))
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _load_answers(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        raise AnswerRequired(f"missing answers file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"invalid answers file: {exc}") from exc
    values = raw.get("values") if isinstance(raw, dict) and "values" in raw else raw
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in values.items()
    ):
        raise UsageError("answers file must contain a string-to-string values object")
    return dict(values)


def _resolve_answers(
    questions: Sequence[Question],
    provided: Mapping[str, str],
    *,
    non_interactive: bool,
) -> AnswerBook:
    answers: dict[str, str] = {}
    for question in questions:
        if question.kind == "port_host":
            expose_id = f"{question.id.removesuffix('.host')}.expose"
            if answers.get(expose_id) == "no":
                continue
        if question.id in provided:
            answers[question.id] = parse_answer(
                question,
                provided[question.id],
                chat_mode=False,
            )
            continue
        if non_interactive:
            raise AnswerRequired(f"missing answer for {question.id}")
        while True:
            default = f" [{question.default}]" if question.default is not None else ""
            choices = f" ({'/'.join(question.choices)})" if question.choices else ""
            try:
                raw = input(f"{question.prompt}{choices}{default}: ")
                answers[question.id] = parse_answer(
                    question,
                    raw,
                    chat_mode=False,
                )
                break
            except (AnswerRequired, PackageError) as exc:
                print(exc.message, file=sys.stderr)
    return AnswerBook(values=answers)


def _store_initial(paths: WorkPaths, inspection: Inspection) -> None:
    previous: RunState | None = None
    if paths.state.is_file():
        previous = load_model(paths.state, RunState)
        if previous.stage is not Stage.NEEDS_MODEL:
            raise UsageError(f"run {previous.run_id} cannot be inspected again")
    if previous is not None and inspection.stage is Stage.INSPECTED:
        state = _transition(previous, Stage.INSPECTED, inspection=inspection)
    else:
        state = RunState(
            run_id=inspection.run_id,
            stage=inspection.stage,
            inspection=inspection,
        )
    atomic_write_model(paths.state, state)


def _transition(state: RunState, stage: Stage, **updates: object) -> RunState:
    if stage not in ALLOWED_TRANSITIONS.get(state.stage, set()):
        raise UsageError(f"invalid state transition: {state.stage.value} -> {stage.value}")
    return state.model_copy(update={"stage": stage, **updates})


def _mark_failed(paths: WorkPaths) -> None:
    if not paths.state.is_file():
        return
    try:
        state = load_model(paths.state, RunState)
        if Stage.FAILED in ALLOWED_TRANSITIONS.get(state.stage, set()):
            atomic_write_model(paths.state, _transition(state, Stage.FAILED))
    except (OSError, ValueError, PackageError):
        return


def _paths(project: Path, run_id: str) -> WorkPaths:
    try:
        return WorkPaths.create(project, run_id)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc


def _existing_paths(project: Path, run_id: str | None) -> WorkPaths:
    if not run_id:
        raise UsageError("--run-id is required")
    paths = _paths(project, run_id)
    if not paths.state.is_file():
        raise UsageError(f"run state does not exist: {run_id}")
    state = load_model(paths.state, RunState)
    inspection = state.inspection
    if inspection is not None and Path(inspection.project_root).resolve() != project:
        raise UsageError("run state belongs to a different project")
    return paths


def _inspection_result(
    inspection: Inspection,
    questions: Sequence[Question],
) -> dict[str, Any]:
    return {
        **inspection.model_dump(mode="json"),
        "questions": [item.model_dump(mode="json") for item in questions],
    }


def _plan_hash(plan: PackagePlan) -> str:
    canonical = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _regular_file_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_project_path(project: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _display_paths(project: Path, paths: Sequence[Path] | Any) -> list[str]:
    values: list[str] = []
    for path in paths:
        resolved = Path(path).resolve()
        try:
            values.append(resolved.relative_to(project).as_posix())
        except ValueError:
            values.append(str(resolved))
    return sorted(set(values))


def _write_result(body: Mapping[str, Any], *, pretty: bool) -> None:
    print(
        json.dumps(
            body,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=pretty,
        )
    )


def _write_error(error: PackageError) -> None:
    print(error.message, file=sys.stderr)
    if error.stage is not None:
        print(f"stage: {error.stage.value}", file=sys.stderr)
    if error.hint:
        print(f"hint: {error.hint}", file=sys.stderr)
    if error.details:
        print(error.details, file=sys.stderr)


def _exit_code(error: PackageError) -> int:
    if isinstance(error, ModelRequired):
        return EXIT_MODEL_REQUIRED
    if isinstance(error, AnswerRequired):
        return EXIT_ANSWERS_REQUIRED
    if isinstance(error, UsageError):
        return 2
    return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())
