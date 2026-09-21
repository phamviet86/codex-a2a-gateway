"""An optional daemon-owned OpenSSH process; never a model-facing SSH proxy.

Only configured connection details are accepted. A ready local listener is not
proof of remote TLS or broker authentication; those are checked independently.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import random
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]


class TunnelFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def valid_host(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", value))


@dataclass(frozen=True)
class SSHConfig:
    host: str
    local_port: int = 18790
    remote_host: str = "127.0.0.1"
    remote_port: int = 443
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    command: str = "ssh"
    connect_timeout: int = 10

    def __post_init__(self) -> None:
        if not valid_host(self.host) or not valid_host(self.remote_host):
            raise ValueError("SSH_HOST and SSH_REMOTE_HOST must be hostnames, addresses, or a configured SSH alias")
        if self.user is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", self.user):
            raise ValueError("SSH_USER must be an SSH account name")
        for name, value, minimum in (
            ("SSH_LOCAL_PORT", self.local_port, 1024),
            ("SSH_REMOTE_PORT", self.remote_port, 1),
            ("SSH_PORT", self.port, 1),
        ):
            if value is not None and (type(value) is not int or not minimum <= value <= 65535):
                raise ValueError(f"{name} is outside the supported port range")
        if type(self.connect_timeout) is not int or not 1 <= self.connect_timeout <= 60:
            raise ValueError("SSH_CONNECT_TIMEOUT_SECONDS must be an integer from 1 to 60")
        if not self.command or self.command.startswith("-") or any(c in self.command for c in "\r\n\x00"):
            raise ValueError("SSH_COMMAND must name one executable, without command arguments")
        if self.identity_file is not None and (
            not self.identity_file or any(c in self.identity_file for c in "\r\n\x00")
        ):
            raise ValueError("SSH_IDENTITY_FILE must be a valid local key path")

    def argv(self, *, inspect: bool = False) -> list[str]:
        options = {
            "LogLevel": "DEBUG1",
            "BatchMode": "yes",
            "StrictHostKeyChecking": "yes",
            "ExitOnForwardFailure": "yes",
            "ConnectTimeout": str(self.connect_timeout),
            "ConnectionAttempts": "1",
            "ServerAliveInterval": "15",
            "ServerAliveCountMax": "3",
            "ControlMaster": "no",
            "ControlPath": "none",
            "ControlPersist": "no",
            "ForkAfterAuthentication": "no",
            "PermitLocalCommand": "no",
            "GatewayPorts": "no",
            "ForwardAgent": "no",
            "ForwardX11": "no",
        }
        argv = [self.command, "-N", "-T", "-n"]
        for key, value in options.items():
            argv.extend(["-o", f"{key}={value}"])
        if self.port is not None:
            argv.extend(["-p", str(self.port)])
        if self.user:
            argv.extend(["-l", self.user])
        if self.identity_file:
            argv.extend(["-i", str(Path(self.identity_file).expanduser()), "-o", "IdentitiesOnly=yes"])
        if inspect:
            argv.append("-G")
        else:
            remote = f"[{self.remote_host}]" if ":" in self.remote_host else self.remote_host
            argv.extend(["-L", f"127.0.0.1:{self.local_port}:{remote}:{self.remote_port}"])
        return [*argv, "--", self.host]


def classify_failure(stderr: bytes, returncode: int | None) -> TunnelFailure:
    text = stderr.lower()
    fatal = {
        "ssh_host_key_failed": (
            b"host key verification failed",
            b"remote host identification has changed",
            b"no matching host key type",
            b"offending",
            b"host key is known",
        ),
        "ssh_authentication_failed": (
            b"permission denied",
            b"authentication failed",
            b"too many authentication failures",
            b"no supported authentication methods",
        ),
        "ssh_local_port_in_use": (
            b"address already in use",
            b"cannot listen to port",
            b"could not request local forwarding",
        ),
        "ssh_configuration_failed": (
            b"bad configuration",
            b"bad configuration option",
            b"unknown option",
            b"invalid port",
            b"bad port",
            b"no such identity",
            b"bad owner",
        ),
    }
    if b"identity file" in text and b"not accessible" in text:
        return TunnelFailure("ssh_identity_file_unavailable")
    for code, markers in fatal.items():
        if any(marker in text for marker in markers):
            return TunnelFailure(code)
    if returncode is not None and returncode < 0:
        return TunnelFailure("ssh_process_exited", retryable=True)
    if returncode == 0 or any(
        marker in text
        for marker in (
            b"connection refused",
            b"connection timed out",
            b"operation timed out",
            b"no route to host",
            b"network is unreachable",
            b"connection reset",
            b"broken pipe",
            b"connection closed",
            b"closed by remote host",
            b"could not resolve hostname",
            b"temporary failure",
            b"server not responding",
            b"timeout, server",
        )
    ):
        return TunnelFailure("ssh_network_unavailable", retryable=True)
    return TunnelFailure("ssh_failed")


class SSHTunnel:
    def __init__(self, config: SSHConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.state = "stopped"
        self.error: str | None = None
        self.attempts = 0
        self.retry_at: float | None = None
        self.generation = 0
        self._stderr = bytearray()
        self._forward_confirmed = False
        self._ready_since: float | None = None
        self._reader: asyncio.Task[None] | None = None
        self._inherit_job_group = sys.platform == "darwin"
        self._owned_root: Any = None
        self._owned_children: dict[int, Any] = {}
        self._child_tracker: asyncio.Task[None] | None = None

    @staticmethod
    def environment() -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CODEX_A2A_GATEWAY_", "HERMES_A2A_GATEWAY_", "CODEX_BRIDGE_", "HERMES_BRIDGE_"))
            and key not in {"HERMES_A2A_TOKEN", "A2A_GATEWAY_TOKEN"}
        }

    @property
    def available(self) -> bool:
        return self.ready.is_set() and self.process is not None and self.process.returncode is None

    def health(self) -> dict[str, Any]:
        return {
            "mode": "ssh-tunnel",
            "state": self.state,
            "ready": self.available,
            "error": self.error,
            "attempts": self.attempts,
            "generation": self.generation,
            "retry_in_seconds": max(0, round(self.retry_at - time.monotonic(), 2)) if self.retry_at else None,
        }

    async def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def close(self) -> None:
        try:
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        finally:
            await self._cleanup()
            self.task = None
            self.state = "stopped"
            self.retry_at = None

    async def _spawn(self, argv: list[str], *, capture_stdout: bool = False) -> None:
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment(),
                # launchd reaps only the job's process group after daemon death.
                # On macOS every child must remain in that group. Linux retains
                # a private group, also contained by the service's cgroup.
                start_new_session=not self._inherit_job_group,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Even cancellation during spawn retains ownership for final cleanup.
            self.process = await spawning
            self._capture_owned_root()
            raise
        self._capture_owned_root()

    def _capture_owned_root(self) -> None:
        self._owned_root = None
        self._owned_children.clear()
        if self._inherit_job_group and self.process is not None and self.process.returncode is None:
            with contextlib.suppress(psutil.NoSuchProcess):
                process = psutil.Process(self.process.pid)
                if process.ppid() == os.getpid() and process.pid != os.getpid():
                    self._owned_root = process
                    self._child_tracker = asyncio.create_task(self._track_owned_children())

    def _capture_owned_children(self) -> None:
        if self._owned_root is not None:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                for child in self._owned_root.children(recursive=True):
                    self._owned_children[child.pid] = child

    async def _track_owned_children(self) -> None:
        # Retain identity-checked handles even if SSH exits and its helpers are
        # reparented before cleanup. Never discover children by shared PGID.
        while self.process is not None and self.process.returncode is None:
            self._capture_owned_children()
            await asyncio.sleep(0.05)

    def _signal_owned_group(self, signum: int) -> None:
        if self._inherit_job_group:
            # psutil guards send_signal with PID + creation-time identity checks.
            # Signaling the inherited group would also kill the daemon or caller.
            self._capture_owned_children()
            processes = [*self._owned_children.values(), self._owned_root]
            for process in processes:
                if process is not None and process.pid != os.getpid():
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        process.send_signal(signum)
        elif self.process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signum)

    def _children_alive(self) -> bool:
        for child in self._owned_children.values():
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    return True
        return False

    async def _cleanup(self) -> None:
        self.ready.clear()
        if self._child_tracker is not None:
            self._child_tracker.cancel()
            await asyncio.gather(self._child_tracker, return_exceptions=True)
            self._child_tracker = None
        deadline = time.monotonic() + 3
        self._signal_owned_group(signal.SIGTERM)
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self._signal_owned_group(signal.SIGKILL)
                await self.process.wait()
        if self._inherit_job_group:
            while self._children_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if self._children_alive():
                self._signal_owned_group(signal.SIGKILL)
        if self._reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(self._reader, return_exceptions=True)), 3)
            except TimeoutError:
                self._signal_owned_group(signal.SIGKILL)
                self._reader.cancel()
                await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        self.process = None
        self._owned_root = None
        self._owned_children.clear()

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(2048):
            self._stderr.extend(chunk)
            confirmation = f"Local forwarding listening on 127.0.0.1 port {self.config.local_port}.".encode()
            if confirmation in self._stderr:
                self._forward_confirmed = True
            if len(self._stderr) > 16384:
                del self._stderr[:-16384]

    async def _inspect_configuration(self) -> None:
        if self.config.identity_file and not Path(self.config.identity_file).expanduser().is_file():
            raise TunnelFailure("ssh_identity_file_unavailable")
        # Reuse aliases and authentication from operator config, but refuse
        # inherited forwards: this daemon owns exactly one loopback listener.
        await self._spawn(self.config.argv(inspect=True), capture_stdout=True)
        assert self.process is not None
        assert self.process.stdout is not None and self.process.stderr is not None
        self._stderr.clear()
        self._reader = asyncio.create_task(self._read_stderr(self.process.stderr))
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                total = 0
                async for line in self.process.stdout:
                    total += len(line)
                    if total > 256 * 1024:
                        raise TunnelFailure("ssh_configuration_failed")
                    if line.split(b" ", 1)[0].lower() in {b"localforward", b"remoteforward", b"dynamicforward"}:
                        raise TunnelFailure("ssh_configured_forwarding_not_allowed")
                await self.process.wait()
                await self._reader
                if self.process.returncode:
                    raise classify_failure(self._stderr, self.process.returncode)
        except (TimeoutError, ValueError) as exc:
            raise TunnelFailure("ssh_configuration_failed") from exc
        finally:
            await self._cleanup()

    async def _connect(self) -> None:
        self._ready_since = None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", self.config.local_port))
            except OSError as exc:
                raise TunnelFailure("ssh_local_port_in_use") from exc
        self._stderr.clear()
        self._forward_confirmed = False
        await self._spawn(self.config.argv())
        assert self.process is not None
        assert self.process.stderr is not None
        self._reader = asyncio.create_task(self._read_stderr(self.process.stderr))
        deadline = time.monotonic() + self.config.connect_timeout + 1
        while self.process.returncode is None and time.monotonic() < deadline:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.config.local_port), 0.2)
            except (OSError, TimeoutError):
                await asyncio.sleep(0.05)
            else:
                writer.close()
                await writer.wait_closed()
                # Check process survival after accept; failed forwarding must not
                # mistake an unrelated process for the owned listener.
                await asyncio.sleep(0.05)
                if self.process.returncode is None and self._forward_confirmed:
                    self.state, self.error = "ready", None
                    self._ready_since = time.monotonic()
                    self.generation += 1
                    self.ready.set()
                    await self.process.wait()
                    break
                await asyncio.sleep(0.05)
        if self.process.returncode is None:
            raise TunnelFailure("ssh_startup_timeout", retryable=True)
        await self._reader
        raise classify_failure(self._stderr, self.process.returncode)

    @staticmethod
    def retry_delay(failures: int) -> float:
        return min(30, random.uniform(0.75, 1.25) * min(30, 2 ** min(failures - 1, 5)))

    async def _run(self) -> None:
        failures = 0
        try:
            try:
                await self._inspect_configuration()
            except (OSError, TunnelFailure) as exc:
                self.state = "failed"
                self.error = exc.code if isinstance(exc, TunnelFailure) else "ssh_executable_unavailable"
                return
            while True:
                if self.attempts:
                    try:
                        await self._inspect_configuration()
                    except (OSError, TunnelFailure) as exc:
                        self.state = "failed"
                        self.error = exc.code if isinstance(exc, TunnelFailure) else "ssh_executable_unavailable"
                        return
                self.state, self.retry_at = "starting", None
                self.attempts += 1
                try:
                    await self._connect()
                except OSError:
                    failure = TunnelFailure("ssh_executable_unavailable")
                except TunnelFailure as exc:
                    failure = exc
                finally:
                    await self._cleanup()
                self.error = failure.code
                if not failure.retryable:
                    self.state = "failed"
                    return
                stable = self._ready_since is not None and time.monotonic() - self._ready_since >= 30
                failures = 1 if stable else failures + 1
                delay = self.retry_delay(failures)
                self.state, self.retry_at = "backoff", time.monotonic() + delay
                await asyncio.sleep(delay)
        finally:
            await self._cleanup()
