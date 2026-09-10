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
        ("worker", "CACHE_URL"),
    }
    assert ("web", "PYTHONUNBUFFERED") not in keys
    assert ("web", "HARDCODED_URL") not in keys


def test_dockerfile_only_env_is_not_a_deployment_candidate(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        'FROM scratch\nENV PATH="/app/.venv/bin:${PATH}" APP_HOME=/app\n',
        encoding="utf-8",
    )
    compose = ComposeDocument.from_data(
        tmp_path,
        {"services": {"web": {"build": {"context": "."}}}},
    )

    found = scan_environment(
        tmp_path,
        {"web": tmp_path},
        compose,
        {"web": dockerfile},
    )

    assert {(item.service, item.name) for item in found} == set()


def test_compose_environment_keeps_explicit_value_over_dockerfile_env(
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM scratch\nENV APP_MODE=container\n",
        encoding="utf-8",
    )
    compose = ComposeDocument.from_data(
        tmp_path,
        {
            "services": {
                "web": {
                    "build": {"context": "."},
                    "environment": {"APP_MODE": "production"},
                }
            }
        },
    )

    found = scan_environment(
        tmp_path,
        {"web": tmp_path},
        compose,
        {"web": dockerfile},
    )

    candidate = next(item for item in found if item.name == "APP_MODE")
    assert [item.value for item in candidate.defaults] == ["production"]
    assert all(item.path == "compose" for item in candidate.sources)


def test_compose_env_file_keeps_explicit_path_over_dockerfile_env(
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM scratch\nENV PATH=/image/bin:${PATH}\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.example"
    env_file.write_text("PATH=/deployment/bin\n", encoding="utf-8")
    compose = ComposeDocument.from_data(
        tmp_path,
        {
            "services": {
                "web": {
                    "build": {"context": "."},
                    "env_file": [".env.example"],
                }
            }
        },
    )

    found = scan_environment(
        tmp_path,
        {"web": tmp_path},
        compose,
        {"web": dockerfile},
    )

    candidate = next(item for item in found if item.name == "PATH")
    assert [item.value for item in candidate.defaults] == ["/deployment/bin"]
    assert all(item.path == ".env.example" for item in candidate.sources)


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


def test_compose_interpolation_outside_environment_is_collected() -> None:
    root, _, roots, dockerfiles = _project()
    compose = ComposeDocument.from_data(
        root,
        {
            "services": {
                "web": {
                    "build": {"context": "web"},
                    "command": ["serve", "--workers", "${WORKERS:-4}"],
                    "volumes": ["${CONFIG_PATH}:/app/config:ro"],
                },
                "worker": {"build": {"context": "worker"}},
            }
        },
    )

    found = scan_environment(root, roots, compose, dockerfiles)
    by_name = {(item.service, item.name): item for item in found}

    assert {default.value for default in by_name[("web", "WORKERS")].defaults} == {"4"}
    assert ("web", "CONFIG_PATH") in by_name


def test_scanner_ignores_test_directories(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        'runtime = os.getenv("RUNTIME_VALUE")\n',
        encoding="utf-8",
    )
    for directory, variable in (
        ("test", "TEST_VALUE"),
        ("tests", "TESTS_VALUE"),
        ("__tests__", "JS_TESTS_VALUE"),
        ("spec", "SPEC_VALUE"),
        ("specs", "SPECS_VALUE"),
    ):
        test_directory = tmp_path / directory
        test_directory.mkdir()
        (test_directory / "test_app.py").write_text(
            f'test_only = os.getenv("{variable}")\n',
            encoding="utf-8",
        )
    compose = ComposeDocument.from_data(
        tmp_path,
        {"services": {"web": {"build": {"context": "."}}}},
    )

    found = scan_environment(tmp_path, {"web": tmp_path}, compose, {})

    assert {(item.service, item.name) for item in found} == {
        ("web", "RUNTIME_VALUE"),
    }


def test_scanner_ignores_test_named_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        'runtime = os.getenv("RUNTIME_VALUE")\n',
        encoding="utf-8",
    )
    for filename, source in (
        ("test_helpers.py", 'os.getenv("PYTHON_PREFIX_VALUE")\n'),
        ("helpers_test.py", 'os.environ["PYTHON_SUFFIX_VALUE"]\n'),
        ("app.test.js", "process.env.JS_TEST_VALUE;\n"),
        ("app.spec.ts", "process.env.TS_SPEC_VALUE;\n"),
        ("main_test.go", 'os.Getenv("GO_TEST_VALUE")\n'),
        ("service_spec.rb", 'ENV["RUBY_SPEC_VALUE"]\n'),
    ):
        (tmp_path / filename).write_text(
            source,
            encoding="utf-8",
        )
    compose = ComposeDocument.from_data(
        tmp_path,
        {"services": {"web": {"build": {"context": "."}}}},
    )

    found = scan_environment(tmp_path, {"web": tmp_path}, compose, {})

    assert {(item.service, item.name) for item in found} == {
        ("web", "RUNTIME_VALUE"),
    }
