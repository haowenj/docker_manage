import stat
from pathlib import Path

import pytest

from docker_package_app.models import Inspection, Stage
from docker_package_app.workspace import (
    WorkPaths,
    atomic_write_model,
    cleanup_run,
    load_model,
)


def test_workspace_is_private_and_keeps_generated_files(tmp_path: Path) -> None:
    paths = WorkPaths.create(tmp_path, "run-1")

    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    assert paths.ignore_file.read_text(encoding="utf-8") == "*\n!.gitignore\n"
    (paths.generated / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    atomic_write_model(
        paths.state,
        Inspection(
            run_id="run-1",
            project_root=str(tmp_path),
            stage=Stage.INSPECTED,
        ),
    )

    assert load_model(paths.state, Inspection).stage is Stage.INSPECTED
    assert stat.S_IMODE(paths.state.stat().st_mode) == 0o600

    cleanup_run(paths)

    assert not paths.run.exists()
    assert (paths.generated / "Dockerfile").exists()


def test_workspace_rejects_unsafe_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run ID"):
        WorkPaths.create(tmp_path, "../outside")


def test_workspace_does_not_replace_existing_ignore_file(tmp_path: Path) -> None:
    root = tmp_path / ".docker-manage"
    root.mkdir()
    ignore = root / ".gitignore"
    ignore.write_text("custom\n", encoding="utf-8")

    paths = WorkPaths.create(tmp_path, "run-1")

    assert paths.ignore_file.read_text(encoding="utf-8") == "custom\n"
