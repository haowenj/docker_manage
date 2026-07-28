import json
from pathlib import Path

import pytest
from docker_package_app.command import CommandResult
from docker_package_app.compose import ComposeDocument
from docker_package_app.docker import (
    EXPORT_RESERVE_BYTES,
    DockerEngine,
    ImageMetadata,
    prepare_build_compose,
)
from docker_package_app.errors import PackageError
from docker_package_app.models import (
    DiskEstimate,
    ImageAction,
    ImageAssignment,
    PackagePlan,
)


class RecordingRunner:
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.outputs = list(outputs or [])

    def run(self, argv, **kwargs):
        call = list(argv)
        self.calls.append((call, kwargs))
        stdout = self.outputs.pop(0) if self.outputs else ""
        return CommandResult(call, 0, stdout, "")


def _plan(root: Path) -> PackagePlan:
    return PackagePlan(
        run_id="run-1",
        project_root=str(root),
        app_name="demo",
        version="v1",
        compose_project_name="demo",
        platform="linux/amd64",
        images=(
            ImageAssignment(
                service="web",
                original_image=None,
                final_image="docker-manage/demo/web:v1",
                action=ImageAction.BUILD,
                platform="linux/amd64",
            ),
        ),
        disk=DiskEstimate(known_input_bytes=0, free_bytes=10_000_000),
    )


def test_pull_and_save_use_argv_without_shell(tmp_path: Path) -> None:
    runner = RecordingRunner()
    engine = DockerEngine(runner)

    engine.pull("redis:7", "linux/amd64")
    engine.save(["demo/web:v1", "redis:7"], tmp_path / "images.tar")

    assert [call for call, _ in runner.calls] == [
        ["docker", "pull", "--platform", "linux/amd64", "redis:7"],
        [
            "docker",
            "image",
            "save",
            "--output",
            str(tmp_path / "images.tar"),
            "demo/web:v1",
            "redis:7",
        ],
    ]


def test_build_uses_temporary_compose_and_selected_services(tmp_path: Path) -> None:
    runner = RecordingRunner()

    DockerEngine(runner).build(
        tmp_path / "build.compose.yaml",
        ["web", "worker"],
        "linux/amd64",
    )

    assert runner.calls[-1][0] == [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "build.compose.yaml"),
        "build",
        "web",
        "worker",
    ]
    assert runner.calls[-1][1]["env"] == {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"}


def test_inspect_rejects_wrong_platform() -> None:
    metadata = {
        "Id": "sha256:abc",
        "RepoDigests": ["demo@sha256:def"],
        "Os": "linux",
        "Architecture": "arm64",
        "Variant": "v8",
        "Size": 1024,
    }
    engine = DockerEngine(RecordingRunner([json.dumps(metadata)]))

    with pytest.raises(PackageError, match="platform"):
        engine.inspect(["demo:v1"], "linux/amd64")


def test_export_rejects_insufficient_disk_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = ImageMetadata(
        reference="demo:v1",
        image_id="sha256:abc",
        repo_digests=(),
        os="linux",
        architecture="amd64",
        variant=None,
        size=1_024,
    )
    required = 2 * image.size + 512 + EXPORT_RESERVE_BYTES
    disk_usage = __import__("shutil")._ntuple_diskusage(  # type: ignore[attr-defined]
        required * 2,
        required * 2 - required + 1,
        required - 1,
    )
    monkeypatch.setattr("docker_package_app.docker.shutil.disk_usage", lambda _: disk_usage)

    with pytest.raises(PackageError) as caught:
        DockerEngine(RecordingRunner()).ensure_export_space(
            [image],
            file_bytes=512,
            output_dir=tmp_path,
        )

    assert str(required) in caught.value.message
    assert str(required - 1) in caught.value.message
    assert caught.value.hint


def test_prepare_build_compose_preserves_source_files(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    ignore = tmp_path / ".dockerignore"
    ignore.write_text("dist/\n", encoding="utf-8")
    original_dockerfile = dockerfile.read_bytes()
    original_ignore = ignore.read_bytes()
    compose = ComposeDocument.from_data(
        tmp_path,
        {"services": {"web": {"build": {"context": ".", "dockerfile": "Dockerfile"}}}},
    )

    output = prepare_build_compose(compose, _plan(tmp_path), tmp_path / "work")

    rendered = ComposeDocument.from_data(tmp_path, __import__("yaml").safe_load(output.read_text()))
    temporary_dockerfile = Path(rendered.service("web")["build"]["dockerfile"])
    assert temporary_dockerfile.read_bytes() == original_dockerfile
    assert temporary_dockerfile.with_name("Dockerfile.dockerignore").read_text().endswith(
        "\n.docker-manage/\n"
    )
    assert dockerfile.read_bytes() == original_dockerfile
    assert ignore.read_bytes() == original_ignore
