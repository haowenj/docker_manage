from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class WorkPaths:
    project_root: Path
    root: Path
    generated: Path
    work: Path
    run: Path
    dist: Path
    state: Path
    ignore_file: Path

    @classmethod
    def create(cls, project_root: Path, run_id: str) -> WorkPaths:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"运行 ID 无效：{run_id!r}")

        project = project_root.resolve()
        root = project / ".docker-manage"
        generated = root / "generated"
        work = root / "work"
        run = work / run_id
        dist = root / "dist"

        for directory in (root, generated, work, run, dist):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

        ignore_file = root / ".gitignore"
        if not ignore_file.exists():
            ignore_file.write_text("*\n!.gitignore\n", encoding="utf-8")
            ignore_file.chmod(0o600)

        return cls(
            project_root=project,
            root=root,
            generated=generated,
            work=work,
            run=run,
            dist=dist,
            state=run / "state.json",
            ignore_file=ignore_file,
        )


def atomic_write_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(value.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def load_model(path: Path, model_type: type[T]) -> T:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def cleanup_run(paths: WorkPaths) -> None:
    run = paths.run.resolve(strict=False)
    work = paths.work.resolve(strict=False)
    root = paths.root.resolve(strict=False)
    if run.parent != work or work.parent != root or run == work:
        raise ValueError(f"拒绝清理不安全的运行路径：{run}")
    if run.exists():
        shutil.rmtree(run)
