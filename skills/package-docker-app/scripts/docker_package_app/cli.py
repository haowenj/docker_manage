from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from pydantic import ValidationError

from docker_package_app.artifact import (
    build_manifest,
    create_verified_archive,
    write_checksums,
)
from docker_package_app.command import CommandRunner
from docker_package_app.compose import ComposeDocument
from docker_package_app.current_config import (
    CURRENT_MOUNTS_RELATIVE,
    attach_current_mounts,
    attach_current_ports,
    attach_current_values,
    write_current_configuration,
)
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
    EXIT_USAGE,
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
from docker_package_app.planning import (
    build_plan,
    compose_current_file_environment,
    compose_file_environment,
)
from docker_package_app.questions import (
    NO_ENV_OVERRIDES,
    build_questions,
    environment_questions,
    format_environment_questions,
    parse_answer,
    parse_environment_overrides,
)
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


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        self.add_argument("-h", "--help", action="help", help="显示帮助并退出")

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(
            EXIT_USAGE,
            f"{self.prog}: 参数错误：{_translate_argparse_error(message)}\n",
        )


def _translate_argparse_error(message: str) -> str:
    required = "the following arguments are required: "
    if message.startswith(required):
        return "缺少必填参数：" + message.removeprefix(required)
    unrecognized = "unrecognized arguments: "
    if message.startswith(unrecognized):
        return "无法识别的参数：" + message.removeprefix(unrecognized)

    invalid_choice = re.fullmatch(
        r"argument ([^:]+): invalid choice: (.+) \(choose from (.+)\)",
        message,
    )
    if invalid_choice:
        argument, value, choices = invalid_choice.groups()
        return f"参数 {argument} 的值无效：{value}；可选值：{choices}"

    missing_value = re.fullmatch(r"argument ([^:]+): expected one argument", message)
    if missing_value:
        return f"参数 {missing_value.group(1)} 需要一个值"
    return "参数内容不符合要求"


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
        print("已取消", file=sys.stderr)
        return EXIT_RUNTIME


def _build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        prog="docker-package-app",
        description="检查本地应用并生成可导入 Docker Manage 的离线部署包。",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="命令",
    )
    descriptions = {
        "inspect": "检查项目并输出待确认问题",
        "plan": "根据答案生成打包计划",
        "package": "确认计划后构建部署包",
        "run": "执行完整交互流程",
    }
    for command, description in descriptions.items():
        child = subparsers.add_parser(command, help=description, description=description)
        _add_shared_arguments(child)
        if command in {"plan", "package", "run"}:
            child.add_argument("--answers", type=Path, help="答案 JSON 文件")
            child.add_argument(
                "--non-interactive",
                action="store_true",
                help="禁用交互输入",
            )
        if command == "package":
            child.add_argument("--confirm-plan-hash", help="确认执行的计划哈希")
        if command == "run":
            child.add_argument("--dry-run", action="store_true", help="只生成计划，不执行打包")
    return parser


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project", type=Path, help="待处理的项目目录")
    parser.add_argument("--json", action="store_true", help="输出紧凑 JSON")
    parser.add_argument("--run-id", help="继续已有运行 ID")
    parser.add_argument("--app-name", help="部署应用名称")
    parser.add_argument("--version", help="镜像版本标签")
    parser.add_argument("--platform", default="linux/amd64", help="目标容器平台")
    parser.add_argument("--profile", action="append", default=[], help="启用 Compose profile")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="指定 Compose 文件",
    )
    parser.add_argument("--dockerfile", help="指定 Dockerfile")
    parser.add_argument("--supplement", type=Path, help="模型补充 JSON 文件")
    parser.add_argument("--keep-work", action="store_true", help="保留运行工作目录")


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
    raise UsageError(f"未知命令：{args.command}")


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
    inspection = inspection.model_copy(
        update={
            "env": attach_current_values(project, inspection.env),
            "ports": attach_current_ports(project, inspection.ports),
        }
    )
    current_files = inspection.files
    if (project / CURRENT_MOUNTS_RELATIVE).exists():
        current_compose = ComposeDocument.load(
            project,
            compose_files,
            args.profile,
            runner,
            environment=compose_current_file_environment(inspection),
            interpolate=True,
        )
        current_files = discover_file_dependencies(
            compose,
            project,
            resolved_compose=current_compose,
        )
    inspection = inspection.model_copy(
        update={"files": attach_current_mounts(project, current_files)}
    )
    questions = (*build_questions(inspection), *extra_questions)
    _store_initial(paths, inspection)
    return _inspection_result(inspection, questions), EXIT_OK


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


def _perform_package(args: argparse.Namespace, paths: WorkPaths) -> dict[str, Any]:
    state = load_model(paths.state, RunState)
    if state.stage is not Stage.PLANNED or state.plan is None or state.inspection is None:
        raise UsageError(f"运行 {state.run_id} 尚未准备好进行打包")
    expected_hash = _plan_hash(state.plan)
    if state.plan_hash != expected_hash or args.confirm_plan_hash != expected_hash:
        raise PackageError("计划哈希确认值与已保存的计划不匹配")

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

    materialized = materialize_files(
        state.inspection.files,
        plan.files,
        payload,
        paths.project_root,
    )
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
    write_current_configuration(
        paths.project_root,
        plan.environment,
        plan.ports,
        plan.files,
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
        raise SupplementValidationError(f"模型补充文件无效：{exc}") from exc
    generated = generated_root.resolve()
    for item in supplement.generated_files:
        candidate = _resolve_project_path(project, item.path)
        if not candidate.is_relative_to(generated) or not candidate.is_file():
            raise SupplementValidationError(
                f"生成文件必须存在于 {generated} 之下：{item.path}"
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
            raise UsageError("发现多个 Compose 文件；请使用 --compose-file 选择")
        selected = base
        if base:
            selected.extend(
                project / value
                for value in candidates.compose_files
                if ".override." in value
            )
    for path in selected:
        if not path.is_file():
            raise UsageError(f"Compose 文件不存在：{path}")
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
            raise UsageError(f"服务 {service} 的构建配置无效")
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
    parts = _split_short_port(value)
    if parts is None:
        return None
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
        raise AnswerRequired(f"缺少答案文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"答案文件无效：{exc}") from exc
    values = raw.get("values") if isinstance(raw, dict) and "values" in raw else raw
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in values.items()
    ):
        raise UsageError("答案文件必须包含键和值均为字符串的 values 对象")
    return dict(values)


def _resolve_answers(
    questions: Sequence[Question],
    provided: Mapping[str, str],
    *,
    non_interactive: bool,
) -> AnswerBook:
    answers: dict[str, str] = {}
    env = environment_questions(questions)
    provided_env = {
        question.id: parse_answer(
            question,
            provided[question.id],
            chat_mode=False,
        )
        for question in env
        if question.id in provided
    }
    pending_env = tuple(
        question for question in env if question.id not in provided_env
    )
    answers.update(provided_env)
    if pending_env:
        if non_interactive:
            raise AnswerRequired(f"缺少答案：{pending_env[0].id}")
        answers.update(_read_environment_overrides(pending_env))

    for question in questions:
        if question.kind == "env":
            continue
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
            raise AnswerRequired(f"缺少答案：{question.id}")
        while True:
            default = (
                f" [默认值：{question.default}]"
                if question.default is not None
                else ""
            )
            choices = (
                f"（可选值：{'/'.join(question.choices)}）"
                if question.choices
                else ""
            )
            try:
                raw = input(f"{question.prompt}{choices}{default}：")
                answers[question.id] = parse_answer(
                    question,
                    raw,
                    chat_mode=False,
                )
                break
            except (AnswerRequired, PackageError) as exc:
                print(exc.message, file=sys.stderr)
    return AnswerBook(values=answers)


def _read_environment_overrides(
    questions: Sequence[Question],
) -> dict[str, str]:
    lines: list[str] = []
    show_questions = True
    while True:
        if show_questions:
            print("请设置环境变量，只输入需要修改的“序号: 值”。")
            for line in format_environment_questions(questions):
                print(line)
            print("全部使用默认值请输入“无修改”；显式空值请使用 <EMPTY>。")

        while True:
            raw = input("环境变量覆盖（空行结束）：")
            if raw == "":
                break
            lines.append(raw)
            if raw.strip() == NO_ENV_OVERRIDES:
                break
        try:
            return parse_environment_overrides(questions, lines)
        except AnswerRequired as exc:
            print(exc.message, file=sys.stderr)
            lines = [
                line for line in lines if line.strip() != NO_ENV_OVERRIDES
            ]
            print("请只补充上述缺失序号。")
            show_questions = False
        except PackageError as exc:
            print(exc.message, file=sys.stderr)
            lines.clear()
            print("请重新输入环境变量覆盖项。")
            show_questions = True


def _store_initial(paths: WorkPaths, inspection: Inspection) -> None:
    previous: RunState | None = None
    if paths.state.is_file():
        previous = load_model(paths.state, RunState)
        if previous.stage is not Stage.NEEDS_MODEL:
            raise UsageError(f"运行 {previous.run_id} 不能再次检查")
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
        raise UsageError(f"状态转换无效：{state.stage.value} -> {stage.value}")
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
        raise UsageError("必须提供 --run-id")
    paths = _paths(project, run_id)
    if not paths.state.is_file():
        raise UsageError(f"运行状态不存在：{run_id}")
    state = load_model(paths.state, RunState)
    inspection = state.inspection
    if inspection is not None and Path(inspection.project_root).resolve() != project:
        raise UsageError("运行状态属于另一个项目")
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
        print(f"阶段：{error.stage.value}", file=sys.stderr)
    if error.hint:
        print(f"建议：{error.hint}", file=sys.stderr)
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
