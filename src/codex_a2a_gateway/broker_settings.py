"""Configuration for the additive, authenticated PostgreSQL broker."""

from __future__ import annotations

import json
import os
from typing import Self
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .settings import is_loopback_url


class BrokerSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    database_url: str = Field(repr=False)
    device_tokens: dict[str, str] = Field(repr=False)
    encryption_key: str = Field(repr=False)
    public_url: str = "https://localhost:8790"
    host: str = "127.0.0.1"
    port: int = Field(default=8790, ge=1, le=65535)
    allow_loopback_http: bool = False
    payload_ttl_seconds: int = Field(default=86400, ge=1)
    artifact_ttl_seconds: int = Field(default=86400, ge=1)
    event_ttl_seconds: int = Field(default=604800, ge=1)
    result_ttl_seconds: int = Field(default=604800, ge=1)
    max_artifact_bytes: int = Field(default=10485760, ge=1, le=104857600)
    device_quota_bytes: int = Field(default=104857600, ge=1)
    max_pending_per_device: int = Field(default=100, ge=1, le=10000)
    max_request_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    max_result_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    poll_seconds: float = Field(default=1.0, ge=0.05, le=60)
    peer_timeout_seconds: float = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if not self.database_url or not self.device_tokens:
            raise ValueError("PostgreSQL and device credentials are required")
        if any(not k or len(k) > 128 or not v or len(v) < 32 for k, v in self.device_tokens.items()):
            raise ValueError("device IDs must be bounded and tokens must contain at least 32 characters")
        if len(set(self.device_tokens.values())) != len(self.device_tokens):
            raise ValueError("each device requires a distinct token")
        try:
            Fernet(self.encryption_key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise ValueError("ENCRYPTION_KEY must be a Fernet key") from None
        host_url = f"http://{self.host if ':' not in self.host else '[' + self.host + ']'}"
        if self.allow_loopback_http and not (is_loopback_url(host_url) and is_loopback_url(self.public_url)):
            raise ValueError("ALLOW_LOOPBACK_HTTP requires a loopback bind and public URL")
        parsed = urlparse(self.public_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_URL must be an HTTPS origin")
        if parsed.path not in ("", "/"):
            raise ValueError("PUBLIC_URL must be an origin without a path")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and self.allow_loopback_http
            and is_loopback_url(self.public_url)
            and is_loopback_url(f"http://{self.host if ':' not in self.host else '[' + self.host + ']'}")
        ):
            raise ValueError("HTTPS is required except explicit loopback development")
        return self

    @classmethod
    def from_env(cls) -> BrokerSettings:
        prefix = "CODEX_A2A_GATEWAY_BROKER_"
        values: dict[str, object] = {}
        for name in cls.model_fields:
            raw = os.environ.get(prefix + name.upper())
            if raw is not None:
                if name == "device_tokens":
                    try:
                        values[name] = json.loads(raw)
                    except ValueError:
                        raise ValueError("DEVICE_TOKENS must be a JSON device-to-token object") from None
                else:
                    values[name] = raw
        return cls.model_validate(values)
