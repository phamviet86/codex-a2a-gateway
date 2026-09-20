"""Configuration for the additive, private v0.6 client transport."""

from __future__ import annotations

import ipaddress
import math
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

PREFIX = "CODEX_A2A_GATEWAY_CLIENT_"


@dataclass(frozen=True)
class ClientSettings:
    broker_url: str
    token: str = field(repr=False)
    payload_key: str = field(repr=False)
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/state/codex-a2a-gateway/client")
    ca_file: str | None = None
    allow_loopback_http: bool = False
    inline_wait_seconds: float = 15
    request_timeout_seconds: float = 20
    max_upload_bytes: int = 8 * 1024 * 1024
    retention_seconds: float = 7 * 86400
    payload_ttl_seconds: float = 86400
    max_pending: int = 256
    codex_command: str = "codex"
    native_delivery: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.broker_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("BROKER_URL must be an origin without credentials, query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("BROKER_URL must not contain a path")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback and self.allow_loopback_http):
            raise ValueError("BROKER_URL requires HTTPS; HTTP is restricted to explicit loopback development")
        if not self.token or "\n" in self.token or "\r" in self.token:
            raise ValueError("CLIENT_TOKEN is required and must be a valid bearer token")
        try:
            Fernet(self.payload_key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("CLIENT_PAYLOAD_KEY must be an environment-provided Fernet key") from exc
        for name in ("inline_wait_seconds", "request_timeout_seconds", "retention_seconds", "payload_ttl_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.inline_wait_seconds > 60 or self.request_timeout_seconds > 300:
            raise ValueError("client wait or request timeout exceeds limit")
        if not 1 <= self.max_upload_bytes <= 32 * 1024 * 1024 or not 1 <= self.max_pending <= 10000:
            raise ValueError("invalid client upload or admission bound")

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "client.sock"

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.ca_file)

    @classmethod
    def from_env(cls) -> ClientSettings:
        def get(name: str, default: str = "") -> str:
            return os.environ.get(PREFIX + name, default)

        return cls(
            broker_url=get("BROKER_URL"),
            token=get("TOKEN"),
            payload_key=get("PAYLOAD_KEY"),
            state_dir=Path(get("STATE_DIR", str(Path.home() / ".local/state/codex-a2a-gateway/client"))),
            ca_file=get("CA_FILE") or None,
            allow_loopback_http=get("ALLOW_LOOPBACK_HTTP", "false").lower() == "true",
            inline_wait_seconds=float(get("INLINE_WAIT_SECONDS", "15")),
            request_timeout_seconds=float(get("REQUEST_TIMEOUT_SECONDS", "20")),
            max_upload_bytes=int(get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))),
            retention_seconds=float(get("RETENTION_SECONDS", str(7 * 86400))),
            payload_ttl_seconds=float(get("PAYLOAD_TTL_SECONDS", "86400")),
            max_pending=int(get("MAX_PENDING", "256")),
            codex_command=get("CODEX_COMMAND", "codex"),
            native_delivery=get("NATIVE_DELIVERY", "true").lower() == "true",
        )
