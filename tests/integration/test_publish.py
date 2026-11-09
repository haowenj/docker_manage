from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, Self

from conftest import CliRunner


class PublishHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str, bytes]]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(("POST", self.path, body))
        if self.path == "/api/deployment-tasks":
            self._json(
                201,
                {"task_id": "task-1", "status": "pending_review"},
            )
            return
        if self.path == "/api/deployment-tasks/task-1/deploy":
            self._json(202, {"task_id": "task-1", "status": "deploying"})
            return
        self._json(404, {"detail": "not found"})

    def do_GET(self) -> None:
        type(self).requests.append(("GET", self.path, b""))
        if self.path == "/api/deployment-tasks/task-1":
            self._json(
                200,
                {
                    "task_id": "task-1",
                    "status": "deployed",
                    "error": None,
                    "command_output": "started\n",
                },
            )
            return
        self._json(404, {"detail": "not found"})

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PublishServer:
    def __init__(self) -> None:
        PublishHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PublishHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def test_publish_uploads_archive_without_deploying(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    archive = tmp_path / "demo.tar.gz"
    archive.write_bytes(b"archive-content")

    with PublishServer() as server:
        result = cli(
            "publish",
            str(archive),
            "--server-url",
            server.url,
            "--json",
        )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["stage"] == "uploaded"
    assert body["task_id"] == "task-1"
    assert body["status"] == "pending_review"
    assert body["task_url"] == f"{server.url}/deployments/task-1"
    assert [request[:2] for request in PublishHandler.requests] == [
        ("POST", "/api/deployment-tasks")
    ]
    assert b"archive-content" in PublishHandler.requests[0][2]


def test_publish_can_deploy_and_wait_for_completion(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    archive = tmp_path / "demo.tar.gz"
    archive.write_bytes(b"archive-content")

    with PublishServer() as server:
        result = cli(
            "publish",
            str(archive),
            "--server-url",
            server.url,
            "--deploy",
            "--poll-interval",
            "0.01",
            "--json",
        )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["stage"] == "deployed"
    assert body["status"] == "deployed"
    assert [request[:2] for request in PublishHandler.requests] == [
        ("POST", "/api/deployment-tasks"),
        ("POST", "/api/deployment-tasks/task-1/deploy"),
        ("GET", "/api/deployment-tasks/task-1"),
    ]
