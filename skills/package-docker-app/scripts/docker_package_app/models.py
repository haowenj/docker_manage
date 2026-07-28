from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage(str, Enum):
    INSPECTED = "inspected"
    NEEDS_MODEL = "needs_model"
    PLANNED = "planned"
    CONFIRMED = "confirmed"
    BUILT = "built"
    EXPORTED = "exported"
    VERIFIED = "verified"
    PACKAGED = "packaged"
    FAILED = "failed"


class ImageAction(str, Enum):
    BUILD = "build"
    PACKAGE = "package"
    REUSE = "reuse"


class FileAction(str, Enum):
    COPY = "copy"
    KEEP_SERVER_PATH = "keep_server_path"


class SourceRef(StrictModel):
    path: str
    line: int | None = Field(default=None, ge=1)


class DefaultValue(StrictModel):
    value: str
    source: SourceRef


class EnvCandidate(StrictModel):
    service: str
    name: str
    defaults: tuple[DefaultValue, ...] = ()
    sources: tuple[SourceRef, ...] = ()


class BuildArgCandidate(StrictModel):
    service: str
    name: str
    default: str | None = None
    source: SourceRef


class PortCandidate(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None
    host_port: int | None = Field(default=None, ge=1, le=65535)


class ImageCandidate(StrictModel):
    service: str
    image: str | None = None
    has_build: bool = False


class FileCandidate(StrictModel):
    service: str
    compose_value: str
    resolved_path: str
    kind: Literal["bind", "config", "secret"]
    inside_project: bool
    project_path: str | None = None
    estimated_size: int = Field(ge=0)


class EnvAssignment(StrictModel):
    service: str
    container_name: str
    artifact_name: str
    value: str


class PortAssignment(StrictModel):
    service: str
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    exposed: bool
    host_ip: str | None = None
    host_port: int | None = Field(default=None, ge=1, le=65535)


class ImageAssignment(StrictModel):
    service: str
    original_image: str | None = None
    final_image: str
    action: ImageAction
    platform: str


class FileAssignment(StrictModel):
    service: str
    original_value: str
    resolved_path: str
    action: FileAction
    payload_path: str | None = None


class BuildArgAssignment(StrictModel):
    service: str
    name: str
    value: str


class DiskEstimate(StrictModel):
    known_input_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    unknown_components: tuple[str, ...] = ()


class Inspection(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    project_root: str
    stage: Stage
    free_disk_bytes: int = Field(default=0, ge=0)
    dockerfiles: tuple[str, ...] = ()
    compose_files: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    env: tuple[EnvCandidate, ...] = ()
    build_args: tuple[BuildArgCandidate, ...] = ()
    ports: tuple[PortCandidate, ...] = ()
    images: tuple[ImageCandidate, ...] = ()
    files: tuple[FileCandidate, ...] = ()
    model_reasons: tuple[str, ...] = ()


class Question(StrictModel):
    id: str
    kind: Literal[
        "env",
        "build_arg",
        "port_expose",
        "port_host",
        "image",
        "file",
        "choice",
        "confirm",
    ]
    prompt: str
    default: str | None = None
    required: bool = True
    choices: tuple[str, ...] = ()


class AnswerBook(StrictModel):
    values: dict[str, str]


class PackagePlan(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    project_root: str
    app_name: str
    version: str
    compose_project_name: str
    platform: str = "linux/amd64"
    profiles: tuple[str, ...] = ()
    environment: tuple[EnvAssignment, ...] = ()
    ports: tuple[PortAssignment, ...] = ()
    images: tuple[ImageAssignment, ...] = ()
    files: tuple[FileAssignment, ...] = ()
    build_args: tuple[BuildArgAssignment, ...] = ()
    disk: DiskEstimate
    questions: tuple[Question, ...] = ()


class RunState(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    stage: Stage
    inspection: Inspection | None = None
    plan: PackagePlan | None = None
    plan_hash: str | None = None
    archive: str | None = None
