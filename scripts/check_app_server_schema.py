#!/usr/bin/env python3
"""Generate and minimally validate the App Server schema from the installed Codex binary."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"schema is not an object: {path.name}")
    return value


def require_fields(root: Path, relative: str, expected: set[str]) -> None:
    schema = load(root / relative)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError(f"schema has no properties: {relative}")
    missing = expected.difference(properties)
    if missing:
        raise RuntimeError(f"{relative} is missing fields: {', '.join(sorted(missing))}")


def require_method(root: Path, method: str) -> None:
    needle = f'"{method}"'
    for path in root.rglob("*.json"):
        if needle in path.read_text(encoding="utf-8"):
            return
    raise RuntimeError(f"generated schemas do not contain request method: {method}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default="codex")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="codex-app-server-schema-") as directory:
        root = Path(directory)
        subprocess.run(
            [args.codex_bin, "app-server", "generate-json-schema", "--out", str(root)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        require_fields(root, "v1/InitializeParams.json", {"clientInfo"})
        require_fields(root, "v2/ThreadStartParams.json", {"cwd", "approvalPolicy"})
        require_fields(root, "v2/ThreadResumeParams.json", {"threadId"})
        require_fields(root, "v2/ThreadReadParams.json", {"threadId", "includeTurns"})
        require_fields(root, "v2/TurnStartParams.json", {"threadId", "input", "clientUserMessageId"})
        require_fields(root, "v2/TurnStartParams.json", {"model", "effort"})
        require_fields(root, "v2/ModelListResponse.json", {"data", "nextCursor"})
        require_fields(root, "v2/TurnInterruptParams.json", {"threadId", "turnId"})
        require_method(root, "item/tool/requestUserInput")
        require_method(root, "item/commandExecution/requestApproval")
        require_method(root, "item/fileChange/requestApproval")
    print("Codex App Server schema is compatible with the gateway adapter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
