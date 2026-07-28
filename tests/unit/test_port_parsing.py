import pytest
from docker_package_app.cli import _compose_port


@pytest.mark.parametrize(
    "raw",
    (
        "${PDF_TRANS_WEB_PORT:-8000}:8000",
        "${PDF_TRANS_WEB_PORT:?error}:8000",
        "${PDF_TRANS_WEB_PORT:+8322}:8000",
    ),
)
def test_compose_port_ignores_colons_inside_interpolation(raw: str) -> None:
    candidate = _compose_port("web", raw)

    assert candidate is not None
    assert candidate.container_port == 8000
    assert candidate.protocol == "tcp"
    assert candidate.host_ip is None
    assert candidate.host_port is None


@pytest.mark.parametrize(
    ("raw", "host_ip", "host_port"),
    (
        ("8322:8000", None, 8322),
        ("127.0.0.1:8322:8000", "127.0.0.1", 8322),
    ),
)
def test_compose_port_preserves_ipv4_short_syntax(
    raw: str,
    host_ip: str | None,
    host_port: int,
) -> None:
    candidate = _compose_port("web", raw)

    assert candidate is not None
    assert candidate.container_port == 8000
    assert candidate.protocol == "tcp"
    assert candidate.host_ip == host_ip
    assert candidate.host_port == host_port
