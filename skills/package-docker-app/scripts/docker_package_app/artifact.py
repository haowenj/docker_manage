from __future__ import annotations

import gzip
import hashlib
import os
import posixpath
import stat
import tarfile
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4

from docker_package_app import ARTIFACT_SCHEMA_VERSION, CLI_VERSION
from docker_package_app.docker import ImageMetadata
from docker_package_app.errors import ArtifactVerificationError
from docker_package_app.files import deployment_source
from docker_package_app.models import FileAction, ImageAction, PackagePlan, StrictModel

HASH_CHUNK_BYTES = 1024 * 1024


class PayloadFile(StrictModel):
    path: str
    size: int
    sha256: str


class PackagedImage(StrictModel):
    reference: str
    image_id: str
    repo_digests: tuple[str, ...]
    platform: str
    size: int
    archived: bool = True


class Manifest(StrictModel):
    schema_version: int
    cli_version: str
    app_name: str
    version: str
    compose_project_name: str
    platform: str
    created_at: str
    service_images: dict[str, str]
    packaged_images: tuple[PackagedImage, ...]
    reused_images: tuple[str, ...]
    server_paths: tuple[str, ...]
    payload_files: tuple[PayloadFile, ...]


def build_manifest(
    plan: PackagePlan,
    image_metadata: Sequence[ImageMetadata],
    payload_root: Path,
) -> Manifest:
    root = payload_root.resolve()
    metadata_by_reference = {item.reference: item for item in image_metadata}
    packaged_references = sorted(
        {
            item.final_image
            for item in plan.images
            if item.action in {ImageAction.BUILD, ImageAction.PACKAGE}
        }
    )
    packaged: list[PackagedImage] = []
    for reference in packaged_references:
        item = metadata_by_reference.get(reference)
        if item is None:
            raise ArtifactVerificationError(
                f"待打包镜像缺少已检查的元数据：{reference}"
            )
        if item.platform != plan.platform:
            raise ArtifactVerificationError(
                f"待打包镜像 {reference} 的平台 {item.platform} 与 {plan.platform} 不匹配"
            )
        packaged.append(
            PackagedImage(
                reference=reference,
                image_id=item.image_id,
                repo_digests=item.repo_digests,
                platform=item.platform,
                size=item.size,
            )
        )

    payload_files = tuple(
        PayloadFile(
            path=relative,
            size=path.stat().st_size,
            sha256=_sha256(path),
        )
        for relative, path in _regular_files(
            root,
            excluded={"manifest.json", "checksums.sha256"},
        )
    )
    return Manifest(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        cli_version=CLI_VERSION,
        app_name=plan.app_name,
        version=plan.version,
        compose_project_name=plan.compose_project_name,
        platform=plan.platform,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        service_images={
            item.service: item.final_image
            for item in sorted(plan.images, key=lambda value: value.service)
        },
        packaged_images=tuple(packaged),
        reused_images=tuple(
            sorted(
                {
                    item.final_image
                    for item in plan.images
                    if item.action is ImageAction.REUSE
                }
            )
        ),
        server_paths=tuple(
            sorted(
                {
                    deployment_source(
                        Path(plan.project_root),
                        item.resolved_path,
                        item.original_value,
                    )
                    for item in plan.files
                    if item.action is FileAction.KEEP_SERVER_PATH
                }
            )
        ),
        payload_files=payload_files,
    )


def write_checksums(payload_root: Path) -> Path:
    root = payload_root.resolve()
    _validate_payload_tree(root)
    entries = _regular_files(root, excluded={"checksums.sha256"})
    lines: list[str] = []
    for relative, path in entries:
        if "\n" in relative or "\r" in relative:
            raise ArtifactVerificationError(f"制品载荷路径包含不安全的换行符：{relative!r}")
        lines.append(f"{_sha256(path)}  {relative}\n")
    destination = root / "checksums.sha256"
    _atomic_write_text(destination, "".join(lines))
    return destination


def verify_payload(payload_root: Path, checksums_path: Path) -> None:
    root = payload_root.resolve()
    _validate_payload_tree(root)
    checksums = checksums_path.resolve()
    if checksums.parent != root or not checksums.is_file():
        raise ArtifactVerificationError("校验和文件必须是载荷根目录中的普通文件")

    expected_files = {
        relative: path
        for relative, path in _regular_files(root, excluded={checksums.name})
    }
    declared: dict[str, str] = {}
    for line_number, line in enumerate(
        checksums.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not _safe_member_name(relative)
            or relative in declared
        ):
            raise ArtifactVerificationError(
                f"第 {line_number} 行的校验和条目无效"
            )
        declared[relative] = digest

    if set(declared) != set(expected_files):
        missing = sorted(set(expected_files) - set(declared))
        unexpected = sorted(set(declared) - set(expected_files))
        raise ArtifactVerificationError(
            f"校验和文件清单不匹配：缺少={missing}，多余={unexpected}"
        )
    for relative, expected in declared.items():
        actual = _sha256(expected_files[relative])
        if actual != expected:
            raise ArtifactVerificationError(f"文件 {relative} 的校验和不匹配")


def create_verified_archive(payload_root: Path, destination: Path) -> Path:
    root = payload_root.resolve()
    destination = destination.resolve(strict=False)
    checksums = root / "checksums.sha256"
    verify_payload(root, checksums)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.partial-{uuid4().hex}")

    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as raw:
            _write_archive_stream(root, raw)
            raw.flush()
            os.fsync(raw.fileno())
        partial.chmod(0o600)
        _verify_archive(partial, root)
        os.replace(partial, destination)
        destination.chmod(0o600)
        return destination
    except (ArtifactVerificationError, OSError, tarfile.TarError) as exc:
        if isinstance(exc, ArtifactVerificationError):
            raise
        raise ArtifactVerificationError(
            f"无法创建已验证的归档：{exc}"
        ) from exc
    finally:
        if partial.exists():
            partial.unlink()


def _verify_archive(archive_path: Path, payload_root: Path) -> None:
    expected = {relative: path for relative, path in _payload_entries(payload_root)}
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not _safe_member_name(member.name) or member.name in seen:
                    raise ArtifactVerificationError(
                        f"归档成员不安全或重复：{member.name!r}"
                    )
                seen.add(member.name)
                source = expected.get(member.name)
                if source is None:
                    raise ArtifactVerificationError(
                        f"归档包含意外成员：{member.name}"
                    )
                if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise ArtifactVerificationError(
                        f"归档成员类型不安全：{member.name}"
                    )
                if member.issym() or member.islnk():
                    if not _safe_link_target(member):
                        raise ArtifactVerificationError(
                            f"归档符号链接指向载荷之外：{member.name}"
                        )
                    continue
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ArtifactVerificationError(
                            f"无法读取归档成员：{member.name}"
                        )
                    if _sha256_file_object(extracted) != _sha256(source):
                        raise ArtifactVerificationError(
                            f"归档成员 {member.name} 的校验和不匹配"
                        )
            if seen != set(expected):
                missing = sorted(set(expected) - seen)
                raise ArtifactVerificationError(f"归档缺少成员：{missing}")
    except tarfile.TarError as exc:
        raise ArtifactVerificationError(f"归档无效：{exc}") from exc


def _regular_files(root: Path, excluded: set[str]) -> list[tuple[str, Path]]:
    return [
        (relative, path)
        for relative, path in _payload_entries(root)
        if relative not in excluded and path.is_file() and not path.is_symlink()
    ]


def _payload_entries(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        raise ArtifactVerificationError(f"制品载荷根路径不是目录：{root}")
    entries: list[tuple[str, Path]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            entries.append((relative, path))
    return sorted(entries, key=lambda item: item[0])


def _validate_payload_tree(root: Path) -> None:
    for relative, path in _payload_entries(root):
        if not _safe_member_name(relative):
            raise ArtifactVerificationError(f"制品载荷路径不安全：{relative!r}")
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            target = path.resolve(strict=False)
            if not target.is_relative_to(root):
                raise ArtifactVerificationError(f"符号链接指向制品载荷之外：{relative}")
        elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ArtifactVerificationError(f"制品载荷文件类型不受支持：{relative}")


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_file_object(handle)


def _sha256_file_object(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = handle.read(HASH_CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _safe_member_name(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _safe_link_target(member: tarfile.TarInfo) -> bool:
    if PurePosixPath(member.linkname).is_absolute():
        return False
    base = posixpath.dirname(member.name) if member.issym() else ""
    normalized = posixpath.normpath(posixpath.join(base, member.linkname))
    return normalized != ".." and not normalized.startswith("../")


def _normalize_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _write_archive_stream(root: Path, raw: BinaryIO) -> None:
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for relative, path in _payload_entries(root):
            archive.add(
                path,
                arcname=relative,
                recursive=False,
                filter=_normalize_tar_info,
            )


def _atomic_write_text(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
