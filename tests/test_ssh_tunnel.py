from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import sys
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from hermes_a2a_gateway.client_settings import ClientSettings
from hermes_a2a_gateway.ssh_tunnel import SSHConfig, SSHTunnel, classify_failure


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def assert_local_port_released(port):
    # TIME_WAIT from a completed readiness connection is not a live listener.
    # First prove connection refusal, then use the same reuse policy as SSH's
    # real preflight. Never use SO_REUSEPORT to share an existing listener.
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED, "port is still accepting connections"
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    record = tmp_path / "ssh-record.jsonl"
    monkeypatch.setenv("FAKE_SSH_RECORD", str(record))
    executable = tmp_path / "fake-ssh"
    executable.write_text(
        f"#!{sys.executable}\n"
        + r"""
import asyncio, json, os, pathlib, signal, sys, psutil
mode = os.environ.get("FAKE_SSH_MODE", "ok")
assert not any(key.startswith(("CODEX_A2A_GATEWAY_", "HERMES_A2A_GATEWAY_")) for key in os.environ)
args = sys.argv[1:]
for required in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ControlMaster=no", "ControlPath=none",
                 "ExitOnForwardFailure=yes", "ForkAfterAuthentication=no", "ForwardAgent=no"):
    assert required in args, required
record = pathlib.Path(os.environ["FAKE_SSH_RECORD"])
with record.open("a") as file:
    file.write(json.dumps({"pid": os.getpid(), "pgid": os.getpgid(0),
                           "started": psutil.Process().create_time(), "args": args}) + "\n")
if "-G" in args:
    if mode == "config_forward":
        print("localforward 0.0.0.0:9000 127.0.0.1:80")
    else:
        print("hostname synthetic-host\nuser fixture")
    sys.exit()
attempt = sum("-L" in json.loads(row)["args"] for row in record.read_text().splitlines())
if mode in {"auth", "hostkey", "config"} or mode == "network_once" and attempt == 1:
    message = {"auth": "Permission denied (publickey)", "hostkey": "Host key verification failed",
               "config": "Bad configuration option", "network_once": "Connection refused"}[mode]
    print(message + " PRIVATE-STDERR-MUST-NOT-LEAK", file=sys.stderr)
    sys.exit(255)
if mode in {"hang", "no_listener"}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "hang" else signal.SIG_DFL)
    import time
    while True: time.sleep(1)
if mode in {"stubborn_child", "child"}:
    import subprocess
    code = "import signal,time; "
    if mode == "stubborn_child":
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    code += "time.sleep(600)"
    child = subprocess.Popen([sys.executable, "-c", code, str(record)])
    with record.open("a") as file:
        file.write(json.dumps({"pid": child.pid, "pgid": os.getpgid(child.pid),
                              "started": psutil.Process(child.pid).create_time(), "args": ["owned-helper"]}) + "\n")
forward = args[args.index("-L") + 1].split(":")
local_port, remote_port = int(forward[1]), int(forward[-1])
async def connected(reader, writer):
    try:
        peer_reader, peer_writer = await asyncio.open_connection("127.0.0.1", remote_port)
        async def copy(source, target):
            try:
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
            finally:
                target.close()
        await asyncio.gather(copy(reader, peer_writer), copy(peer_reader, writer))
    except (OSError, ConnectionError):
        writer.close()
    finally:
        await writer.wait_closed()
async def main():
    server = await asyncio.start_server(connected, "127.0.0.1", local_port)
    if mode != "no_confirm":
        print(f"debug1: Local forwarding listening on 127.0.0.1 port {local_port}.", file=sys.stderr, flush=True)
    if mode == "crash_once" and attempt == 1:
        asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    async with server:
        await server.serve_forever()
asyncio.run(main())
"""
    )
    executable.chmod(0o700)
    return executable, record


async def wait_for(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def rows(record):
    return [json.loads(row) for row in record.read_text().splitlines()] if record.exists() else []


def assert_process_gone(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_ssh_arguments_are_fixed_argv_and_alias_preserves_config_auth():
    config = SSHConfig(host="operator-vps", user="fixture", port=2222, identity_file="/tmp/key with spaces")
    argv = config.argv()
    assert argv[-2:] == ["--", "operator-vps"]
    assert argv[argv.index("-i") + 1] == "/tmp/key with spaces"
    assert "127.0.0.1:18790:127.0.0.1:443" in argv
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert "ServerAliveInterval=15" in argv and "ServerAliveCountMax=3" in argv
    assert "-p" not in SSHConfig(host="configured-alias").argv()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "-oProxyCommand=bad"},
        {"host": "name;touch x"},
        {"host": "user@host"},
        {"host": "host\n"},
        {"host": "ok", "remote_host": "$(bad)"},
        {"host": "ok", "local_port": 0},
        {"host": "ok", "user": "-root"},
        {"host": "ok", "port": 65536},
    ],
)
def test_ssh_rejects_option_injection_and_invalid_connection_config(kwargs):
    with pytest.raises(ValueError):
        SSHConfig(**kwargs)


def test_client_settings_require_tls_loopback_port_and_explicit_identity(tmp_path):
    base = ClientSettings(
        broker_url="https://127.0.0.1:18790",
        token="fixture",
        payload_key=Fernet.generate_key().decode(),
        state_dir=tmp_path,
        transport_mode="ssh-tunnel",
        ssh_host="operator-vps",
    )
    assert replace(base, ssh_tls_server_name="broker.example").ssh_config().host == "operator-vps"
    for kwargs in [
        {"broker_url": "https://remote.example:18790"},
        {"broker_url": "https://127.0.0.1:9443"},
        {"broker_url": "http://127.0.0.1:18790", "allow_loopback_http": True},
        {"ssh_host": None},
        {"ssh_tls_server_name": "-invalid"},
        {"transport_mode": "guess"},
    ]:
        with pytest.raises(ValueError):
            replace(base, **kwargs)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("auth", "ssh_authentication_failed"),
        ("hostkey", "ssh_host_key_failed"),
        ("config", "ssh_configuration_failed"),
        ("config_forward", "ssh_configured_forwarding_not_allowed"),
    ],
)
async def test_fatal_ssh_errors_stop_without_blind_retry_or_raw_stderr(fake_ssh, monkeypatch, mode, code):
    executable, record = fake_ssh
    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port()))
    await tunnel.start()
    await wait_for(lambda: tunnel.state == "failed")
    assert tunnel.health()["error"] == code
    assert "PRIVATE-STDERR" not in json.dumps(tunnel.health())
    count = len(rows(record))
    await asyncio.sleep(0.1)
    assert len(rows(record)) == count
    await tunnel.close()
    for row in rows(record):
        assert_process_gone(row["pid"])


async def test_owned_ssh_lifecycle_and_environment(fake_ssh, monkeypatch):
    executable, record = fake_ssh
    monkeypatch.setenv("CODEX_A2A_GATEWAY_CLIENT_TOKEN", "secret")
    monkeypatch.setenv("HERMES_A2A_GATEWAY_CLIENT_TOKEN", "also-secret")
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port()))
    await tunnel.start()
    await wait_for(lambda: tunnel.available)
    assert tunnel.health()["state"] == "ready"
    pid = tunnel.process.pid
    with pytest.raises(AssertionError, match="still accepting"):
        assert_local_port_released(tunnel.config.local_port)
    await tunnel.close()
    assert not tunnel.available and tunnel.state == "stopped"
    assert_process_gone(pid)
    assert_local_port_released(tunnel.config.local_port)
    assert len(rows(record)) == 2


@pytest.mark.parametrize("mode", ["network_once", "crash_once"])
async def test_transient_disconnect_reconnects_with_new_owned_process(fake_ssh, monkeypatch, mode):
    executable, record = fake_ssh
    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port()))
    monkeypatch.setattr(tunnel, "retry_delay", lambda failures: 0.05)
    await tunnel.start()
    await wait_for(lambda: tunnel.available and tunnel.attempts == 2)
    assert tunnel.generation == (2 if mode == "crash_once" else 1)
    await tunnel.close()
    for row in rows(record):
        assert_process_gone(row["pid"])


async def test_occupied_port_is_fatal_and_never_adopted(fake_ssh):
    executable, record = fake_ssh
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=port))
        await tunnel.start()
        await wait_for(lambda: tunnel.state == "failed")
        assert tunnel.error == "ssh_local_port_in_use"
        assert not tunnel.available
        assert all("-L" not in row["args"] for row in rows(record))
        await tunnel.close()
        assert occupied.fileno() != -1


async def test_shutdown_while_starting_kills_stubborn_owned_process(fake_ssh, monkeypatch):
    executable, record = fake_ssh
    monkeypatch.setenv("FAKE_SSH_MODE", "hang")
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port()))
    await tunnel.start()
    await wait_for(lambda: len(rows(record)) == 2)
    pid = rows(record)[-1]["pid"]
    await asyncio.wait_for(tunnel.close(), 5)
    assert_process_gone(pid)


async def test_missing_key_or_executable_fails_without_retry(fake_ssh, tmp_path):
    executable, _ = fake_ssh
    for config in [
        SSHConfig(host="fixture", command=str(executable), identity_file=str(tmp_path / "absent")),
        SSHConfig(host="fixture", command=str(tmp_path / "absent")),
    ]:
        tunnel = SSHTunnel(config)
        await tunnel.start()
        await wait_for(lambda current=tunnel: current.state == "failed")
        assert tunnel.attempts == 0
        await tunnel.close()


def test_backoff_is_bounded_exponential_with_jitter():
    assert 0.75 <= SSHTunnel.retry_delay(1) <= 1.25
    assert 1.5 <= SSHTunnel.retry_delay(2) <= 2.5
    assert all(0 < SSHTunnel.retry_delay(failures) <= 30 for failures in range(1, 100))
    failure = classify_failure(b"debug1: identity file /private/id_ed25519 type 3\nConnection refused", 255)
    assert failure.retryable


@pytest.mark.parametrize("mode", ["no_confirm", "no_listener"])
async def test_startup_timeout_never_claims_unconfirmed_listener(fake_ssh, monkeypatch, mode):
    executable, record = fake_ssh
    monkeypatch.setenv("FAKE_SSH_MODE", mode)
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port(), connect_timeout=1))
    monkeypatch.setattr(tunnel, "retry_delay", lambda failures: 10)
    await tunnel.start()
    await wait_for(lambda: tunnel.state == "backoff")
    assert tunnel.error == "ssh_startup_timeout"
    assert not tunnel.available
    await tunnel.close()
    for row in rows(record):
        assert_process_gone(row["pid"])


async def test_cancel_during_subprocess_creation_still_reaps_owned_child(fake_ssh, monkeypatch):
    executable, record = fake_ssh
    original_spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        created.set()
        await asyncio.sleep(0.05)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port()))
    await tunnel.start()
    await created.wait()
    await asyncio.wait_for(tunnel.close(), 2)
    for row in rows(record):
        assert_process_gone(row["pid"])


async def test_inherited_group_cleanup_reaps_owned_stubborn_child_without_signaling_sibling(fake_ssh, monkeypatch):
    executable, record = fake_ssh
    monkeypatch.setenv("FAKE_SSH_MODE", "stubborn_child")
    tunnel = SSHTunnel(SSHConfig(host="fixture", command=str(executable), local_port=unused_port()))
    tunnel._inherit_job_group = True

    def forbidden_group_signal(*args):
        raise AssertionError("inherited/shared process group must never be signaled")

    monkeypatch.setattr(os, "killpg", forbidden_group_signal)
    sibling = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    try:
        await tunnel.start()
        await wait_for(lambda: tunnel.available)
        helpers = [row for row in rows(record) if row["args"] == ["owned-helper"]]
        assert len(helpers) == 1
        await wait_for(lambda: helpers[0]["pid"] in tunnel._owned_children)
        assert os.getpgid(tunnel.process.pid) == os.getpgid(0)
        assert helpers[0]["pgid"] == os.getpgid(0)
        await asyncio.wait_for(tunnel.close(), 5)
        assert sibling.returncode is None
        await wait_for(lambda: not _process_alive(helpers[0]["pid"]))
        assert not tunnel._owned_children
    finally:
        await tunnel.close()
        if sibling.returncode is None:
            sibling.terminate()
        await sibling.wait()


def _process_alive(pid):
    import psutil

    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("HERMES_A2A_GATEWAY_TEST_LAUNCHD") != "1",
    reason="opt-in macOS disposable LaunchAgent containment test",
)
async def test_launchd_sigkill_reaps_owned_ssh_and_helpers_then_restarts_same_port(fake_ssh, tmp_path):
    import plistlib
    import signal
    from pathlib import Path
    from uuid import uuid4

    import psutil

    executable, record = fake_ssh
    label = "org.openai.hermes-a2a.synthetic-" + uuid4().hex
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    history = tmp_path / "synthetic-starts.jsonl"
    port = unused_port()
    source = f"""
import asyncio,json,os,psutil
from pathlib import Path
from hermes_a2a_gateway.ssh_tunnel import SSHConfig,SSHTunnel
async def main():
    tunnel=SSHTunnel(SSHConfig(host="synthetic-no-real-ssh",command={str(executable)!r},local_port={port}))
    try:
        await tunnel.start()
        async with asyncio.timeout(10):
            while not tunnel.available:
                await asyncio.sleep(0.02)
        with Path({str(history)!r}).open("a") as file:
            file.write(json.dumps({{"label":{label!r},"pid":os.getpid(),"pgid":os.getpgid(0),
                                   "started":psutil.Process().create_time(),"ssh_pid":tunnel.process.pid,"ssh_pgid":os.getpgid(tunnel.process.pid)}})+"\\n")
        await asyncio.Event().wait()
    finally:
        await tunnel.close()
asyncio.run(main())
"""
    plist = tmp_path / f"{label}.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [sys.executable, "-c", source],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 1,
                "AbandonProcessGroup": False,
                "ExitTimeOut": 3,
                "EnvironmentVariables": {
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    "FAKE_SSH_MODE": "child",
                    "FAKE_SSH_RECORD": str(record),
                },
                "StandardOutPath": str(tmp_path / "synthetic.out"),
                "StandardErrorPath": str(tmp_path / "synthetic.err"),
            }
        )
    )

    async def launchctl(*args):
        process = await asyncio.create_subprocess_exec(
            "/bin/launchctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        output, error = await process.communicate()
        return process.returncode, output, error

    bootstrapped = False
    try:
        result, _, error = await launchctl("bootstrap", domain, str(plist))
        assert result == 0, error.decode()
        bootstrapped = True
        await wait_for(lambda: len(rows(history)) >= 1, timeout=15)
        first = rows(history)[0]
        assert first["pgid"] == first["pid"]
        assert first["ssh_pgid"] == first["pgid"]
        old_helpers = [row for row in rows(record) if row["args"] == ["owned-helper"]]
        assert old_helpers and all(row["pgid"] == first["pgid"] for row in old_helpers)
        daemon = psutil.Process(first["pid"])
        assert source in daemon.cmdline()
        daemon.send_signal(signal.SIGKILL)
        await wait_for(lambda: len(rows(history)) >= 2, timeout=20)
        second = rows(history)[1]
        assert second["pid"] != first["pid"]
        assert second["ssh_pid"] != first["ssh_pid"]
        assert second["pgid"] == second["ssh_pgid"]
        await wait_for(lambda: not _process_alive(first["ssh_pid"]))
        await wait_for(lambda: all(not _process_alive(row["pid"]) for row in old_helpers))
        _, connection = await asyncio.open_connection("127.0.0.1", port)
        connection.close()
        await connection.wait_closed()
    finally:
        if bootstrapped:
            await launchctl("bootout", target)
        try:
            await wait_for(lambda: all(not _process_alive(row["pid"]) for row in rows(record)), timeout=10)
            await wait_for(lambda: all(not _process_alive(row["pid"]) for row in rows(history)), timeout=10)
        finally:
            # A failing containment assertion must still clean only the exact
            # synthetic processes, never a later occupant of a reused PID.
            for row in [*rows(record), *rows(history)]:
                try:
                    process = psutil.Process(row["pid"])
                    if process.create_time() == row.get("started"):
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
    assert_local_port_released(port)
    result, _, _ = await launchctl("print", target)
    assert result != 0
