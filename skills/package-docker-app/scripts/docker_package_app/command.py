from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from docker_package_app.errors import PackageError


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        command = [str(part) for part in argv]
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=merged_env,
                text=True,
                capture_output=True,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise PackageError(
                f"unable to execute {shlex.join(command)}",
                hint="Install the required command and ensure it is available on PATH.",
                details=str(exc),
            ) from exc

        result = CommandResult(
            argv=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            raise PackageError(
                f"command failed ({result.returncode}): {shlex.join(command)}",
                hint="Review the command error and retry after correcting the local environment.",
                details=result.stderr.strip() or result.stdout.strip(),
            )
        return result

