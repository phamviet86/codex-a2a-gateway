from __future__ import annotations

import argparse
import asyncio
import json
import sys

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
    return parser


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
    raise SystemExit(2)


if __name__ == "__main__":
    main()
