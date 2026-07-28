from __future__ import annotations

from pathlib import Path

from docker_package_app.errors import AnswerRequired, PlanValidationError
from docker_package_app.models import Inspection, Question

YES_VALUES = {"yes", "y", "true", "1", "是"}
NO_VALUES = {"no", "n", "false", "0", "否"}


def build_questions(inspection: Inspection) -> tuple[Question, ...]:
    questions: list[Question] = []
    for candidate in sorted(inspection.env, key=lambda item: (item.service, item.name)):
        values = sorted({item.value for item in candidate.defaults})
        default = values[0] if len(values) == 1 else None
        source_text = ", ".join(
            f"{item.source.path}:{item.source.line or '-'}={item.value}"
            for item in candidate.defaults
        )
        prompt = f"Set {candidate.service}.{candidate.name}"
        if source_text:
            prompt += f". Defaults: {source_text}"
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
                prompt=f"Set build argument {candidate.service}.{candidate.name}",
                default=candidate.default,
            )
        )

    for port in sorted(
        inspection.ports,
        key=lambda item: (item.service, item.container_port, item.protocol),
    ):
        prefix = f"port.{port.service}.{port.container_port}/{port.protocol}"
        questions.append(
            Question(
                id=f"{prefix}.expose",
                kind="port_expose",
                prompt=(
                    f"Expose {port.service} container port "
                    f"{port.container_port}/{port.protocol}?"
                ),
                default="yes" if port.host_port else "no",
                choices=("yes", "no"),
            )
        )
        questions.append(
            Question(
                id=f"{prefix}.host",
                kind="port_host",
                prompt=f"Host port for {port.service}:{port.container_port}/{port.protocol}",
                default=str(port.host_port or port.container_port),
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
                    f"Check Docker Manage for {image.image}. Paste a reusable image "
                    "reference, or choose 打包 to pull and include the original image."
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
                    f"{candidate.resolved_path} is outside the project. "
                    "Choose keep_server_path or abort."
                ),
                choices=("keep_server_path", "abort"),
            )
        )
    return tuple(questions)


def parse_answer(question: Question, raw: str, *, chat_mode: bool) -> str:
    if raw == "<EMPTY>":
        return ""
    wants_default = (chat_mode and raw == "默认") or (not chat_mode and raw == "")
    if wants_default:
        if question.default is None:
            raise AnswerRequired(
                f"answer required for {question.id}",
                hint="Provide a value; use <EMPTY> for an explicit empty string.",
            )
        return question.default
    if raw == "" and question.required:
        raise AnswerRequired(f"answer required for {question.id}")

    value = raw
    if question.kind == "port_expose":
        normalized = raw.strip().lower()
        if normalized in YES_VALUES:
            value = "yes"
        elif normalized in NO_VALUES:
            value = "no"
        else:
            raise PlanValidationError(f"invalid yes/no answer for {question.id}: {raw}")
    if question.choices and value not in question.choices:
        raise PlanValidationError(
            f"invalid answer for {question.id}: {value}; "
            f"choose one of {', '.join(question.choices)}"
        )
    return value


def _file_question_id(resolved_path: str) -> str:
    import hashlib

    digest = hashlib.sha256(str(Path(resolved_path)).encode()).hexdigest()[:16]
    return f"file.{digest}.decision"

