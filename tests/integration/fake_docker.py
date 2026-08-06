#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path


def main() -> int:
    log_path = os.environ.get("FAKE_DOCKER_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\n")

    args = sys.argv[1:]
    if args[:2] == ["version", "--format"]:
        print(os.environ.get("FAKE_DOCKER_VERSION", "29.0.0|29.0.0"))
    elif args[:2] == ["compose", "version"]:
        print(os.environ.get("FAKE_DOCKER_COMPOSE_VERSION", "Docker Compose version v2.0.0"))
    elif args[:2] == ["buildx", "version"]:
        print(os.environ.get("FAKE_DOCKER_BUILDX_VERSION", "github.com/docker/buildx v0.20.0"))
    elif args and args[0] == "compose" and "config" in args:
        variable = (
            "FAKE_DOCKER_COMPOSE_CONFIG"
            if "--no-interpolate" in args
            else "FAKE_DOCKER_RESOLVED_COMPOSE_CONFIG"
        )
        fallback = os.environ.get("FAKE_DOCKER_COMPOSE_CONFIG", '{"services": {}}')
        print(os.environ.get(variable, fallback))
    elif args[:3] == ["image", "inspect", "--format"]:
        raw = os.environ.get("FAKE_DOCKER_INSPECT", "[]")
        records = json.loads(raw)
        if isinstance(records, dict):
            records = [records]
        for record in records:
            print(json.dumps(record))
    elif args[:2] == ["image", "save"] and "--output" in args:
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w"):
            pass

    stderr = os.environ.get("FAKE_DOCKER_STDERR")
    if stderr:
        print(stderr, file=sys.stderr)
    return int(os.environ.get("FAKE_DOCKER_EXIT", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
