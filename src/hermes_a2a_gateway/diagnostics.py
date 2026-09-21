"""Bounded, allowlisted peer diagnostics; free-form peer text is never a policy input."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .models import A2ATaskResult

DIAGNOSTICS_URI = "https://github.com/phamviet86/hermes-a2a-gateway/extensions/diagnostics/v1"
REASONS = {"context_rate_limited", "context_turn_limit", "input_required", "peer_rejected"}
ACTIONS = {"wait_then_submit_new", "inspect_operation", "contact_operator", "provide_input"}


def peer_error_details(task: A2ATaskResult) -> dict[str, Any]:
    details: dict[str, Any] = {
        "reason": "input_required" if task.state == "input_required" else "peer_rejected",
        "peer_state": task.state,
        "recommended_action": "provide_input" if task.state == "input_required" else "contact_operator",
    }
    metadata = task.raw.get("metadata")
    value = metadata.get(DIAGNOSTICS_URI) if isinstance(metadata, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("reason"), str) or value["reason"] not in REASONS:
        legacy = (
            rf"Anti-loop protection: context {re.escape(task.context_id)} exceeded [1-9][0-9]* turns\. "
            r"Start a new context or increase A2A_MAX_PINGPONG_TURNS\."
        )
        if task.state == "rejected" and len(task.text) <= 1024 and re.fullmatch(legacy, task.text):
            details["reason"] = "context_turn_limit"
        return details
    details["reason"] = value["reason"]
    if type(value.get("execution_started")) is bool:
        details["execution_started"] = value["execution_started"]
    delay = value.get("retry_after_seconds")
    if type(delay) is int and 0 <= delay <= 86400:
        details["retry_after_seconds"] = delay
    instant = value.get("retry_at")
    if isinstance(instant, str) and len(instant) <= 40:
        try:
            parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                details["retry_at"] = parsed.isoformat()
        except ValueError:
            pass
    action = value.get("recommended_action")
    if isinstance(action, str) and action in ACTIONS:
        details["recommended_action"] = action
    return details


def peer_capabilities(value: Any) -> dict[str, Any]:
    """Accept only a complete known capability contract; never echo peer strings."""
    unknown = {"status": "unknown", "policy": "unknown"}
    if not isinstance(value, dict) or value.get("diagnostics_extension") != DIAGNOSTICS_URI:
        return unknown
    policy = value.get("policy")
    if policy not in ("sliding_window", "legacy_turn_limit"):
        return unknown
    limit = value.get("context_limit")
    if type(limit) is not int or not 1 <= limit <= 100000:
        return unknown
    output: dict[str, Any] = {
        "status": "supported",
        "diagnostics_extension": DIAGNOSTICS_URI,
        "policy": policy,
        "context_limit": limit,
    }
    if policy == "sliding_window":
        window = value.get("window_seconds")
        if type(window) is not int or not 1 <= window <= 86400:
            return unknown
        output["window_seconds"] = window
    if value.get("patch_version") == "0.8.0":
        output["patch_version"] = "0.8.0"
    return output
