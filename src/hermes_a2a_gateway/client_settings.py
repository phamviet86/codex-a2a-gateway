"""Configuration for the private client transport and managed SSH tunnel."""

from __future__ import annotations

import ipaddress
import math
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .ssh_tunnel import SSHConfig, valid_host

PREFIX = "HERMES_A2A_GATEWAY_CLIENT_"


@dataclass(frozen=True)
class ClientSettings:
    broker_url: str
    token: str = field(repr=False)
    payload_key: str = field(repr=False)
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/state/hermes-a2a-gateway/client")
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
    transport_mode: str = "direct"
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    ssh_identity_file: str | None = None
    ssh_command: str = "ssh"
    ssh_local_port: int = 18790
    ssh_remote_host: str = "127.0.0.1"
    ssh_remote_port: int = 443
    ssh_tls_server_name: str | None = None
    ssh_connect_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if self.transport_mode not in {"direct", "ssh-tunnel"}:
            raise ValueError("TRANSPORT_MODE must be direct or ssh-tunnel")
        parsed = urlsplit(self.broker_url)
        if self.transport_mode == "ssh-tunnel":
            self.ssh_config()
            if parsed.scheme != "https" or parsed.hostname != "127.0.0.1" or parsed.port != self.ssh_local_port:
                raise ValueError("SSH tunnel BROKER_URL must be https://127.0.0.1:<SSH_LOCAL_PORT>")
        if self.ssh_tls_server_name is not None and (
            self.transport_mode != "ssh-tunnel" or not valid_host(self.ssh_tls_server_name)
        ):
            raise ValueError("SSH_TLS_SERVER_NAME requires ssh-tunnel and a valid certificate hostname")
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
        for name in ("request_timeout_seconds", "retention_seconds", "payload_ttl_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.inline_wait_seconds) or not 0 <= self.inline_wait_seconds <= 60:
            raise ValueError("INLINE_WAIT_SECONDS must be finite and between 0 and 60")
        if self.request_timeout_seconds > 300:
            raise ValueError("client wait or request timeout exceeds limit")
        if not 1 <= self.max_upload_bytes <= 32 * 1024 * 1024 or not 1 <= self.max_pending <= 10000:
            raise ValueError("invalid client upload or admission bound")

    def ssh_config(self) -> SSHConfig:
        if self.ssh_host is None:
            raise ValueError("SSH_HOST is required in ssh-tunnel mode")
        return SSHConfig(
            host=self.ssh_host,
            user=self.ssh_user,
            port=self.ssh_port,
            identity_file=self.ssh_identity_file,
            command=self.ssh_command,
            local_port=self.ssh_local_port,
            remote_host=self.ssh_remote_host,
            remote_port=self.ssh_remote_port,
            connect_timeout=self.ssh_connect_timeout_seconds,
        )

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
            state_dir=Path(get("STATE_DIR", str(Path.home() / ".local/state/hermes-a2a-gateway/client"))),
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
            transport_mode=get("TRANSPORT_MODE", "direct"),
            ssh_host=get("SSH_HOST") or None,
            ssh_user=get("SSH_USER") or None,
            ssh_port=int(get("SSH_PORT")) if get("SSH_PORT") else None,
            ssh_identity_file=get("SSH_IDENTITY_FILE") or None,
            ssh_command=get("SSH_COMMAND", "ssh"),
            ssh_local_port=int(get("SSH_LOCAL_PORT", "18790")),
            ssh_remote_host=get("SSH_REMOTE_HOST", "127.0.0.1"),
            ssh_remote_port=int(get("SSH_REMOTE_PORT", "443")),
            ssh_tls_server_name=get("SSH_TLS_SERVER_NAME") or None,
            ssh_connect_timeout_seconds=int(get("SSH_CONNECT_TIMEOUT_SECONDS", "10")),
        )
