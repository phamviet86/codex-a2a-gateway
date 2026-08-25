from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def _default_state_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "codex-hermes-a2a-bridge" / "state.sqlite3"


def _default_conversation_dir() -> Path:
    return Path.home() / ".hermes" / "a2a_conversations"


def is_loopback_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


class Settings(BaseModel):
    endpoint: str = "http://127.0.0.1:9900"
    token: str = ""
    state_path: Path = Field(default_factory=_default_state_path)
    default_timeout: float = Field(default=60.0, ge=1.0, le=300.0)
    auto_wait: float = Field(default=15.0, ge=0.1, le=120.0)
    max_message_chars: int = Field(default=32768, ge=256, le=1_000_000)
    max_turns: int = Field(default=5, ge=1, le=20)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    connect_timeout: float = Field(default=3.0, ge=0.1, le=30.0)
    correlation_timeout: float = Field(default=300.0, ge=30.0, le=600.0)
    sync_wait: float = Field(default=30.0, ge=1.0, le=120.0)
    conversation_dir: Path = Field(default_factory=_default_conversation_dir)

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_loopback(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not is_loopback_url(value):
            raise ValueError("v0.1 only allows a loopback Hermes A2A endpoint")
        return value

    @classmethod
    def from_env(cls) -> Settings:
        values: dict[str, object] = {}
        env_map = {
            "endpoint": "HERMES_A2A_ENDPOINT",
            "token": "HERMES_A2A_TOKEN",
            "state_path": "HERMES_BRIDGE_STATE_PATH",
            "default_timeout": "HERMES_BRIDGE_DEFAULT_TIMEOUT",
            "auto_wait": "HERMES_BRIDGE_AUTO_WAIT",
            "max_message_chars": "HERMES_BRIDGE_MAX_MESSAGE_CHARS",
            "max_turns": "HERMES_BRIDGE_MAX_TURNS",
            "max_concurrency": "HERMES_BRIDGE_MAX_CONCURRENCY",
            "connect_timeout": "HERMES_BRIDGE_CONNECT_TIMEOUT",
            "correlation_timeout": "HERMES_BRIDGE_CORRELATION_TIMEOUT",
            "sync_wait": "HERMES_BRIDGE_SYNC_WAIT",
            "conversation_dir": "HERMES_A2A_CONVERSATION_DIR",
        }
        for field, env_name in env_map.items():
            raw = os.environ.get(env_name)
            if raw not in (None, ""):
                values[field] = raw
        return cls.model_validate(values)

    @property
    def card_url(self) -> str:
        return self.endpoint + "/.well-known/agent-card.json"

    @property
    def legacy_card_url(self) -> str:
        return self.endpoint + "/.well-known/agent.json"

    @property
    def health_url(self) -> str:
        return self.endpoint + "/health"
