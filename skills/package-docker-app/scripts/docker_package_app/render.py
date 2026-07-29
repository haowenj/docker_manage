from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import set_key

from docker_package_app.command import CommandRunner
from docker_package_app.compose import ComposeDocument
from docker_package_app.errors import PackageError
from docker_package_app.files import FileRewriteKey
from docker_package_app.models import PackagePlan


def render_deployment(
    base: ComposeDocument,
    plan: PackagePlan,
    file_rewrites: Mapping[FileRewriteKey, str],
) -> dict[str, Any]:
    data = copy.deepcopy(base.data)
    data["name"] = plan.compose_project_name
    image_plans = {item.service: item for item in plan.images}
    environment_by_service: dict[str, dict[str, str]] = {}
    for item in plan.environment:
        environment_by_service.setdefault(item.service, {})[item.container_name] = (
            f"${{{item.artifact_name}}}"
        )
    ports_by_service: dict[str, list[dict[str, Any]]] = {}
    for item in plan.ports:
        if not item.exposed:
            ports_by_service.setdefault(item.service, [])
            continue
        port: dict[str, Any] = {
            "target": item.container_port,
            "published": item.host_port,
            "protocol": item.protocol,
        }
        if item.host_ip is not None:
            port["host_ip"] = item.host_ip
        ports_by_service.setdefault(item.service, []).append(port)

    for service, config in data["services"].items():
        image = image_plans.get(service)
        if image is None:
            raise PackageError(f"部署计划没有为服务 {service} 指定镜像")
        if image.platform != plan.platform:
            raise PackageError(
                f"服务 {service} 的镜像平台 {image.platform} 与 {plan.platform} 不匹配"
            )
        config.pop("build", None)
        config["image"] = image.final_image
        config["platform"] = plan.platform

        had_environment = "environment" in config or "env_file" in config
        config.pop("env_file", None)
        service_environment = environment_by_service.get(service, {})
        if service_environment or had_environment:
            config["environment"] = {
                name: service_environment[name]
                for name in sorted(service_environment)
            }

        if service in ports_by_service or "ports" in config:
            ports = ports_by_service.get(service, [])
            if ports:
                config["ports"] = ports
            else:
                config.pop("ports", None)

        _rewrite_service_volumes(service, config, file_rewrites)

    for kind in ("config", "secret"):
        definitions = data.get(f"{kind}s")
        if not isinstance(definitions, dict):
            continue
        for definition in definitions.values():
            if not isinstance(definition, dict):
                continue
            source = definition.get("file")
            if not isinstance(source, str):
                continue
            destinations = {
                destination
                for (_service, rewrite_kind, original), destination
                in file_rewrites.items()
                if rewrite_kind == kind and original == source
            }
            if len(destinations) > 1:
                raise PackageError(f"{kind} 文件 {source} 存在不一致的载荷改写")
            if destinations:
                definition["file"] = destinations.pop()

    return data


def write_deployment(
    compose_data: Mapping[str, Any],
    env_values: Mapping[str, str],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    compose_path = output_dir / "compose.yaml"
    env_path = output_dir / ".env"

    compose_path.write_text(
        yaml.safe_dump(
            dict(compose_data),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    compose_path.chmod(0o600)

    env_path.write_text("", encoding="utf-8")
    env_path.chmod(0o600)
    for name in sorted(env_values):
        set_key(
            env_path,
            name,
            env_values[name],
            quote_mode="always",
        )
    env_path.chmod(0o600)
    return compose_path, env_path


def validate_deployment(
    compose_path: Path,
    env_path: Path,
    runner: CommandRunner,
) -> None:
    result = runner.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(compose_path),
            "config",
        ]
    )
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "variable is not set" in output:
        raise PackageError(
            "部署 Compose 校验发现未设置的变量",
            hint="请把缺失变量加入生成的部署环境变量文件。",
            details=(result.stderr or result.stdout).strip(),
        )


def _rewrite_service_volumes(
    service: str,
    config: dict[str, Any],
    rewrites: Mapping[FileRewriteKey, str],
) -> None:
    volumes = config.get("volumes")
    if not isinstance(volumes, list):
        return
    for index, volume in enumerate(volumes):
        if isinstance(volume, dict):
            source = volume.get("source")
            if volume.get("type") == "bind" and isinstance(source, str):
                rewrite = rewrites.get((service, "bind", source))
                if rewrite is not None:
                    volume["source"] = rewrite
            continue
        if not isinstance(volume, str):
            continue
        source, separator, remainder = volume.partition(":")
        rewrite = rewrites.get((service, "bind", source))
        if separator and rewrite is not None:
            volumes[index] = f"{rewrite}:{remainder}"
