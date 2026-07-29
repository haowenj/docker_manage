import json
import stat
from pathlib import Path

import pytest
from docker_package_app.current_config import (
    CURRENT_ENV_SOURCE,
    CURRENT_PORTS_RELATIVE,
    artifact_component,
    attach_current_ports,
    attach_current_values,
    write_current_configuration,
    write_current_environment,
    write_current_ports,
)
from docker_package_app.errors import PackageError, UsageError
from docker_package_app.models import (
    DefaultValue,
    EnvAssignment,
    EnvCandidate,
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


def test_artifact_component_matches_existing_service_prefix_rule() -> None:
    assert artifact_component("api-web") == "API_WEB"
    assert artifact_component("worker_2") == "WORKER_2"


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
        "API_KEY='secret'",
        "PORT='8322'",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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
