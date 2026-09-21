"""Offline tests against the actual pinned, patched Hermes source.

Only unrelated runtime imports and model execution are replaced. Protocol,
security, HTTP dispatch and admission methods execute the published patch.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPAT = ROOT / "compat" / "hermes"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def patched(tmp_path):
    target = tmp_path / "plugins" / "platforms" / "a2a"
    target.mkdir(parents=True)
    for source in (COMPAT / "upstream").glob("*.py"):
        (target / source.name).write_bytes(source.read_bytes())
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    manifest = json.loads((COMPAT / "hermes-a2a-compat.json").read_text())
    for name, hashes in manifest["files"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == hashes["original_sha256"]
    manifest["upstream_commit"] = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    fixture_manifest = tmp_path / "manifest.json"
    fixture_manifest.write_text(json.dumps(manifest))
    utility = load_module("hermes_patch_utility", ROOT / "scripts" / "hermes_patch.py")
    assert utility.execute(tmp_path, fixture_manifest, COMPAT / "hermes-a2a-compat.patch", "apply") == "patched"
    return tmp_path, target, fixture_manifest, utility


@pytest.fixture
def runtime(patched, monkeypatch, tmp_path):
    _, target, _, _ = patched
    shared = types.ModuleType("gateway.platforms._shared")
    shared.coerce_port = lambda value, default: int(value) if str(value).isdigit() else default
    shared.profile_scoped = lambda: False
    monkeypatch.setitem(sys.modules, "gateway.platforms._shared", shared)
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path / "home"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    protocol = load_module("fixture_protocol", target / "protocol.py")
    security = load_module("fixture_security", target / "security.py")
    # Execute actual patched adapter HTTP class and selected unmodified/admission
    # methods; omitted methods only start models, route profiles, or import Hermes.
    tree = ast.parse((target / "adapter.py").read_text())
    methods = {
        "_scope_for_agent",
        "_end_task",
        "_prepare_task",
        "_rpc_message_send",
        "_rpc_message_stream",
        "_sse_headers",
        "_sse_write",
        "_emit_terminal",
        "_rpc_tasks_get",
        "_find_task",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "A2ARequestHandler":
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "A2AAdapter":
            node.bases = []
            node.body = [child for child in node.body if isinstance(child, ast.FunctionDef) and child.name in methods]
            nodes.append(node)
        elif (
            (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id in {"_METHODS", "_METHOD_TABLE"}
            )
            or isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in {"_METHODS", "_METHOD_TABLE"} for t in node.targets)
        ):
            nodes.append(node)
    namespace = {
        "protocol": protocol,
        "security": security,
        "BaseHTTPRequestHandler": BaseHTTPRequestHandler,
        "json": json,
        "logger": logging.getLogger("patch_test"),
        "_MAX_BODY": 1_000_000,
        "_ok": protocol.jsonrpc_result,
        "_err": protocol.jsonrpc_error,
    }
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "patched_adapter", "exec"), namespace)
    adapter = namespace["A2AAdapter"]()
    agent = {"slug": "", "tenant": "", "local": False}
    adapter._agents = {"": agent}
    adapter._scope_for_agent(agent)
    adapter.tasks, adapter._turns = protocol.TaskStore(), protocol.TurnTracker()
    adapter._gateway_limiter = protocol.GatewayContextLimiter()
    adapter._rate_limiter = protocol.RateLimiter()
    adapter._security_context = security.A2ASecurityContext("valid", (), frozenset(), False, "127.0.0.1", "", "valid")
    adapter._public_url = ""
    adapter._register_inline_push = lambda *a, **k: None
    adapter._activate_task = adapter._pop_pending = lambda *a: None
    adapter._forward_to_profile = lambda *a: ("safe result", protocol.STATE_COMPLETED)
    adapter._record_outcome = lambda tid, ctx, peer, state, reply: adapter.tasks.complete(tid, state, reply)
    adapter._route_for_path = lambda path: {"agent": agent, "subpath": path}
    adapter._route_for_request = lambda path, params: {"agent": agent, "subpath": path}
    adapter._build_card = lambda *a, **k: protocol.build_agent_card(name="fixture", url="http://127.0.0.1")
    return SimpleNamespace(
        protocol=protocol, security=security, adapter=adapter, handler=namespace["A2ARequestHandler"]
    )


def params(context="context"):
    return {"message": {"contextId": context, "parts": [{"text": "Harmless fixture request"}]}}


def test_patch_apply_revert_and_hash_conflict(patched):
    root, target, manifest, utility = patched
    patch = COMPAT / "hermes-a2a-compat.patch"
    assert utility.execute(root, manifest, patch, "check") == "patched"
    assert utility.execute(root, manifest, patch, "apply") == "patched"
    assert utility.execute(root, manifest, patch, "revert") == "original"
    assert utility.execute(root, manifest, patch, "revert") == "original"
    (target / "adapter.py").write_text("# user changes\n")
    with pytest.raises(ValueError, match="Conflicting"):
        utility.execute(root, manifest, patch, "apply")
    assert (target / "adapter.py").read_text() == "# user changes\n"


def test_patch_wrong_commit_and_modified_patch_refused(patched, tmp_path):
    root, _, manifest, utility = patched
    data = json.loads(manifest.read_text())
    data["upstream_commit"] = "0" * 40
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="commit"):
        utility.execute(root, manifest, COMPAT / "hermes-a2a-compat.patch", "check")
    tampered = tmp_path / "tampered.patch"
    tampered.write_text("invalid")
    with pytest.raises(ValueError, match="checksum"):
        utility.execute(root, manifest, tampered, "check")


def test_sliding_clock_denials_do_not_extend_and_keys_are_isolated(runtime):
    now = [0.0]
    limiter = runtime.protocol.GatewayContextLimiter(clock=lambda: now[0], wall_clock=lambda: 1000 + now[0])
    for _ in range(5):
        assert limiter.admit("peer", ("agent", "tenant"), "ctx") is None
    denial = limiter.admit("peer", ("agent", "tenant"), "ctx")
    assert denial["retry_after_seconds"] == 60
    assert denial["execution_started"] is False
    now[0] = 59
    assert limiter.admit("peer", ("agent", "tenant"), "ctx")["retry_after_seconds"] == 1
    assert limiter.admit("other", ("agent", "tenant"), "ctx") is None
    assert limiter.admit("peer", ("other", "tenant"), "ctx") is None
    assert limiter.admit("peer", ("agent", "other"), "ctx") is None
    assert limiter.admit("peer", ("agent", "tenant"), "other") is None
    now[0] = 60
    assert limiter.admit("peer", ("agent", "tenant"), "ctx") is None


def test_concurrent_admission_is_bounded(runtime):
    limiter = runtime.protocol.GatewayContextLimiter()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: limiter.admit("peer", ("", ""), "ctx"), range(20)))
    assert results.count(None) == 5


def test_credential_policy_does_not_change_identity(runtime):
    sec = runtime.adapter._security_context
    assert sec.gateway_policy("Bearer valid", "127.0.0.1")
    assert sec.authenticate("Bearer valid", "127.0.0.1") == "ip:127.0.0.1"
    assert not sec.gateway_policy("Bearer invalid", "127.0.0.1")
    assert not sec.gateway_policy(None, "127.0.0.1")
    assert not sec.gateway_policy("Bearer valid", "192.168.1.1")
    other = runtime.security.A2ASecurityContext("valid", (), frozenset(), False, "127.0.0.1", "", "not-valid")
    assert not other.gateway_policy("Bearer not-valid", "127.0.0.1")


def test_25_turns_same_context_and_stored_rejection(runtime):
    now = [0.0]
    adapter, protocol = runtime.adapter, runtime.protocol
    adapter._gateway_limiter = protocol.GatewayContextLimiter(clock=lambda: now[0])
    for _ in range(25):
        task, pending = adapter._prepare_task(params(), "same-peer", gateway_policy=True)
        assert pending is None and task["status"]["state"] == protocol.STATE_COMPLETED
        assert task["contextId"] == "context"
        now[0] += 13
    for _ in range(5):
        adapter._prepare_task(params("burst"), "same-peer", gateway_policy=True)
    task, _ = adapter._prepare_task(params("burst"), "same-peer", gateway_policy=True)
    assert task["status"]["state"] == protocol.STATE_REJECTED
    saved = protocol.TaskStore.to_task(adapter.tasks.get(task["id"]))
    assert saved["metadata"] == task["metadata"]
    for _ in range(10):
        adapter.tasks.get(task["id"])
    now[0] += 60
    task, _ = adapter._prepare_task(params("burst"), "same-peer", gateway_policy=True)
    assert task["status"]["state"] == protocol.STATE_COMPLETED


@pytest.fixture
def http_runtime(runtime):
    server = ThreadingHTTPServer(("127.0.0.1", 0), runtime.handler)
    server.adapter = runtime.adapter
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield runtime, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def request(url, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=json.dumps(body).encode() if body else None, headers=headers)
    with urlopen(req, timeout=5) as response:
        return json.loads(response.read())


def test_http_auth_capabilities_forged_metadata_and_get(http_runtime):
    runtime, url = http_runtime
    for token in (None, "wrong"):
        with pytest.raises(HTTPError) as exc:
            request(url + "/gateway-capabilities", token)
        assert exc.value.code == 401
    capability = request(url + "/gateway-capabilities", "valid")
    assert capability["policy"] == "sliding_window" and capability["window_seconds"] == 60
    runtime.adapter._security_context = runtime.security.A2ASecurityContext(
        "valid", (("legacy", "legacy-peer"),), frozenset(), False, "127.0.0.1", "", "valid"
    )
    assert request(url + "/gateway-capabilities", "legacy")["policy"] == "legacy_turn_limit"
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": params("legacy-context")}
    body["params"]["message"]["metadata"] = {"gateway_policy": True, "policy": "sliding_window"}
    for _ in range(5):
        assert request(url, "legacy", body)["result"]["task"]["status"]["state"] == runtime.protocol.STATE_COMPLETED
    task = request(url, "legacy", body)["result"]["task"]
    diagnostic = task["metadata"][runtime.protocol.GATEWAY_DIAGNOSTICS]
    assert diagnostic["reason"] == "context_turn_limit"
    get = {"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task["id"]}}
    assert request(url, "legacy", get)["result"]["metadata"] == task["metadata"]
    # Selected bearer preserves its original identity and obeys bounded policy.
    body["params"] = params("gateway-context")
    for _ in range(5):
        request(url, "valid", body)
    limited = request(url, "valid", body)["result"]["task"]
    assert limited["metadata"][runtime.protocol.GATEWAY_DIAGNOSTICS]["reason"] == "context_rate_limited"
    assert runtime.adapter.tasks.get(limited["id"])["peer"] == "ip:127.0.0.1"
    runtime.adapter._security_context = runtime.security.A2ASecurityContext(
        "valid", (), frozenset({"other"}), False, "127.0.0.1", "", "valid"
    )
    with pytest.raises(HTTPError) as exc:
        request(url + "/gateway-capabilities", "valid")
    assert exc.value.code == 403


def test_localhost_policy_secret_preserves_existing_unauthenticated_peers(http_runtime):
    runtime, url = http_runtime
    sec = runtime.security.A2ASecurityContext("", (), frozenset(), False, "127.0.0.1", "", "selector")
    runtime.adapter._security_context = sec
    assert sec.authenticate(None, "127.0.0.1") == sec.authenticate("Bearer selector", "127.0.0.1")
    assert sec.gateway_policy("Bearer selector", "127.0.0.1")
    assert not sec.gateway_policy("Bearer wrong", "127.0.0.1")
    assert not sec.gateway_policy(None, "127.0.0.1")
    for token in (None, "wrong"):
        with pytest.raises(HTTPError) as exc:
            request(url + "/gateway-capabilities", token)
        assert exc.value.code == 401
    assert request(url + "/gateway-capabilities", "selector")["policy"] == "sliding_window"
    # Existing unauthenticated loopback submitters retain original legacy policy.
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": params("unselected")}
    for _ in range(5):
        assert request(url, None, body)["result"]["task"]["status"]["state"] == runtime.protocol.STATE_COMPLETED
    denied = request(url, None, body)["result"]["task"]
    assert denied["metadata"][runtime.protocol.GATEWAY_DIAGNOSTICS]["reason"] == "context_turn_limit"


def test_sse_rejection_preserves_diagnostics(http_runtime):
    runtime, url = http_runtime
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": params("streamed")}
    for _ in range(5):
        request(url, "valid", body)
    body["method"] = "SendStreamingMessage"
    req = Request(url, data=json.dumps(body).encode(), headers={"Authorization": "Bearer valid"})
    with urlopen(req, timeout=5) as response:
        events = [
            json.loads(line[6:])["result"]
            for line in response.read().decode().splitlines()
            if line.startswith("data: ")
        ]
    task_diagnostic = events[0]["task"]["metadata"][runtime.protocol.GATEWAY_DIAGNOSTICS]
    assert events[1]["statusUpdate"]["metadata"][runtime.protocol.GATEWAY_DIAGNOSTICS] == task_diagnostic
    assert "final" not in events[1]["statusUpdate"]


def test_patch_mixed_staged_and_symlink_refused(patched):
    root, target, manifest, utility = patched
    patch = COMPAT / "hermes-a2a-compat.patch"
    original = (COMPAT / "upstream" / "security.py").read_bytes()
    (target / "security.py").write_bytes(original)
    with pytest.raises(ValueError, match="Mixed"):
        utility.execute(root, manifest, patch, "apply")
    (target / "security.py").write_bytes((COMPAT / "upstream" / "security.py").read_bytes())
    # Mixed-state remains blocked irrespective of requested action.
    with pytest.raises(ValueError, match="Mixed"):
        utility.execute(root, manifest, patch, "revert")
    (target / "security.py").unlink()
    (target / "security.py").symlink_to(COMPAT / "upstream" / "security.py")
    with pytest.raises(ValueError, match="Symlink"):
        utility.execute(root, manifest, patch, "check")


def test_patch_staged_change_refused(patched):
    root, _, manifest, utility = patched
    subprocess.run(["git", "-C", str(root), "add", "plugins/platforms/a2a"], check=True)
    with pytest.raises(ValueError, match="staged"):
        utility.execute(root, manifest, COMPAT / "hermes-a2a-compat.patch", "revert")
