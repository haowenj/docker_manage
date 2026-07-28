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
    if args[:3] == ["image", "inspect", "--format"]:
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
