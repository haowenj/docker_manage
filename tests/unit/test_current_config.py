import json
import stat
from pathlib import Path

import pytest
from docker_package_app.current_config import (
    CURRENT_ENV_SOURCE,
    CURRENT_IMAGES_RELATIVE,
    CURRENT_MOUNTS_RELATIVE,
    CURRENT_PORTS_RELATIVE,
    artifact_component,
    attach_current_images,
    attach_current_mounts,
    attach_current_ports,
    attach_current_values,
    write_current_configuration,
    write_current_environment,
    write_current_mounts,
    write_current_ports,
)
from docker_package_app.errors import PackageError, UsageError
from docker_package_app.models import (
    DefaultValue,
    EnvAssignment,
    EnvCandidate,
    FileAction,
    FileAssignment,
    FileCandidate,
    ImageCandidate,
    PortAssignment,
    PortCandidate,
    SourceRef,
)
from dotenv import dotenv_values


def _candidate(service: str, name: str, default: str = "declared") -> EnvCandidate:
    source = SourceRef(path=f"{service}.py", line=1)
    return EnvCandidate(
        service=service,
        name=name,
        defaults=(DefaultValue(value=default, source=source),),
        sources=(source,),
    )


def _bind(resolved_path: str, *, inside: bool) -> FileCandidate:
    return FileCandidate(
        service="web",
        compose_value=resolved_path,
        resolved_path=resolved_path,
        kind="bind",
        inside_project=inside,
        project_path=Path(resolved_path).name if inside else None,
        estimated_size=0,
    )


def _write_mount_snapshot(
    project: Path,
    mounts: list[dict[str, str]],
) -> Path:
    snapshot = project / CURRENT_MOUNTS_RELATIVE
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps({"schema_version": 1, "mounts": mounts}),
        encoding="utf-8",
    )
    return snapshot


def _file_assignment(
    service: str,
    resolved_path: str,
    kind: str,
    action: FileAction,
) -> FileAssignment:
    return FileAssignment(
        service=service,
        original_value=resolved_path,
        resolved_path=resolved_path,
        kind=kind,
        action=action,
        payload_path=(
            f"files/{Path(resolved_path).name}"
            if action is FileAction.COPY
            else None
        ),
    )


def test_missing_snapshot_preserves_discovered_candidates(tmp_path: Path) -> None:
    candidates = (_candidate("web", "PORT", "8000"),)

    attached = attach_current_values(tmp_path, candidates)

    assert attached == candidates
    assert attached[0].current is None


def test_service_key_precedes_generic_and_unknown_keys_are_ignored(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text(
        "PORT='7000'\nWEB_PORT='8000'\nEMPTY=\nUNKNOWN='ignored'\n",
        encoding="utf-8",
    )
    candidates = (
        _candidate("web", "PORT"),
        _candidate("worker", "PORT"),
        _candidate("web", "EMPTY"),
    )

    attached = attach_current_values(tmp_path, candidates)

    assert [item.current.value if item.current else None for item in attached] == [
        "8000",
        "7000",
        "",
    ]
    assert all(
        item.current is not None
        and item.current.source == SourceRef(path=CURRENT_ENV_SOURCE)
        for item in attached
    )
    assert {(item.service, item.name) for item in attached} == {
        ("web", "PORT"),
        ("worker", "PORT"),
        ("web", "EMPTY"),
    }


def test_matched_key_without_value_is_rejected(tmp_path: Path) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT\n", encoding="utf-8")

    with pytest.raises(UsageError, match="PORT.*没有具体值"):
        attach_current_values(tmp_path, (_candidate("web", "PORT"),))


def test_current_snapshot_does_not_interpolate_dollar_values(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("API_KEY=${OTHER}\n", encoding="utf-8")

    attached = attach_current_values(
        tmp_path,
        (_candidate("web", "API_KEY"),),
    )

    assert attached[0].current is not None
    assert attached[0].current.value == "${OTHER}"


def test_artifact_component_matches_existing_service_prefix_rule() -> None:
    assert artifact_component("api-web") == "API_WEB"
    assert artifact_component("worker_2") == "WORKER_2"


def test_missing_mount_snapshot_preserves_file_candidates(tmp_path: Path) -> None:
    candidates = (_bind("/project/data", inside=True),)

    attached = attach_current_mounts(tmp_path, candidates)

    assert attached == candidates
    assert attached[0].current_action is None


def test_image_snapshot_is_ignored_after_original_image_changes(tmp_path: Path) -> None:
    snapshot = tmp_path / CURRENT_IMAGES_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps({
            "schema_version": 1,
            "images": [{
                "service": "db",
                "original_image": "postgres:16",
                "decision": "registry.intra/postgres:16",
            }],
        }),
        encoding="utf-8",
    )

    attached = attach_current_images(
        tmp_path,
        (ImageCandidate(service="db", image="postgres:17"),),
    )

    assert attached[0].current_decision is None


def test_image_snapshot_matches_uninterpolated_compose_image(tmp_path: Path) -> None:
    snapshot = tmp_path / CURRENT_IMAGES_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps({
            "schema_version": 1,
            "images": [{
                "service": "db",
                "original_image": "${DB_IMAGE}",
                "decision": "registry.intra/postgres:16",
            }],
        }),
        encoding="utf-8",
    )

    attached = attach_current_images(
        tmp_path,
        (ImageCandidate(service="db", image="${DB_IMAGE}"),),
    )

    assert attached[0].current_decision == "registry.intra/postgres:16"


def test_invalid_image_snapshot_stops_inspection(tmp_path: Path) -> None:
    snapshot = tmp_path / CURRENT_IMAGES_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(
        '{"schema_version":1,"images":[{"service":"db",'
        '"original_image":"postgres:16","decision":"not valid image"}]}',
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match="当前镜像快照无效"):
        attach_current_images(
            tmp_path,
            (ImageCandidate(service="db", image="postgres:16"),),
        )


def test_attach_current_mounts_matches_paths_and_ignores_stale_entries(
    tmp_path: Path,
) -> None:
    _write_mount_snapshot(
        tmp_path,
        [
            {"resolved_path": "/project/data", "action": "copy"},
            {"resolved_path": "/removed", "action": "keep_server_path"},
        ],
    )
    candidates = (
        _bind("/project/data", inside=True),
        _bind("/project/new", inside=True),
    )

    attached = attach_current_mounts(tmp_path, candidates)

    assert attached[0].current_action is FileAction.COPY
    assert attached[1].current_action is None


@pytest.mark.parametrize(
    "body",
    (
        "{",
        '{"schema_version":2,"mounts":[]}',
        (
            '{"schema_version":1,"mounts":['
            '{"resolved_path":"/project/data","action":"copy"},'
            '{"resolved_path":"/project/data/../data",'
            '"action":"keep_server_path"}]}'
        ),
        (
            '{"schema_version":1,"mounts":['
            '{"resolved_path":"/project/data","action":"abort"}]}'
        ),
    ),
)
def test_attach_current_mounts_rejects_invalid_snapshot(
    tmp_path: Path,
    body: str,
) -> None:
    snapshot = tmp_path / CURRENT_MOUNTS_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(body, encoding="utf-8")

    with pytest.raises(UsageError, match="当前挂载快照"):
        attach_current_mounts(
            tmp_path,
            (_bind("/project/data", inside=True),),
        )


def test_external_bind_rejects_current_copy_action(tmp_path: Path) -> None:
    _write_mount_snapshot(
        tmp_path,
        [{"resolved_path": "/srv/data", "action": "copy"}],
    )

    with pytest.raises(UsageError, match="项目目录外.*copy"):
        attach_current_mounts(
            tmp_path,
            (_bind("/srv/data", inside=False),),
        )


def test_attach_current_ports_matches_identity_and_ignores_unknown(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / CURRENT_PORTS_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ports": [
                    {
                        "service": "web",
                        "container_port": 8000,
                        "protocol": "tcp",
                        "exposed": True,
                        "host_port": 8322,
                    },
                    {
                        "service": "removed",
                        "container_port": 9000,
                        "protocol": "tcp",
                        "exposed": False,
                        "host_port": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    candidates = (
        PortCandidate(service="web", container_port=8000, host_port=8080),
        PortCandidate(service="web", container_port=8000, protocol="udp"),
    )

    attached = attach_current_ports(tmp_path, candidates)

    assert attached[0].current is not None
    assert attached[0].current.exposed is True
    assert attached[0].current.host_port == 8322
    assert attached[1].current is None
    assert len(attached) == len(candidates)


@pytest.mark.parametrize(
    "body",
    (
        "{",
        '{"schema_version":2,"ports":[]}',
        (
            '{"schema_version":1,"ports":['
            '{"service":"web","container_port":8000,"protocol":"tcp",'
            '"exposed":true,"host_port":null}]}'
        ),
    ),
)
def test_attach_current_ports_rejects_invalid_snapshot(
    tmp_path: Path,
    body: str,
) -> None:
    snapshot = tmp_path / CURRENT_PORTS_RELATIVE
    snapshot.parent.mkdir()
    snapshot.write_text(body, encoding="utf-8")

    with pytest.raises(UsageError, match="当前端口快照"):
        attach_current_ports(
            tmp_path,
            (PortCandidate(service="web", container_port=8000),),
        )


def test_write_current_environment_is_complete_sorted_and_private(
    tmp_path: Path,
) -> None:
    previous = tmp_path / ".docker-manage/.env"
    previous.parent.mkdir()
    previous.write_text("STALE='remove-me'\n", encoding="utf-8")

    path = write_current_environment(
        tmp_path,
        (
            EnvAssignment(
                service="web",
                container_name="PORT",
                artifact_name="PORT",
                value="8322",
            ),
            EnvAssignment(
                service="web",
                container_name="API_KEY",
                artifact_name="API_KEY",
                value="secret",
            ),
        ),
    )

    assert path == tmp_path / ".docker-manage/.env"
    assert dotenv_values(path) == {"API_KEY": "secret", "PORT": "8322"}
    assert path.read_text(encoding="utf-8").splitlines() == [
        "API_KEY=secret",
        "PORT=8322",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_current_environment_keeps_docker_envfile_values_unquoted(
    tmp_path: Path,
) -> None:
    path = write_current_environment(
        tmp_path,
        (
            EnvAssignment(
                service="web",
                container_name="API_KEY",
                artifact_name="API_KEY",
                value="token-${OTHER} with spaces:中文!$?",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        "API_KEY=token-${OTHER} with spaces:中文!$?\n"
    )


def test_write_current_ports_is_complete_sorted_and_private(
    tmp_path: Path,
) -> None:
    path = write_current_ports(
        tmp_path,
        (
            PortAssignment(
                service="worker",
                container_port=9000,
                exposed=False,
            ),
            PortAssignment(
                service="web",
                container_port=8000,
                exposed=True,
                host_port=8322,
            ),
        ),
    )

    assert path == tmp_path / ".docker-manage/ports.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "ports": [
            {
                "service": "web",
                "container_port": 8000,
                "protocol": "tcp",
                "exposed": True,
                "host_port": 8322,
            },
            {
                "service": "worker",
                "container_port": 9000,
                "protocol": "tcp",
                "exposed": False,
                "host_port": None,
            },
        ],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_current_mounts_is_bind_only_deduplicated_and_private(
    tmp_path: Path,
) -> None:
    path = write_current_mounts(
        tmp_path,
        (
            _file_assignment("web", "/project/data", "bind", FileAction.COPY),
            _file_assignment("worker", "/project/data", "bind", FileAction.COPY),
            _file_assignment(
                "web",
                "/project/server-data",
                "bind",
                FileAction.KEEP_SERVER_PATH,
            ),
            _file_assignment(
                "web",
                "/project/app.ini",
                "config",
                FileAction.COPY,
            ),
        ),
    )

    assert path == tmp_path / ".docker-manage/mounts.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "mounts": [
            {"resolved_path": "/project/data", "action": "copy"},
            {
                "resolved_path": "/project/server-data",
                "action": "keep_server_path",
            },
        ],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_failure_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / ".docker-manage/.env"
    snapshot.parent.mkdir()
    snapshot.write_text("PORT='last-good'\n", encoding="utf-8")
    monkeypatch.setattr(
        "docker_package_app.current_config.os.replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(PackageError, match="无法更新当前环境变量快照"):
        write_current_environment(
            tmp_path,
            (
                EnvAssignment(
                    service="web",
                    container_name="PORT",
                    artifact_name="PORT",
                    value="next",
                ),
            ),
        )

    assert snapshot.read_text(encoding="utf-8") == "PORT='last-good'\n"


def test_configuration_write_restores_environment_when_ports_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".docker-manage/.env"
    ports_path = tmp_path / ".docker-manage/ports.json"
    env_path.parent.mkdir()
    env_path.write_text("PORT='last-good'\n", encoding="utf-8")
    ports_path.write_text('{"schema_version":1,"ports":[]}\n', encoding="utf-8")

    def fail_ports(_root: Path, _ports: object) -> Path:
        raise PackageError("ports failed")

    monkeypatch.setattr(
        "docker_package_app.current_config.write_current_ports",
        fail_ports,
    )

    with pytest.raises(PackageError, match="ports failed"):
        write_current_configuration(
            tmp_path,
            (
                EnvAssignment(
                    service="web",
                    container_name="PORT",
                    artifact_name="PORT",
                    value="next",
                ),
            ),
            (
                PortAssignment(
                    service="web",
                    container_port=8000,
                    exposed=True,
                    host_port=8322,
                ),
            ),
        )

    assert env_path.read_text(encoding="utf-8") == "PORT='last-good'\n"
    assert ports_path.read_text(encoding="utf-8") == (
        '{"schema_version":1,"ports":[]}\n'
    )


def test_configuration_write_restores_all_snapshots_when_mounts_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".docker-manage/.env"
    ports_path = tmp_path / ".docker-manage/ports.json"
    mounts_path = tmp_path / ".docker-manage/mounts.json"
    env_path.parent.mkdir()
    env_path.write_text("PORT='last-good'\n", encoding="utf-8")
    ports_path.write_text(
        '{"schema_version":1,"ports":[]}\n',
        encoding="utf-8",
    )
    mounts_path.write_text(
        '{"schema_version":1,"mounts":[]}\n',
        encoding="utf-8",
    )
    env_path.chmod(0o640)
    ports_path.chmod(0o644)
    mounts_path.chmod(0o600)
    previous = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (env_path, ports_path, mounts_path)
    }

    def fail_mounts(_root: Path, _files: object) -> Path:
        raise PackageError("mounts failed")

    monkeypatch.setattr(
        "docker_package_app.current_config.write_current_mounts",
        fail_mounts,
    )

    with pytest.raises(PackageError, match="mounts failed"):
        write_current_configuration(
            tmp_path,
            (
                EnvAssignment(
                    service="web",
                    container_name="PORT",
                    artifact_name="PORT",
                    value="next",
                ),
            ),
            (
                PortAssignment(
                    service="web",
                    container_port=8000,
                    exposed=True,
                    host_port=8322,
                ),
            ),
            (
                _file_assignment(
                    "web",
                    "/project/data",
                    "bind",
                    FileAction.COPY,
                ),
            ),
        )

    for path, (body, mode) in previous.items():
        assert path.read_bytes() == body
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_configuration_write_removes_new_mount_snapshot_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".docker-manage/.env"
    ports_path = tmp_path / ".docker-manage/ports.json"
    mounts_path = tmp_path / ".docker-manage/mounts.json"
    env_path.parent.mkdir()
    env_path.write_text("PORT='last-good'\n", encoding="utf-8")
    ports_path.write_text(
        '{"schema_version":1,"ports":[]}\n',
        encoding="utf-8",
    )

    def write_then_fail(root: Path, _files: object) -> Path:
        target = root / CURRENT_MOUNTS_RELATIVE
        target.write_text("new", encoding="utf-8")
        raise PackageError("mounts failed")

    monkeypatch.setattr(
        "docker_package_app.current_config.write_current_mounts",
        write_then_fail,
    )

    with pytest.raises(PackageError, match="mounts failed"):
        write_current_configuration(tmp_path, (), (), ())

    assert not mounts_path.exists()


def test_configuration_write_restores_all_snapshots_when_images_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / ".docker-manage"
    current.mkdir()
    previous = {
        current / ".env": "PORT='last-good'\n",
        current / "ports.json": '{"schema_version":1,"ports":[]}\n',
        current / "mounts.json": '{"schema_version":1,"mounts":[]}\n',
        current / "images.json": '{"schema_version":1,"images":[]}\n',
    }
    for path, body in previous.items():
        path.write_text(body, encoding="utf-8")

    def write_then_fail(root: Path, _images: object) -> Path:
        target = root / CURRENT_IMAGES_RELATIVE
        target.write_text("partial", encoding="utf-8")
        raise PackageError("images failed")

    monkeypatch.setattr(
        "docker_package_app.current_config.write_current_images",
        write_then_fail,
    )

    with pytest.raises(PackageError, match="images failed"):
        write_current_configuration(tmp_path, (), (), (), ())

    assert {path: path.read_text(encoding="utf-8") for path in previous} == previous
