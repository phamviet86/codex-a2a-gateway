#!/usr/bin/env python3
"""Read-only live lifecycle check for an existing bridge conversation."""

from __future__ import annotations

import argparse
import asyncio
import json

from codex_hermes_a2a_bridge.core import BridgeService
from codex_hermes_a2a_bridge.settings import Settings


async def check(conversation_key: str) -> int:
    service = BridgeService(Settings.from_env())
    try:
        status = await service.status()
        listed = await service.tasks_list(conversation_key=conversation_key)
        if not status["ok"] or not listed["tasks"]:
            print(json.dumps({"ok": False, "status": status, "task_count": listed["count"]}))
            return 1
        task_id = listed["tasks"][0]["bridge_task_id"]
        got = await service.task_get(task_id)
        waited = await service.task_wait(task_id, timeout=2)
        context = await service.contexts(action="inspect", conversation_key=conversation_key)
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": status["hermes"]["health"]["status"],
                    "list_count": listed["count"],
                    "get_state": got["state"],
                    "wait_state": waited["state"],
                    "context_id": context["context"]["context_id"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        await service.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("conversation_key")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.conversation_key)))


if __name__ == "__main__":
    main()
