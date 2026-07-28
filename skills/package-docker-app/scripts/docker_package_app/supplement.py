from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from docker_package_app.envscan import merge_defaults
from docker_package_app.errors import SupplementValidationError
from docker_package_app.models import (
    DefaultValue,
    EnvCandidate,
    Inspection,
    Question,
    SourceRef,
    Stage,
    StrictModel,
)


class GeneratedFile(StrictModel):
    kind: Literal["dockerfile", "compose"]
    path: str = Field(min_length=1)


class SupplementEnvironment(StrictModel):
    service: str = Field(min_length=1)
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    default: str | None
    path: str = Field(min_length=1)
    line: int = Field(ge=1)


class SupplementAmbiguity(StrictModel):
    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    choices: tuple[str, ...] = Field(min_length=2)


class ModelSupplement(StrictModel):
    schema_version: Literal[1]
    generated_files: tuple[GeneratedFile, ...]
    environment: tuple[SupplementEnvironment, ...]
    ambiguities: tuple[SupplementAmbiguity, ...]


def load_supplement(
    path: Path,
    project_root: Path,
    generated_root: Path,
    service_names: Collection[str],
) -> ModelSupplement:
    try:
        supplement = ModelSupplement.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise SupplementValidationError(f"模型补充文件无效：{exc}") from exc

    project = project_root.resolve()
    generated = generated_root.resolve()
    for item in supplement.generated_files:
        candidate = _resolve_from_project(project, item.path)
        if not candidate.is_relative_to(generated):
            raise SupplementValidationError(
                f"生成文件必须位于 {generated} 之下：{item.path}"
            )
        if not candidate.is_file():
            raise SupplementValidationError(f"生成文件不存在：{candidate}")

    allowed_services = set(service_names)
    for item in supplement.environment:
        if item.service not in allowed_services:
            raise SupplementValidationError(
                f"模型补充文件包含未知服务：{item.service}"
            )
        source = _resolve_from_project(project, item.path)
        if not source.is_relative_to(project) or not source.is_file():
            raise SupplementValidationError(
                f"环境变量来源必须是项目文件：{item.path}"
            )
    return supplement


def merge_supplement(
    inspection: Inspection,
    supplement: ModelSupplement,
) -> Inspection:
    extra_env: list[EnvCandidate] = []
    for item in supplement.environment:
        source = SourceRef(path=item.path, line=item.line)
        defaults = (
            ()
            if item.default is None
            else (DefaultValue(value=item.default, source=source),)
        )
        extra_env.append(
            EnvCandidate(
                service=item.service,
                name=item.name,
                defaults=defaults,
                sources=(source,),
            )
        )

    dockerfiles = list(inspection.dockerfiles)
    compose_files = list(inspection.compose_files)
    for item in supplement.generated_files:
        target = dockerfiles if item.kind == "dockerfile" else compose_files
        if item.path not in target:
            target.append(item.path)

    return inspection.model_copy(
        update={
            "stage": Stage.INSPECTED,
            "dockerfiles": tuple(sorted(dockerfiles)),
            "compose_files": tuple(sorted(compose_files)),
            "env": merge_defaults((*inspection.env, *extra_env)),
            "model_reasons": (),
        }
    )


def supplement_questions(
    supplement: ModelSupplement,
) -> tuple[Question, ...]:
    return tuple(
        Question(
            id=f"model.{item.id}",
            kind="choice",
            prompt=item.prompt,
            choices=item.choices,
        )
        for item in supplement.ambiguities
    )


def _resolve_from_project(project_root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()
