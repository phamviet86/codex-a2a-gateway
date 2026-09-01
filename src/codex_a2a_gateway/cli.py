from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path

from . import __version__
from .core import BridgeService
from .gateway import run_gateway
from .models import TERMINAL_STATES, BridgeError
from .server import run_stdio
from .settings import Settings


async def _status() -> int:
    service = BridgeService(Settings.from_env())
    try:
        result = await service.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    finally:
        await service.aclose()


async def _smoke(message: str, conversation_key: str) -> int:
    settings = Settings.from_env()
    service = BridgeService(settings)
    try:
        result = await service.chat(message, conversation_key=conversation_key, mode="sync")
        if result.get("state") not in TERMINAL_STATES:
            result = await service.task_wait(
                str(result["bridge_task_id"]),
                timeout=min(settings.correlation_timeout, 300),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except BridgeError as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    finally:
        await service.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bidirectional A2A gateway for Codex")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server over stdio")
    sub.add_parser("gateway", help="Run the standalone inbound A2A HTTP gateway")
    sub.add_parser("doctor", help="Check gateway state, Hermes health, and Agent Card")
    smoke = sub.add_parser("smoke", help="Send one explicit harmless live test message")
    smoke.add_argument("message")
    smoke.add_argument("--conversation-key", default="bridge-cli-smoke")
    plugin = sub.add_parser(
        "install-hermes-plugin",
        help="Install the bundled durable Hermes -> Codex A2A client plugin",
    )
    plugin.add_argument(
        "--replace", action="store_true", help="Replace a different existing plugin at this exact target"
    )
    return parser


def _same_tree(left: Path, right: Path) -> bool:
    if not right.is_dir():
        return False
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    return left_files == right_files and all(
        (left / path).read_bytes() == (right / path).read_bytes() for path in left_files
    )


def _install_hermes_plugin(*, replace: bool) -> int:
    configured_home = os.environ.get("HERMES_HOME")
    hermes_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".hermes"
    destination = hermes_home / "plugins" / "codex-a2a-gateway"
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with resources.as_file(resources.files("codex_a2a_gateway.hermes_plugin") / "asset") as source:
        if destination.exists() and _same_tree(source, destination):
            print(f"Hermes plugin already installed: {destination}")
            return 0
        if destination.exists() and not replace:
            print(
                f"Refusing to replace existing plugin at {destination}; rerun with --replace after review.",
                file=sys.stderr,
            )
            return 2
        with tempfile.TemporaryDirectory(prefix="codex-a2a-plugin-", dir=destination_parent) as temporary:
            staged = Path(temporary) / "codex-a2a-gateway"
            shutil.copytree(source, staged)
            backup = destination_parent / ".codex-a2a-gateway.previous"
            if backup.exists():
                shutil.rmtree(backup)
            if destination.exists():
                destination.replace(backup)
            try:
                staged.replace(destination)
            except Exception:
                if backup.exists() and not destination.exists():
                    backup.replace(destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
    print(f"Installed Hermes plugin: {destination}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    command = args.command or "serve"
    if command == "serve":
        run_stdio()
        return
    if command == "gateway":
        run_gateway()
        return
    if command == "doctor":
        raise SystemExit(asyncio.run(_status()))
    if command == "smoke":
        raise SystemExit(asyncio.run(_smoke(args.message, args.conversation_key)))
    if command == "install-hermes-plugin":
        raise SystemExit(_install_hermes_plugin(replace=args.replace))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
