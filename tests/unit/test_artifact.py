from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from docker_package_app.artifact import (
    build_manifest,
    create_verified_archive,
    verify_payload,
    write_checksums,
)
from docker_package_app.docker import ImageMetadata
from docker_package_app.errors import ArtifactVerificationError
from docker_package_app.models import (
    DiskEstimate,
    FileAction,
    FileAssignment,
    ImageAction,
    ImageAssignment,
    PackagePlan,
)


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    root = tmp_path / "payload"
    (root / "files").mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / ".env").write_text("PORT='8000'\n", encoding="utf-8")
    (root / "images.tar").write_bytes(b"image-tar")
    (root / "files/app.ini").write_text("mode=prod\n", encoding="utf-8")
    (root / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    write_checksums(root)
    return root


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
            ImageAssignment(
                service="redis",
                original_image="redis:7",
                final_image="registry.intra/redis:7",
                action=ImageAction.REUSE,
                platform="linux/amd64",
            ),
        ),
        files=(
            FileAssignment(
                service="web",
                original_value="/srv/shared",
                resolved_path="/srv/shared",
                action=FileAction.KEEP_SERVER_PATH,
            ),
        ),
        disk=DiskEstimate(known_input_bytes=0, free_bytes=1_000_000_000),
    )


def test_manifest_records_images_dependencies_and_payload_hashes(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (payload_root / ".env").write_text("", encoding="utf-8")
    metadata = ImageMetadata(
        reference="docker-manage/demo/web:v1",
        image_id="sha256:abc",
        repo_digests=("demo@sha256:def",),
        os="linux",
        architecture="amd64",
        variant=None,
        size=123,
    )

    manifest = build_manifest(_plan(tmp_path), [metadata], payload_root)
    body = json.loads(manifest.model_dump_json())

    assert body["schema_version"] == 1
    assert body["cli_version"] == "0.1.0"
    assert body["service_images"] == {
        "redis": "registry.intra/redis:7",
        "web": "docker-manage/demo/web:v1",
    }
    assert body["packaged_images"][0]["image_id"] == "sha256:abc"
    assert body["reused_images"] == ["registry.intra/redis:7"]
    assert body["server_paths"] == ["/srv/shared"]
    assert [item["path"] for item in body["payload_files"]] == [".env", "compose.yaml"]


def test_archive_contains_verified_payload_and_is_reproducible(
    payload: Path,
    tmp_path: Path,
) -> None:
    first = create_verified_archive(payload, tmp_path / "demo-v1.tar.gz")
    second = create_verified_archive(payload, tmp_path / "demo-v1-copy.tar.gz")

    assert first.exists()
    assert first.stat().st_mode & 0o777 == 0o600
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        assert {
            "manifest.json",
            "compose.yaml",
            ".env",
            "images.tar",
            "checksums.sha256",
            "files/app.ini",
        }.issubset(archive.getnames())


def test_verifier_rejects_checksum_tampering(payload: Path) -> None:
    checksums = payload / "checksums.sha256"
    (payload / "compose.yaml").write_text("services:\n  changed: {}\n", encoding="utf-8")

    with pytest.raises(ArtifactVerificationError, match="校验和"):
        verify_payload(payload, checksums)


def test_archive_rejects_symlink_escape(payload: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (payload / "files/outside").symlink_to(outside)

    with pytest.raises(ArtifactVerificationError, match="符号链接"):
        create_verified_archive(payload, tmp_path / "bad.tar.gz")
    assert not (tmp_path / "bad.tar.gz").exists()
    assert not tuple(tmp_path.glob("bad.tar.gz.partial-*"))
