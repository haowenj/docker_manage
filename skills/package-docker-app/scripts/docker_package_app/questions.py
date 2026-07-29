from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from docker_package_app.errors import AnswerRequired, PlanValidationError
from docker_package_app.models import Inspection, Question

YES_VALUES = {"yes", "y", "true", "1", "是"}
NO_VALUES = {"no", "n", "false", "0", "否"}
NO_ENV_OVERRIDES = "无修改"


def build_questions(inspection: Inspection) -> tuple[Question, ...]:
    questions: list[Question] = []
    for candidate in sorted(inspection.env, key=lambda item: (item.service, item.name)):
        values = sorted({item.value for item in candidate.defaults})
        default = (
            candidate.current.value
            if candidate.current is not None
            else values[0]
            if len(values) == 1
            else None
        )
        source_text = ", ".join(
            f"{item.source.path}:{item.source.line or '-'}={item.value}"
            for item in candidate.defaults
        )
        prompt = f"设置 {candidate.service}.{candidate.name}"
        if candidate.current is not None:
            prompt += (
                f"。当前配置值：{candidate.current.value}，"
                f"来源：{candidate.current.source.path}"
            )
        if source_text:
            prompt += f"。声明默认值来源：{source_text}"
        questions.append(
            Question(
                id=f"env.{candidate.service}.{candidate.name}",
                kind="env",
                prompt=prompt,
                default=default,
            )
        )

    for candidate in sorted(
        inspection.build_args,
        key=lambda item: (item.service, item.name),
    ):
        questions.append(
            Question(
                id=f"buildarg.{candidate.service}.{candidate.name}",
                kind="build_arg",
                prompt=f"设置构建参数 {candidate.service}.{candidate.name}",
                default=candidate.default,
            )
        )

    for port in sorted(
        inspection.ports,
        key=lambda item: (item.service, item.container_port, item.protocol),
    ):
        prefix = f"port.{port.service}.{port.container_port}/{port.protocol}"
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

    for image in sorted(inspection.images, key=lambda item: item.service):
        if image.has_build:
            continue
        questions.append(
            Question(
                id=f"image.{image.service}.decision",
                kind="image",
                prompt=(
                    f"请在 Docker Manage 中检查镜像 {image.image}。粘贴可复用的镜像引用，"
                    "或输入“打包”以拉取并包含原始镜像。"
                ),
                default="打包",
            )
        )

    for candidate in sorted(
        (item for item in inspection.files if not item.inside_project),
        key=lambda item: item.resolved_path,
    ):
        questions.append(
            Question(
                id=_file_question_id(candidate.resolved_path),
                kind="file",
                prompt=(
                    f"{candidate.resolved_path} 位于项目目录之外。请选择 "
                    "keep_server_path（保留服务器路径）或 abort（中止）。"
                ),
                choices=("keep_server_path", "abort"),
            )
        )
    return tuple(questions)


def environment_questions(questions: Sequence[Question]) -> tuple[Question, ...]:
    return tuple(question for question in questions if question.kind == "env")


def format_environment_questions(
    questions: Sequence[Question],
) -> tuple[str, ...]:
    lines: list[str] = []
    for index, question in enumerate(environment_questions(questions), start=1):
        if question.default is not None:
            suffix = f"，默认值：{question.default}"
        elif "声明默认值来源：" in question.prompt:
            suffix = "，必填，默认值冲突"
        else:
            suffix = "，必填，无默认值"
        lines.append(f"{index}. {question.prompt}{suffix}")
    return tuple(lines)


def parse_environment_overrides(
    questions: Sequence[Question],
    lines: Sequence[str],
) -> dict[str, str]:
    env = environment_questions(questions)
    meaningful = tuple(line.strip() for line in lines if line.strip())
    overrides: dict[int, str] = {}
    if meaningful != (NO_ENV_OVERRIDES,):
        for line in meaningful:
            sequence_text, separator, value = line.partition(":")
            if not separator or not sequence_text.strip().isdigit():
                raise PlanValidationError(
                    f"环境变量输入格式错误：{line}；请使用 序号: 值"
                )
            sequence = int(sequence_text.strip())
            if sequence < 1 or sequence > len(env):
                raise PlanValidationError(f"环境变量序号超出范围：{sequence}")
            if sequence in overrides:
                raise PlanValidationError(f"环境变量序号重复：{sequence}")
            overrides[sequence] = value.strip()

    answers: dict[str, str] = {}
    missing: list[str] = []
    for sequence, question in enumerate(env, start=1):
        if sequence in overrides:
            answers[question.id] = parse_answer(
                question,
                overrides[sequence],
                chat_mode=True,
            )
        elif question.default is not None:
            answers[question.id] = question.default
        else:
            missing.append(f"{sequence}. {question.id}")
    if missing:
        raise AnswerRequired("以下环境变量必须填写：" + "；".join(missing))
    return answers


def parse_answer(question: Question, raw: str, *, chat_mode: bool) -> str:
    if raw == "<EMPTY>":
        return ""
    wants_default = (chat_mode and raw == "默认") or (not chat_mode and raw == "")
    if wants_default:
        if question.default is None:
            raise AnswerRequired(
                f"问题 {question.id} 必须填写",
                hint="请提供一个值；显式空字符串请使用 <EMPTY>。",
            )
        return question.default
    if raw == "" and question.required:
        raise AnswerRequired(f"问题 {question.id} 必须填写")

    value = raw
    if question.kind == "port_expose":
        normalized = raw.strip().lower()
        if normalized in YES_VALUES:
            value = "yes"
        elif normalized in NO_VALUES:
            value = "no"
        else:
            raise PlanValidationError(f"问题 {question.id} 的是/否答案无效：{raw}")
    if question.choices and value not in question.choices:
        raise PlanValidationError(
            f"问题 {question.id} 的答案无效：{value}；"
            f"请选择以下值之一：{', '.join(question.choices)}"
        )
    return value


def _file_question_id(resolved_path: str) -> str:
    import hashlib

    digest = hashlib.sha256(str(Path(resolved_path)).encode()).hexdigest()[:16]
    return f"file.{digest}.decision"
