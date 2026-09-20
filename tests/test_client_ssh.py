from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import signal
import ssl
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_ssh_tunnel import fake_ssh as ssh_fixture
from test_ssh_tunnel import unused_port, wait_for

from hermes_a2a_gateway.client import ClientService
from hermes_a2a_gateway.native_delivery import NativeOrigin

fake_ssh = ssh_fixture


@pytest.fixture
async def tls_peer(tmp_path, request):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "broker.example")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("broker.example")]
                + (
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                    if getattr(request, "param", None) == "loopback"
                    else []
                )
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "ca.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    keyfile.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    requests, names, operations = [], [], {}
    lost_ack = {"enabled": False}
    context.set_servername_callback(lambda sock, server_name, ctx: names.append(server_name))

    async def connected(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            method, path, _ = head.split("\r\n", 1)[0].split()
            headers = {
                line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                for line in head.split("\r\n")[1:]
                if ":" in line
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            requests.append((method, path, headers, body))
            status, result = 200, {"ok": True}
            if path != "/healthz" and headers.get("authorization") != "Bearer fixture-token":
                status, result = 401, {"error": {"code": "unauthorized"}}
            elif method == "POST" and path == "/v1/operations":
                command = json.loads(body)
                result = {
                    "operation_id": command["operation_id"],
                    "flow_id": command["flow_id"],
                    "state": "accepted",
                    "result_id": None,
                    "result": None,
                    "error": None,
                    "remote_task_id": None,
                    "created_at": "2026-09-21T00:00:00Z",
                    "updated_at": "2026-09-21T00:00:00Z",
                }
                operations[result["operation_id"]] = result
                if lost_ack["enabled"]:
                    lost_ack["enabled"] = False
                    return
                status = 202
            elif path.startswith("/v1/operations/"):
                operation_id = path.rsplit("/", 1)[-1]
                result = operations.get(operation_id)
                if result is None:
                    status, result = 404, {"error": {"code": "not_found"}}
            data = json.dumps(result).encode()
            writer.write(
                (
                    f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + data
            )
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(connected, "127.0.0.1", 0, ssl=context)
    yield {
        "port": server.sockets[0].getsockname()[1],
        "ca": str(certfile),
        "requests": requests,
        "sni": names,
        "operations": operations,
        "lost_ack": lost_ack,
    }
    server.close()
    await server.wait_closed()


def settings(tmp_path, fake_ssh, tls_peer, **changes):
    from hermes_a2a_gateway.client_settings import ClientSettings

    port = unused_port()
    config = ClientSettings(
        broker_url=f"https://127.0.0.1:{port}",
        token="fixture-token",
        payload_key=Fernet.generate_key().decode(),
        state_dir=tmp_path / "client",
        transport_mode="ssh-tunnel",
        ssh_host="configured-alias",
        ssh_local_port=port,
        ssh_command=str(fake_ssh[0]),
        ssh_remote_port=tls_peer["port"],
        ca_file=tls_peer["ca"],
        ssh_tls_server_name="broker.example",
        native_delivery=False,
    )
    return replace(config, **changes)


async def test_verified_tls_and_bearer_through_owned_tunnel_with_explicit_sni(tmp_path, fake_ssh, tls_peer):
    client = ClientService(settings(tmp_path, fake_ssh, tls_peer))
    try:
        assert client.health()["connection"]["broker"]["state"] == "paused"
        with pytest.raises(httpx.ConnectError, match="ssh_tunnel_unavailable"):
            await client.broker.get("/healthz")
        assert tls_peer["requests"] == []
        await client.tunnel.start()
        await wait_for(lambda: client.tunnel.available)
        report = await client.diagnostics()
        assert report["connection"]["ssh"]["ready"]
        assert report["connection"]["tls"]["state"] == "verified"
        assert report["connection"]["broker"]["state"] == "authenticated"
        assert "broker.example" in tls_peer["sni"]
        assert all(item[2]["authorization"] == "Bearer fixture-token" for item in tls_peer["requests"])
        assert all(item[2]["host"] == f"broker.example:{tls_peer['port']}" for item in tls_peer["requests"])
        assert all(item[0] == "GET" for item in tls_peer["requests"])
    finally:
        await client.close()


@pytest.mark.parametrize(
    "changes", [{"ca_file": None}, {"ssh_tls_server_name": "wrong.example"}, {"ssh_tls_server_name": None}]
)
async def test_tunnel_never_bypasses_ca_or_hostname_verification(tmp_path, fake_ssh, tls_peer, changes):
    client = ClientService(settings(tmp_path, fake_ssh, tls_peer, **changes))
    try:
        await client.tunnel.start()
        await wait_for(lambda: client.tunnel.available)
        report = await client.diagnostics()
        assert report["connection"]["ssh"]["ready"]
        assert report["connection"]["tls"]["state"] == "verification_failed"
        assert report["connection"]["broker"]["state"] == "not_checked"
        assert tls_peer["requests"] == []
    finally:
        await client.close()


async def test_broker_auth_failure_is_distinct_from_ssh_and_tls(tmp_path, fake_ssh, tls_peer):
    client = ClientService(settings(tmp_path, fake_ssh, tls_peer, token="wrong-token"))
    try:
        await client.tunnel.start()
        await wait_for(lambda: client.tunnel.available)
        report = await client.diagnostics()
        assert report["connection"]["ssh"]["ready"]
        assert report["connection"]["tls"]["state"] == "verified"
        assert report["connection"]["broker"]["state"] == "authentication_failed"
    finally:
        await client.close()


async def test_lost_broker_ack_then_tunnel_reconnect_recovers_exact_operation_without_resend(
    tmp_path,
    fake_ssh,
    tls_peer,
    monkeypatch,
):
    client = ClientService(settings(tmp_path, fake_ssh, tls_peer))
    monkeypatch.setattr(client.tunnel, "retry_delay", lambda failures: 0.05)
    origin = NativeOrigin(str(uuid4()), str(uuid4()), "original-call")
    operation_id = client.store.create(origin, "harmless probe fixture", [], 0)
    try:
        await client.tunnel.start()
        await wait_for(lambda: client.tunnel.available)
        tls_peer["lost_ack"]["enabled"] = True
        with pytest.raises(httpx.HTTPError):
            await client.dispatch_pending()
        assert client.store.row(operation_id)["submission_state"] == "pending"
        old_pid = client.tunnel.process.pid
        os.kill(old_pid, signal.SIGTERM)
        await wait_for(lambda: client.tunnel.available and client.tunnel.process.pid != old_pid)
        await client.dispatch_pending()
        assert client.store.row(operation_id)["submission_state"] == "accepted"
        submits = [item for item in tls_peer["requests"] if item[0] == "POST" and item[1] == "/v1/operations"]
        assert len(submits) == 1
        assert json.loads(submits[0][3])["operation_id"] == operation_id
        assert client.store.cursor == 0
    finally:
        await client.close()


@pytest.mark.parametrize("tls_peer", ["loopback"], indirect=True)
async def test_loopback_certificate_works_without_tls_name_override(tmp_path, fake_ssh, tls_peer):
    client = ClientService(settings(tmp_path, fake_ssh, tls_peer, ssh_tls_server_name=None))
    try:
        await client.tunnel.start()
        await wait_for(lambda: client.tunnel.available)
        report = await client.diagnostics()
        assert report["connection"]["tls"]["state"] == "verified"
        assert report["connection"]["broker"]["state"] == "authenticated"
    finally:
        await client.close()
