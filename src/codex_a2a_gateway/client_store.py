"""Single-writer client ledger. Sensitive submit outbox payloads are encrypted."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet

from .client_settings import ClientSettings
from .native_delivery import NativeOrigin, native_uuid

TERMINAL = frozenset({"completed", "failed", "canceled", "outcome_unknown"})
FINAL = TERMINAL - {"outcome_unknown"}
STATES = TERMINAL | {"accepted", "running"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("client state directory must be owned by this user and not a symlink")
    path.chmod(0o700)


class ClientStore:
    def __init__(self, settings: ClientSettings) -> None:
        self.settings = settings
        private_directory(settings.state_dir)
        self.lock_fd = os.open(settings.state_dir / "writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.lock_fd)
            raise RuntimeError("another client daemon owns this state directory") from exc
        self.cipher = Fernet(settings.payload_key.encode("ascii"))
        path = settings.state_dir / "client.sqlite3"
        if path.is_symlink():
            self.close_lock()
            raise ValueError("client database may not be a symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS client_flows(thread_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS client_operations(
              operation_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, thread_id TEXT NOT NULL,
              turn_id TEXT NOT NULL, call_id TEXT NOT NULL, digest TEXT NOT NULL, payload BLOB,
              payload_expires REAL NOT NULL, snapshot TEXT, submission_state TEXT NOT NULL,
              delivery_state TEXT NOT NULL, delivery_id TEXT, queue_id TEXT, inline_deadline REAL NOT NULL,
              cancel_state TEXT, conflict INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
              updated REAL NOT NULL, expires REAL NOT NULL, expired INTEGER NOT NULL DEFAULT 0,
              UNIQUE(thread_id, turn_id, call_id));
            CREATE TABLE IF NOT EXISTS client_cursor(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), seq INTEGER NOT NULL);
            INSERT OR IGNORE INTO client_cursor VALUES(1,0);
            CREATE TABLE IF NOT EXISTS client_receipts(
              operation_id TEXT NOT NULL, result_id TEXT NOT NULL, status TEXT NOT NULL,
              sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(operation_id,result_id,status));
        """)
        with self.db:
            self.db.execute(
                "UPDATE client_operations SET delivery_state='delivery_outcome_unknown' "
                "WHERE delivery_state='enqueueing'"
            )
            self.db.execute(
                "UPDATE client_operations SET inline_deadline=0,delivery_state='ready' "
                "WHERE delivery_state='inline_pending'"
            )
            self.db.execute("UPDATE client_operations SET cancel_state='outcome_unknown' WHERE cancel_state='sending'")
            self.db.execute(
                "INSERT OR IGNORE INTO client_receipts "
                "SELECT operation_id,json_extract(snapshot,'$.result_id'),'delivery_outcome_unknown',0 "
                "FROM client_operations WHERE delivery_state='delivery_outcome_unknown' "
                "AND json_extract(snapshot,'$.result_id') IS NOT NULL"
            )
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                sidecar.chmod(0o600)

    def close_lock(self) -> None:
        fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        os.close(self.lock_fd)

    def close(self) -> None:
        self.db.close()
        self.close_lock()

    def row(self, operation_id: str, origin: NativeOrigin | None = None) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM client_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or (origin is not None and row["thread_id"] != origin.thread_id):
            raise KeyError("operation is not bound to this native thread")
        return dict(row)

    def resolve_call(self, origin: NativeOrigin) -> dict[str, Any]:
        """Recover a lost local submit ACK by exact host call identity, never inference."""
        row = self.db.execute(
            "SELECT operation_id FROM client_operations WHERE thread_id=? AND turn_id=? AND call_id=?",
            (origin.thread_id, origin.turn_id, origin.call_id),
        ).fetchone()
        if row is None:
            raise KeyError("no durable operation for this exact native tool call")
        return self.view(row["operation_id"], origin)

    def create(self, origin: NativeOrigin, prompt: str, artifact_ids: list[str], wait: float) -> str:
        if not prompt.strip() or len(prompt.encode()) > 256 * 1024:
            raise ValueError("prompt must contain 1..262144 UTF-8 bytes")
        if len(artifact_ids) > 32:
            raise ValueError("at most 32 artifacts are permitted")
        for artifact in artifact_ids:
            native_uuid(artifact, "artifact_id")
        digest = hashlib.sha256(canonical({"prompt": prompt, "artifact_ids": artifact_ids}).encode()).hexdigest()
        existing = self.db.execute(
            "SELECT operation_id,digest FROM client_operations WHERE thread_id=? AND turn_id=? AND call_id=?",
            (origin.thread_id, origin.turn_id, origin.call_id),
        ).fetchone()
        if existing:
            if existing["digest"] != digest:
                raise ValueError("native tool call identity was reused with a different command")
            return str(existing["operation_id"])
        count = self.db.execute(
            "SELECT count(*) FROM client_operations WHERE submission_state IN ('pending','accepted') AND expired=0 "
            "AND (snapshot IS NULL OR json_extract(snapshot,'$.state') NOT IN ('completed','failed','canceled'))"
        ).fetchone()[0]
        if count >= self.settings.max_pending:
            raise ValueError("client pending operation limit reached")
        now = time.time()
        operation_id = str(uuid4())
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO client_flows VALUES(?,?)", (origin.thread_id, str(uuid4())))
            flow_id = self.db.execute(
                "SELECT flow_id FROM client_flows WHERE thread_id=?", (origin.thread_id,)
            ).fetchone()[0]
            payload = self.cipher.encrypt(
                canonical(
                    {
                        "operation_id": operation_id,
                        "flow_id": flow_id,
                        "prompt": prompt,
                        "artifact_ids": artifact_ids,
                    }
                ).encode()
            )
            self.db.execute(
                "INSERT INTO client_operations(operation_id,flow_id,thread_id,turn_id,call_id,digest,payload,"
                "payload_expires,submission_state,delivery_state,inline_deadline,created,updated,expires) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    flow_id,
                    origin.thread_id,
                    origin.turn_id,
                    origin.call_id,
                    digest,
                    payload,
                    now + self.settings.payload_ttl_seconds,
                    "pending",
                    "inline_pending",
                    now + wait,
                    now,
                    now,
                    now + self.settings.retention_seconds,
                ),
            )
        return operation_id

    def payload(self, operation_id: str) -> dict[str, Any] | None:
        row = self.row(operation_id)
        if row["payload"] is None or row["payload_expires"] <= time.time():
            return None
        return dict(json.loads(self.cipher.decrypt(row["payload"])))

    def ids(self, *, pending: bool = False) -> list[str]:
        query = "SELECT operation_id FROM client_operations WHERE expired=0"
        if pending:
            query += " AND submission_state='pending'"
        return [row[0] for row in self.db.execute(query)]

    @property
    def cursor(self) -> int:
        return int(self.db.execute("SELECT seq FROM client_cursor WHERE singleton=1").fetchone()[0])

    def advance(self, seq: int) -> None:
        if type(seq) is not int or seq < 0:
            raise ValueError("invalid event cursor")
        with self.db:
            self.db.execute("UPDATE client_cursor SET seq=max(seq,?) WHERE singleton=1", (seq,))

    def accept_snapshot(self, snapshot: dict[str, Any], seq: int | None = None) -> None:
        operation_id = native_uuid(snapshot.get("operation_id"), "operation_id")
        flow_id = native_uuid(snapshot.get("flow_id"), "flow_id")
        if snapshot.get("state") not in STATES:
            raise ValueError("invalid broker operation state")
        required = {"result_id", "result", "error", "remote_task_id", "created_at", "updated_at"}
        if not required <= snapshot.keys():
            raise ValueError("incomplete broker snapshot")
        if snapshot["result_id"] is not None:
            native_uuid(snapshot["result_id"], "result_id")
        if snapshot["state"] in TERMINAL and snapshot["result_id"] is None:
            raise ValueError("terminal broker snapshot requires stable result_id")
        if seq is not None and (type(seq) is not int or seq < 0):
            raise ValueError("invalid event cursor")
        with self.db:
            row = self.db.execute("SELECT * FROM client_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row and not row["expired"]:
                if flow_id != row["flow_id"]:
                    raise ValueError("broker flow identity disagrees")
                previous = json.loads(row["snapshot"]) if row["snapshot"] else None
                stale = previous and previous["state"] in TERMINAL
                exact_resolution = (
                    previous is not None
                    and previous["state"] == "outcome_unknown"
                    and snapshot["state"] in FINAL
                    and bool(previous.get("remote_task_id"))
                    and previous["remote_task_id"] == snapshot["remote_task_id"]
                    and previous["result_id"] == snapshot["result_id"]
                )
                if (
                    previous is not None
                    and stale
                    and not exact_resolution
                    and snapshot["state"] in TERMINAL
                    and any(previous.get(key) != snapshot.get(key) for key in ("state", "result_id", "result", "error"))
                ):
                    self.db.execute("UPDATE client_operations SET conflict=1 WHERE operation_id=?", (operation_id,))
                elif not stale or exact_resolution:
                    self.db.execute(
                        "UPDATE client_operations SET snapshot=?,submission_state='accepted',payload=NULL,updated=?,"
                        "expires=? WHERE operation_id=?",
                        (canonical(snapshot), time.time(), time.time() + self.settings.retention_seconds, operation_id),
                    )
                    if snapshot["result_id"]:
                        self._receipt(operation_id, snapshot["result_id"], "stored")
            if seq is not None:
                self.db.execute("UPDATE client_cursor SET seq=max(seq,?) WHERE singleton=1", (seq,))

    def _receipt(self, operation_id: str, result_id: str, status: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO client_receipts VALUES(?,?,?,0)", (operation_id, result_id, status))

    def view(self, operation_id: str, origin: NativeOrigin, *, consume: bool = False) -> dict[str, Any]:
        row = self.row(operation_id, origin)
        snapshot = (
            json.loads(row["snapshot"])
            if row["snapshot"]
            else {
                "operation_id": operation_id,
                "flow_id": row["flow_id"],
                "state": "outcome_unknown",
                "result_id": None,
                "result": None,
                "error": None,
                "remote_task_id": None,
            }
        )
        if consume and snapshot.get("result_id") and snapshot["state"] in FINAL and not row["expired"]:
            with self.db:
                # An external enqueue already claimed cannot be undone by a simultaneous pull.
                if row["delivery_state"] in {"inline_pending", "ready", "unsupported"}:
                    self.db.execute(
                        "UPDATE client_operations SET delivery_state='inline_returned' WHERE operation_id=?",
                        (operation_id,),
                    )
                    row["delivery_state"] = "inline_returned"
                self._receipt(operation_id, snapshot["result_id"], "delivered")
        return {
            **snapshot,
            "submission_state": row["submission_state"],
            "delivery_state": row["delivery_state"],
            "queue_submission_id": row["queue_id"],
            "snapshot_conflict": bool(row["conflict"]),
            "result_expired": bool(row["expired"]),
            "cancel_state": row["cancel_state"],
            "delivery_note": "Queue acceptance and tool return do not prove native consumption. Use gateway_get/wait.",
        }

    def release_inline(self, operation_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE client_operations SET inline_deadline=0,delivery_state='ready' "
                "WHERE operation_id=? AND delivery_state='inline_pending'",
                (operation_id,),
            )

    def claim_delivery(self, operation_id: str) -> dict[str, Any] | None:
        row = self.row(operation_id)
        if row["expired"] or row["delivery_state"] != "ready":
            return None
        if row["inline_deadline"] > time.time() or not row["snapshot"]:
            return None
        snapshot = json.loads(row["snapshot"])
        if not snapshot.get("result_id") or snapshot["state"] not in FINAL:
            return None
        delivery_id = str(uuid4())
        with self.db:
            self.db.execute(
                "UPDATE client_operations SET delivery_state='enqueueing',delivery_id=? WHERE operation_id=?",
                (delivery_id, operation_id),
            )
        return {**row, "delivery_id": delivery_id, "snapshot": snapshot}

    def finish_delivery(self, operation_id: str, status: str, queue_id: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE client_operations SET delivery_state=?,queue_id=? WHERE operation_id=?",
                (status, queue_id, operation_id),
            )
            if status == "delivery_outcome_unknown":
                row = self.row(operation_id)
                snapshot = json.loads(row["snapshot"])
                self._receipt(operation_id, snapshot["result_id"], status)

    def mark_submission(self, operation_id: str, state: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE client_operations SET submission_state=? WHERE operation_id=?", (state, operation_id)
            )

    def cancel_state(self, operation_id: str, state: str) -> None:
        with self.db:
            self.db.execute("UPDATE client_operations SET cancel_state=? WHERE operation_id=?", (state, operation_id))

    def receipts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM client_receipts WHERE sent=0")]

    def receipt_sent(self, receipt: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "UPDATE client_receipts SET sent=1 WHERE operation_id=? AND result_id=? AND status=?",
                (receipt["operation_id"], receipt["result_id"], receipt["status"]),
            )

    def prune(self) -> None:
        now = time.time()
        with self.db:
            self.db.execute("UPDATE client_operations SET payload=NULL WHERE payload_expires<=?", (now,))
            rows = self.db.execute(
                "SELECT operation_id,snapshot FROM client_operations WHERE expires<=? AND expired=0", (now,)
            )
            for row in rows.fetchall():
                snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
                if snapshot is None or snapshot["state"] not in FINAL:
                    continue
                snapshot["result"] = None
                snapshot["error"] = None
                self.db.execute(
                    "UPDATE client_operations SET snapshot=?,expired=1,payload=NULL WHERE operation_id=?",
                    (canonical(snapshot), row["operation_id"]),
                )
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
