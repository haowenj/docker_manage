from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from docker_package_app.command import CommandRunner
from docker_package_app.errors import UsageError


class ComposeDocument:
    def __init__(
        self,
        project_root: Path,
        data: dict[str, Any],
        files: Sequence[Path] = (),
    ) -> None:
        services = data.get("services")
        if not isinstance(services, dict):
            raise UsageError("Compose 文档必须包含 services 映射")
        if not all(isinstance(name, str) and isinstance(value, dict) for name, value in services.items()):
            raise UsageError("每个 Compose 服务都必须使用字符串名称和映射配置")
        self.project_root = project_root.resolve()
        self.files = tuple(Path(path).resolve() for path in files)
        self.data = copy.deepcopy(data)

    @classmethod
    def load(
        cls,
        project_root: Path,
        files: Sequence[Path],
        profiles: Sequence[str],
        runner: CommandRunner,
    ) -> ComposeDocument:
        root = project_root.resolve()
        argv = ["docker", "compose", "--project-directory", str(root)]
        resolved_files = [Path(path).resolve() for path in files]
        for path in resolved_files:
            argv.extend(["-f", str(path)])
        for profile in profiles:
            argv.extend(["--profile", profile])
        argv.extend(
            [
                "config",
                "--format",
                "json",
                "--no-interpolate",
                "--no-path-resolution",
            ]
        )
        result = runner.run(argv, cwd=root)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UsageError(
                "Docker Compose 返回了无效的 JSON",
                details=result.stdout,
            ) from exc
        if not isinstance(data, dict):
            raise UsageError("Docker Compose 配置必须是一个对象")
        return cls(root, data, resolved_files)

    @classmethod
    def from_data(
        cls,
        project_root: Path,
        data: Mapping[str, Any],
    ) -> ComposeDocument:
        return cls(project_root, dict(data))

    def services(self) -> tuple[str, ...]:
        return tuple(sorted(self.data["services"]))

    def service(self, name: str) -> dict[str, Any]:
        return self.data["services"][name]

    def build_services(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in self.services()
            if "build" in self.service(name)
        )

    def image_services(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in self.services()
            if "build" not in self.service(name)
            and isinstance(self.service(name).get("image"), str)
            and bool(self.service(name)["image"].strip())
        )

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = yaml.safe_dump(
            self.data,
            sort_keys=False,
            allow_unicode=True,
        )
        parsed = yaml.safe_load(rendered)
        if parsed != self.data:
            raise UsageError("渲染后的 Compose YAML 改变了文档结构")
        path.write_text(rendered, encoding="utf-8")
