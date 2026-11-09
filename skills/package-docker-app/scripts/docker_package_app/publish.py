from __future__ import annotations

import hashlib
import http.client
import json
import re
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit
from uuid import uuid4

from docker_package_app.errors import PublishError

_CHUNK_SIZE = 1024 * 1024
_FAILED_STATUSES = {"failed", "discarded"}


def publish_archive(
    archive: Path,
    server_url: str,
    *,
    deploy: bool = False,
    timeout: float = 1800.0,
    wait_timeout: float = 1800.0,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    archive = Path(archive)
    if not archive.is_file():
        raise PublishError(f"归档不存在：{archive}")
    if timeout <= 0 or wait_timeout <= 0 or poll_interval <= 0:
        raise PublishError("超时和轮询间隔必须大于 0")

    parsed = _parse_server_url(server_url)
    task, digest = _upload_archive(archive, parsed, timeout)
    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise PublishError("Docker Manage 响应缺少 task_id")

    if deploy:
        _request_json(
            "POST",
            parsed,
            f"/api/deployment-tasks/{quote(task_id, safe='')}/deploy",
            timeout,
        )
        task = _wait_for_deployment(
            parsed,
            task_id,
            timeout=timeout,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
        )

    status = task.get("status")
    if not isinstance(status, str) or not status:
        raise PublishError("Docker Manage 响应缺少任务状态")

    return {
        "stage": "deployed" if deploy else "uploaded",
        "archive": str(archive),
        "sha256": digest,
        "server_url": _base_url(parsed),
        "task_id": task_id,
        "status": status,
        "task_url": f"{_base_url(parsed)}/deployments/{quote(task_id, safe='')}",
    }


def _upload_archive(
    archive: Path,
    parsed: SplitResult,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    boundary = f"----docker-manage-{uuid4().hex}"
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", archive.name) or "archive.tar.gz"
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/gzip\r\n"
        "\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    size = archive.stat().st_size
    content_length = len(prefix) + size + len(suffix)
    connection = _connection(parsed, timeout)
    digest = hashlib.sha256()
    try:
        connection.putrequest("POST", _endpoint_path(parsed, "/api/deployment-tasks"))
        connection.putheader(
            "Content-Type",
            f"multipart/form-data; boundary={boundary}",
        )
        connection.putheader("Content-Length", str(content_length))
        connection.putheader("Accept", "application/json")
        connection.putheader("Connection", "close")
        connection.endheaders()
        connection.send(prefix)
        with archive.open("rb") as source:
            remaining = size
            while remaining:
                chunk = source.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    raise PublishError("归档在上传过程中被截断")
                connection.send(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        raise PublishError(f"上传归档失败：{exc}") from exc
    finally:
        connection.close()

    task = _decode_response(status, raw)
    return task, digest.hexdigest()


def _wait_for_deployment(
    parsed: SplitResult,
    task_id: str,
    *,
    timeout: float,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_timeout
    task_path = f"/api/deployment-tasks/{quote(task_id, safe='')}"
    while True:
        task = _request_json("GET", parsed, task_path, timeout)
        status = task.get("status")
        if status == "deployed":
            return task
        if status in _FAILED_STATUSES:
            raise PublishError(
                f"部署未成功：{task.get('error') or status}",
                details=_task_details(task),
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublishError(f"等待部署完成超时，任务仍为 {status or '未知状态'}")
        time.sleep(min(poll_interval, remaining))


def _request_json(
    method: str,
    parsed: SplitResult,
    path: str,
    timeout: float,
) -> dict[str, Any]:
    connection = _connection(parsed, timeout)
    try:
        connection.request(
            method,
            path,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        raise PublishError(f"Docker Manage 请求失败：{exc}") from exc
    finally:
        connection.close()
    return _decode_response(status, raw)


def _decode_response(status: int, raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError(
            f"Docker Manage 返回了无法解析的响应（HTTP {status}）"
        ) from exc

    if 200 <= status < 300:
        if not isinstance(payload, dict):
            raise PublishError("Docker Manage 响应不是 JSON 对象")
        return payload

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, str) or not detail:
        detail = json.dumps(payload, ensure_ascii=False)[:1000]
    raise PublishError(f"Docker Manage 请求失败（HTTP {status}）：{detail}")


def _parse_server_url(value: str) -> SplitResult:
    if not value or not value.strip():
        raise PublishError(
            "缺少 Docker Manage 服务地址，请传入 --server-url 或设置 "
            "DOCKER_MANAGE_SERVER_URL"
        )
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PublishError("Docker Manage 服务地址必须是 http:// 或 https:// URL")
    if parsed.query or parsed.fragment:
        raise PublishError("Docker Manage 服务地址不能包含查询参数或片段")
    if parsed.username or parsed.password:
        raise PublishError("Docker Manage 服务地址不能包含用户名或密码")
    return parsed


def _base_url(parsed: SplitResult) -> str:
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _endpoint_path(parsed: SplitResult, endpoint: str) -> str:
    return f"{parsed.path.rstrip('/')}{endpoint}"


def _connection(parsed: SplitResult, timeout: float) -> http.client.HTTPConnection:
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)


def _task_details(task: dict[str, Any]) -> str | None:
    output = task.get("command_output")
    if not isinstance(output, str) or not output.strip():
        return None
    return f"命令输出：\n{output[-4000:]}"
