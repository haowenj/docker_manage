from pathlib import Path

import pytest
from docker_package_app.errors import PlanValidationError
from docker_package_app.models import (
    AnswerBook,
    DefaultValue,
    EnvCandidate,
    FileAction,
    FileCandidate,
    ImageAction,
    ImageCandidate,
    Inspection,
    PortCandidate,
    SourceRef,
    Stage,
)
from docker_package_app.planning import build_plan
from docker_package_app.questions import _file_question_id


def _inspection(tmp_path: Path, *, collision: bool = False) -> Inspection:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    ports = (
        PortCandidate(service="web", container_port=8000, host_port=8080),
        PortCandidate(
            service="worker",
            container_port=9000,
            host_port=8080 if collision else 9090,
        ),
    )
    return Inspection(
        run_id="run-1",
        project_root=str(tmp_path),
        stage=Stage.INSPECTED,
        free_disk_bytes=10_000_000,
        env=(
            EnvCandidate(
                service="web",
                name="PORT",
                defaults=(DefaultValue(value="8000", source=SourceRef(path="web.py", line=1)),),
            ),
            EnvCandidate(
                service="worker",
                name="PORT",
                defaults=(DefaultValue(value="9000", source=SourceRef(path="worker.py", line=1)),),
            ),
        ),
        ports=ports,
        images=(
            ImageCandidate(service="web", image="example/web:dev", has_build=True),
            ImageCandidate(service="redis", image="redis:7", has_build=False),
        ),
        files=(
            FileCandidate(
                service="web",
                compose_value="./config",
                resolved_path=str(config),
                kind="bind",
                inside_project=True,
                estimated_size=12,
            ),
        ),
    )


def _answers(
    project_root: Path,
    *,
    collision: bool = False,
    file_decision: str = "copy",
) -> AnswerBook:
    return AnswerBook(
        values={
            "env.web.PORT": "8000",
            "env.worker.PORT": "9000",
            "port.web.8000/tcp.expose": "yes",
            "port.web.8000/tcp.host": "8080",
            "port.worker.9000/tcp.expose": "yes",
            "port.worker.9000/tcp.host": "8080" if collision else "9090",
            "image.redis.decision": "registry.intra/redis:7-approved",
            _file_question_id(str((project_root / "config").resolve())): file_decision,
        }
    )


def test_same_container_name_gets_service_prefixed_artifact_keys(tmp_path: Path) -> None:
    plan = build_plan(
        _inspection(tmp_path),
        _answers(tmp_path),
        app_name="demo",
        version="abc1234",
        platform="linux/amd64",
    )

    env_values = {item.artifact_name: item.value for item in plan.environment}
    assert env_values["WEB_PORT"] == "8000"
    assert env_values["WORKER_PORT"] == "9000"
    assert [(image.service, image.action, image.final_image) for image in plan.images] == [
        ("redis", ImageAction.REUSE, "registry.intra/redis:7-approved"),
        ("web", ImageAction.BUILD, "docker-manage/demo/web:abc1234"),
    ]
    assert plan.files[0].payload_path == "files/config"
    assert plan.files[0].kind == "bind"


def test_duplicate_host_port_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PlanValidationError, match="主机端口"):
        build_plan(
            _inspection(tmp_path, collision=True),
            _answers(tmp_path, collision=True),
            app_name="demo",
            version="v1",
            platform="linux/amd64",
        )


def test_package_decision_keeps_original_image(tmp_path: Path) -> None:
    answers = _answers(tmp_path).model_copy(
        update={"values": {**_answers(tmp_path).values, "image.redis.decision": "打包"}}
    )

    plan = build_plan(
        _inspection(tmp_path),
        answers,
        app_name="demo",
        version="v1",
        platform="linux/arm64",
    )

    redis = next(image for image in plan.images if image.service == "redis")
    assert redis.action is ImageAction.PACKAGE
    assert redis.final_image == "redis:7"
    assert redis.platform == "linux/arm64"


def test_hidden_host_port_answer_is_not_required(tmp_path: Path) -> None:
    values = dict(_answers(tmp_path).values)
    values["port.worker.9000/tcp.expose"] = "no"
    values.pop("port.worker.9000/tcp.host")

    plan = build_plan(
        _inspection(tmp_path),
        AnswerBook(values=values),
        app_name="demo",
        version="v1",
        platform="linux/amd64",
    )

    worker_port = next(item for item in plan.ports if item.service == "worker")
    assert worker_port.exposed is False
    assert worker_port.host_port is None


def test_project_bind_can_keep_server_path(tmp_path: Path) -> None:
    plan = build_plan(
        _inspection(tmp_path),
        _answers(tmp_path, file_decision="keep_server_path"),
        app_name="demo",
        version="v1",
        platform="linux/amd64",
    )

    assignment = plan.files[0]
    assert assignment.kind == "bind"
    assert assignment.action is FileAction.KEEP_SERVER_PATH
    assert assignment.payload_path is None
    assert plan.disk.known_input_bytes == 0


def test_project_bind_abort_stops_planning(tmp_path: Path) -> None:
    with pytest.raises(PlanValidationError, match="已因路径.*config.*中止打包"):
        build_plan(
            _inspection(tmp_path),
            _answers(tmp_path, file_decision="abort"),
            app_name="demo",
            version="v1",
            platform="linux/amd64",
        )


def test_bind_keep_does_not_override_same_path_config_copy(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.ini"
    shared.write_text("mode=server\n", encoding="utf-8")
    inspection = _inspection(tmp_path).model_copy(
        update={
            "files": (
                FileCandidate(
                    service="web",
                    compose_value="./shared.ini",
                    resolved_path=str(shared),
                    kind="bind",
                    inside_project=True,
                    project_path="shared.ini",
                    estimated_size=12,
                ),
                FileCandidate(
                    service="web",
                    compose_value="./shared.ini",
                    resolved_path=str(shared),
                    kind="config",
                    inside_project=True,
                    project_path="shared.ini",
                    estimated_size=12,
                ),
            )
        }
    )
    values = dict(_answers(tmp_path).values)
    values.pop(_file_question_id(str((tmp_path / "config").resolve())))
    values[_file_question_id(str(shared.resolve()))] = "keep_server_path"

    plan = build_plan(
        inspection,
        AnswerBook(values=values),
        app_name="demo",
        version="v1",
        platform="linux/amd64",
    )

    decisions = {(item.kind, item.action) for item in plan.files}
    assert decisions == {
        ("bind", FileAction.KEEP_SERVER_PATH),
        ("config", FileAction.COPY),
    }
    assert plan.disk.known_input_bytes == 12
