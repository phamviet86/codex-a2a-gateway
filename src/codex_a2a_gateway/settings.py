from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


def _default_state_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    current = root / "codex-a2a-gateway" / "state.sqlite3"
    legacy = root / "codex-hermes-a2a-bridge" / "state.sqlite3"
    return legacy if legacy.is_file() and not current.exists() else current


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
    max_pending_tasks: int = Field(default=16, ge=1, le=256)
    connect_timeout: float = Field(default=3.0, ge=0.1, le=30.0)
    correlation_timeout: float = Field(default=300.0, ge=30.0, le=600.0)
    sync_wait: float = Field(default=30.0, ge=1.0, le=120.0)
    conversation_dir: Path = Field(default_factory=_default_conversation_dir)
    inbound_host: str = "127.0.0.1"
    inbound_port: int = Field(default=9910, ge=1, le=65535)
    inbound_public_url: str = ""
    inbound_token: str = ""
    agent_name: str = "Codex A2A Gateway"
    backend: str = "app-server"
    cli_fallback: bool = False
    codex_bin: str = "codex"
    codex_workspace: Path = Field(default_factory=Path.cwd)
    codex_timeout: float = Field(default=300.0, ge=1.0, le=3600.0)
    approval_policy: str = "never"
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    # The gateway is the receiver and may narrow the App Server model catalog.
    # An empty allow-list means "no additional restriction", not "no model".
    codex_allowed_models: tuple[str, ...] = ()
    codex_default_model: str = ""
    codex_allowed_reasoning_efforts: tuple[str, ...] = (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    )
    codex_default_reasoning_effort: str = ""

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_loopback(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not is_loopback_url(value):
            raise ValueError("v0.1 only allows a loopback Hermes A2A endpoint")
        return value

    @field_validator("backend")
    @classmethod
    def backend_must_be_supported(cls, value: str) -> str:
        if value not in {"app-server", "cli"}:
            raise ValueError("CODEX_A2A_GATEWAY_BACKEND must be app-server or cli")
        return value

    @field_validator("approval_policy")
    @classmethod
    def approval_policy_must_be_supported(cls, value: str) -> str:
        if value not in {"never", "untrusted", "on-request"}:
            raise ValueError("CODEX_A2A_GATEWAY_APPROVAL_POLICY must be never, untrusted, or on-request")
        return value

    @field_validator("inbound_host")
    @classmethod
    def non_loopback_requires_token(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("inbound host must not be empty")
        return value

    @field_validator("inbound_public_url")
    @classmethod
    def public_url_must_be_http_root(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("CODEX_A2A_PUBLIC_URL must be an HTTP(S) origin without credentials or a path")
        return value

    @model_validator(mode="after")
    def validate_inbound_exposure(self) -> Self:
        if not self.inbound_is_loopback and not self.inbound_token:
            raise ValueError("a non-loopback CODEX_A2A_HOST requires CODEX_A2A_BEARER_TOKEN")
        if self.inbound_public_url and not is_loopback_url(self.inbound_public_url) and not self.inbound_token:
            raise ValueError("a non-loopback CODEX_A2A_PUBLIC_URL requires CODEX_A2A_BEARER_TOKEN")
        return self

    @classmethod
    def from_env(cls) -> Settings:
        values: dict[str, object] = {}
        env_map = {
            "endpoint": ("HERMES_A2A_ENDPOINT",),
            "token": ("HERMES_A2A_TOKEN",),
            "state_path": ("CODEX_A2A_GATEWAY_STATE_PATH", "HERMES_BRIDGE_STATE_PATH"),
            "default_timeout": ("CODEX_A2A_GATEWAY_DEFAULT_TIMEOUT", "HERMES_BRIDGE_DEFAULT_TIMEOUT"),
            "auto_wait": ("CODEX_A2A_GATEWAY_AUTO_WAIT", "HERMES_BRIDGE_AUTO_WAIT"),
            "max_message_chars": ("CODEX_A2A_GATEWAY_MAX_MESSAGE_CHARS", "HERMES_BRIDGE_MAX_MESSAGE_CHARS"),
            "max_turns": ("CODEX_A2A_GATEWAY_MAX_TURNS", "HERMES_BRIDGE_MAX_TURNS"),
            "max_concurrency": ("CODEX_A2A_GATEWAY_MAX_CONCURRENCY", "HERMES_BRIDGE_MAX_CONCURRENCY"),
            "max_pending_tasks": ("CODEX_A2A_MAX_PENDING_TASKS",),
            "connect_timeout": ("CODEX_A2A_GATEWAY_CONNECT_TIMEOUT", "HERMES_BRIDGE_CONNECT_TIMEOUT"),
            "correlation_timeout": (
                "CODEX_A2A_GATEWAY_CORRELATION_TIMEOUT",
                "HERMES_BRIDGE_CORRELATION_TIMEOUT",
            ),
            "sync_wait": ("CODEX_A2A_GATEWAY_SYNC_WAIT", "HERMES_BRIDGE_SYNC_WAIT"),
            "conversation_dir": ("HERMES_A2A_CONVERSATION_DIR",),
            "inbound_host": ("CODEX_A2A_HOST",),
            "inbound_port": ("CODEX_A2A_PORT",),
            "inbound_public_url": ("CODEX_A2A_PUBLIC_URL",),
            "inbound_token": ("CODEX_A2A_BEARER_TOKEN",),
            "agent_name": ("CODEX_A2A_AGENT_NAME",),
            "backend": ("CODEX_A2A_GATEWAY_BACKEND", "CODEX_BRIDGE_BACKEND"),
            "cli_fallback": ("CODEX_A2A_GATEWAY_CLI_FALLBACK", "CODEX_BRIDGE_CLI_FALLBACK"),
            "codex_bin": ("CODEX_CLI_BIN",),
            "codex_workspace": ("CODEX_WORKSPACE_ROOT",),
            "codex_timeout": ("CODEX_A2A_GATEWAY_CODEX_TIMEOUT", "CODEX_BRIDGE_CODEX_TIMEOUT"),
            "approval_policy": ("CODEX_A2A_GATEWAY_APPROVAL_POLICY", "CODEX_BRIDGE_APPROVAL_POLICY"),
            "max_request_bytes": ("CODEX_A2A_MAX_REQUEST_BYTES",),
            "codex_default_model": ("CODEX_A2A_GATEWAY_DEFAULT_MODEL",),
            "codex_default_reasoning_effort": ("CODEX_A2A_GATEWAY_DEFAULT_REASONING_EFFORT",),
        }
        for field, env_names in env_map.items():
            for env_name in env_names:
                raw = os.environ.get(env_name)
                if raw not in (None, ""):
                    values[field] = raw
                    break
        for field, env_name in {
            "codex_allowed_models": "CODEX_A2A_GATEWAY_ALLOWED_MODELS",
            "codex_allowed_reasoning_efforts": "CODEX_A2A_GATEWAY_ALLOWED_REASONING_EFFORTS",
        }.items():
            raw = os.environ.get(env_name)
            if raw is not None:
                values[field] = tuple(item.strip() for item in raw.split(",") if item.strip())
        return cls.model_validate(values)

    @property
    def inbound_is_loopback(self) -> bool:
        if self.inbound_host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.inbound_host).is_loopback
        except ValueError:
            return False

    @property
    def advertised_url(self) -> str:
        return (self.inbound_public_url or f"http://{self.inbound_host}:{self.inbound_port}").rstrip("/")

    @property
    def inbound_admission_limit(self) -> int:
        return max(self.max_pending_tasks, self.max_concurrency)

    @property
    def card_url(self) -> str:
        return self.endpoint + "/.well-known/agent-card.json"

    @property
    def legacy_card_url(self) -> str:
        return self.endpoint + "/.well-known/agent.json"

    @property
    def health_url(self) -> str:
        return self.endpoint + "/health"
