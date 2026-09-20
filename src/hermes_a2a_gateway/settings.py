"""Loopback Hermes peer settings; client and broker have separate configuration."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


def is_loopback_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        _ = parsed.port
        return parsed.hostname.lower() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        return False


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: str = "http://127.0.0.1:9900"
    token: str = Field(default="", repr=False)
    default_timeout: float = Field(default=60.0, ge=1.0, le=300.0)
    connect_timeout: float = Field(default=3.0, ge=0.1, le=30.0)

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_loopback(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not is_loopback_url(value):
            raise ValueError("Hermes A2A endpoint must be loopback without credentials, query or fragment")
        return value

    @property
    def card_url(self) -> str:
        return self.endpoint + "/.well-known/agent-card.json"

    @property
    def legacy_card_url(self) -> str:
        # Peer discovery compatibility is distinct from removed legacy gateway modes.
        return self.endpoint + "/.well-known/agent.json"
