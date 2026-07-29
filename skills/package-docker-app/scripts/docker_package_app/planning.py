from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from docker_package_app.current_config import artifact_component
from docker_package_app.errors import AnswerRequired, PlanValidationError
from docker_package_app.models import (
    AnswerBook,
    BuildArgAssignment,
    DiskEstimate,
    EnvAssignment,
    FileAction,
    FileAssignment,
    ImageAction,
    ImageAssignment,
    Inspection,
    PackagePlan,
    PortAssignment,
)
from docker_package_app.questions import _file_question_id, build_questions

PLATFORM_PATTERN = re.compile(r"^linux/[a-z0-9_]+(?:/[a-z0-9_]+)?$")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
IMAGE_PATTERN = re.compile(
    r"^(?:[a-z0-9.-]+(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)


def build_plan(
    inspection: Inspection,
    answers: AnswerBook,
    *,
    app_name: str,
    version: str,
    platform: str,
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

    environment = _build_environment(inspection, answers)
    build_args = tuple(
        BuildArgAssignment(
            service=item.service,
            name=item.name,
            value=answers.values[f"buildarg.{item.service}.{item.name}"],
        )
        for item in sorted(inspection.build_args, key=lambda value: (value.service, value.name))
    )
    ports = _build_ports(inspection, answers)
    images = _build_images(inspection, answers, app_name, version, platform)
    files = _build_files(inspection, answers)
    copied_input_bytes = _copied_input_bytes(inspection, files)
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


def _build_environment(
    inspection: Inspection,
    answers: AnswerBook,
) -> tuple[EnvAssignment, ...]:
    values_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in inspection.env:
        value = answers.values[f"env.{item.service}.{item.name}"]
        values_by_name[item.name].append((item.service, value))

    assignments: list[EnvAssignment] = []
    for name, service_values in sorted(values_by_name.items()):
        distinct_values = {value for _, value in service_values}
        for service, value in sorted(service_values):
            artifact_name = name
            if len(distinct_values) > 1:
                artifact_name = f"{artifact_component(service)}_{name}"
            assignments.append(
                EnvAssignment(
                    service=service,
                    container_name=name,
                    artifact_name=artifact_name,
                    value=value,
                )
            )
    return tuple(assignments)


def _build_ports(
    inspection: Inspection,
    answers: AnswerBook,
) -> tuple[PortAssignment, ...]:
    assignments: list[PortAssignment] = []
    exposed: list[PortAssignment] = []
    for item in sorted(
        inspection.ports,
        key=lambda value: (value.service, value.container_port, value.protocol),
    ):
        prefix = f"port.{item.service}.{item.container_port}/{item.protocol}"
        is_exposed = answers.values[f"{prefix}.expose"] == "yes"
        host_port = None
        if is_exposed:
            try:
                host_port = int(answers.values[f"{prefix}.host"])
            except ValueError as exc:
                raise PlanValidationError(f"{prefix} 的主机端口无效") from exc
            if not 1 <= host_port <= 65535:
                raise PlanValidationError(f"{prefix} 的主机端口无效：{host_port}")
        assignment = PortAssignment(
            service=item.service,
            container_port=item.container_port,
            protocol=item.protocol,
            exposed=is_exposed,
            host_ip=item.host_ip,
            host_port=host_port,
        )
        assignments.append(assignment)
        if is_exposed:
            for previous in exposed:
                if _ports_conflict(previous, assignment):
                    raise PlanValidationError(
                        f"主机端口冲突：{previous.service} 和 {assignment.service} "
                        f"都使用 {host_port}/{assignment.protocol}"
                    )
            exposed.append(assignment)
    return tuple(assignments)


def _build_images(
    inspection: Inspection,
    answers: AnswerBook,
    app_name: str,
    version: str,
    platform: str,
) -> tuple[ImageAssignment, ...]:
    assignments: list[ImageAssignment] = []
    for item in sorted(inspection.images, key=lambda value: value.service):
        if item.has_build:
            assignments.append(
                ImageAssignment(
                    service=item.service,
                    original_image=item.image,
                    final_image=f"docker-manage/{app_name}/{item.service}:{version}",
                    action=ImageAction.BUILD,
                    platform=platform,
                )
            )
            continue
        if not item.image:
            raise PlanValidationError(f"服务 {item.service} 既没有 build 也没有 image")
        decision = answers.values[f"image.{item.service}.decision"]
        action = ImageAction.PACKAGE if decision == "打包" else ImageAction.REUSE
        final_image = item.image if action is ImageAction.PACKAGE else decision
        if not IMAGE_PATTERN.fullmatch(final_image):
            raise PlanValidationError(f"服务 {item.service} 的镜像引用无效：{final_image}")
        assignments.append(
            ImageAssignment(
                service=item.service,
                original_image=item.image,
                final_image=final_image,
                action=action,
                platform=platform,
            )
        )
    return tuple(assignments)


def _build_files(
    inspection: Inspection,
    answers: AnswerBook,
) -> tuple[FileAssignment, ...]:
    root = Path(inspection.project_root).resolve()
    assignments: list[FileAssignment] = []
    for item in sorted(
        inspection.files,
        key=lambda value: (
            value.resolved_path,
            value.service,
            value.kind,
            value.compose_value,
        ),
    ):
        path = Path(item.resolved_path).resolve()
        if item.kind == "bind" or not item.inside_project:
            decision = answers.values[_file_question_id(str(path))]
            if decision == "abort":
                raise PlanValidationError(f"已因路径 {path} 中止打包")
            action = FileAction(decision)
        else:
            action = FileAction.COPY

        if action is FileAction.COPY and not item.inside_project:
            raise PlanValidationError(f"无法复制项目目录之外的路径：{path}")

        payload_path = None
        if action is FileAction.COPY:
            relative = path.relative_to(root)
            payload = Path("files") / (relative if relative.parts else Path("project"))
            payload_path = payload.as_posix()
        assignments.append(
            FileAssignment(
                service=item.service,
                original_value=item.compose_value,
                resolved_path=str(path),
                kind=item.kind,
                action=action,
                payload_path=payload_path,
            )
        )
    return tuple(assignments)


def _copied_input_bytes(
    inspection: Inspection,
    assignments: Sequence[FileAssignment],
) -> int:
    copied_paths = {
        item.resolved_path
        for item in assignments
        if item.action is FileAction.COPY
    }
    size_by_path: dict[str, int] = {}
    for item in inspection.files:
        if item.resolved_path in copied_paths:
            size_by_path[item.resolved_path] = max(
                size_by_path.get(item.resolved_path, 0),
                item.estimated_size,
            )
    return sum(size_by_path.values())


def _ports_conflict(left: PortAssignment, right: PortAssignment) -> bool:
    if left.protocol != right.protocol or left.host_port != right.host_port:
        return False
    wildcard = {None, "", "0.0.0.0", "::"}
    return left.host_ip == right.host_ip or left.host_ip in wildcard or right.host_ip in wildcard


def _validate_identity(app_name: str, version: str, platform: str) -> None:
    if not NAME_PATTERN.fullmatch(app_name):
        raise PlanValidationError(f"应用名称无效：{app_name}")
    if not TAG_PATTERN.fullmatch(version):
        raise PlanValidationError(f"版本标签无效：{version}")
    if not PLATFORM_PATTERN.fullmatch(platform):
        raise PlanValidationError(f"目标平台无效：{platform}")
