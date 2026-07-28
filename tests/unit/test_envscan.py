from pathlib import Path

from docker_package_app.compose import ComposeDocument
from docker_package_app.envscan import scan_build_args, scan_environment


def _project() -> tuple[Path, ComposeDocument, dict[str, Path], dict[str, Path]]:
    root = Path("tests/fixtures/env-project").resolve()
    compose = ComposeDocument.from_data(
        root,
        {
            "services": {
                "web": {
                    "build": {"context": "web"},
                    "environment": {"PORT": "${PORT:-7000}"},
                    "env_file": [".env.example"],
                },
                "worker": {"build": {"context": "worker"}},
            }
        },
    )
    roots = {"web": root / "web", "worker": root / "worker"}
    dockerfiles = {
        "web": root / "web/Dockerfile",
        "worker": root / "worker/Dockerfile",
    }
    return root, compose, roots, dockerfiles


def test_scanner_collects_only_explicit_env_reads() -> None:
    root, compose, roots, dockerfiles = _project()

    found = scan_environment(root, roots, compose, dockerfiles)

    keys = {(item.service, item.name) for item in found}
    assert keys >= {
        ("web", "PORT"),
        ("web", "API_KEY"),
        ("web", "DATABASE_URL"),
        ("web", "LOG_LEVEL"),
        ("web", "PYTHONUNBUFFERED"),
        ("worker", "CACHE_URL"),
    }
    assert ("web", "HARDCODED_URL") not in keys


def test_conflicting_defaults_keep_all_sources() -> None:
    root, compose, roots, dockerfiles = _project()

    port = next(
        item
        for item in scan_environment(root, roots, compose, dockerfiles)
        if item.service == "web" and item.name == "PORT"
    )

    assert {default.value for default in port.defaults} == {"7000", "8000", "8080"}


def test_required_dockerfile_arg_is_collected() -> None:
    _, _, _, dockerfiles = _project()

    args = scan_build_args(dockerfiles)

    assert [(item.service, item.name, item.default) for item in args] == [
        ("web", "APP_MODE", "prod"),
        ("web", "PRIVATE_INDEX_URL", None),
        ("worker", "WORKER_QUEUE", "default"),
    ]
