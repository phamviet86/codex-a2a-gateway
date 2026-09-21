"""PostgreSQL ledger. Every public method owns one bounded transaction.

Payloads, results, artifacts and replay snapshots are encrypted; UUIDs, timestamps,
request digests and lifecycle states are intentionally queryable. No SQL contains
payload literals. A per-device row lock orders events by commit, not sequence
allocation time. The dispatcher holds a session advisory lock for its lifetime.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row

from .broker_settings import BrokerSettings

FINAL = frozenset({"completed", "failed", "canceled"})
TERMINAL = FINAL | {"outcome_unknown"}


class BrokerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_v06_devices (
 device_id text PRIMARY KEY, sequence bigint NOT NULL DEFAULT 0, floor bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS broker_v06_operations (
 device_id text NOT NULL REFERENCES broker_v06_devices(device_id), operation_id uuid NOT NULL,
 flow_id uuid NOT NULL, digest text NOT NULL, state text NOT NULL,
 result_id uuid, result_cipher bytea, result_expires_at timestamptz,
 error_code text, error_message text, remote_task_id text, polled_at timestamptz,
 created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
 cancel_claimed boolean NOT NULL DEFAULT false, cancel_requested boolean NOT NULL DEFAULT false,
 PRIMARY KEY(device_id, operation_id)
);
CREATE TABLE IF NOT EXISTS broker_v06_payloads (
 device_id text NOT NULL, operation_id uuid NOT NULL, cipher bytea NOT NULL, expires_at timestamptz NOT NULL,
 PRIMARY KEY(device_id, operation_id),
 FOREIGN KEY(device_id, operation_id) REFERENCES broker_v06_operations(device_id, operation_id)
);
CREATE TABLE IF NOT EXISTS broker_v06_outbox (
 device_id text NOT NULL, operation_id uuid NOT NULL, state text NOT NULL DEFAULT 'pending',
 claimed_at timestamptz,
 PRIMARY KEY(device_id, operation_id),
 FOREIGN KEY(device_id, operation_id) REFERENCES broker_v06_operations(device_id, operation_id)
);
CREATE TABLE IF NOT EXISTS broker_v06_events (
 device_id text NOT NULL REFERENCES broker_v06_devices(device_id), sequence bigint NOT NULL,
 cipher bytea NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY(device_id, sequence)
);
CREATE TABLE IF NOT EXISTS broker_v06_artifacts (
 device_id text NOT NULL REFERENCES broker_v06_devices(device_id), artifact_id uuid NOT NULL,
 sha256 text NOT NULL, size bigint NOT NULL, content_type text NOT NULL,
 cipher bytea NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY(device_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS broker_v06_receipts (
 device_id text NOT NULL, operation_id uuid NOT NULL, result_id uuid NOT NULL, status text NOT NULL,
 created_at timestamptz NOT NULL, PRIMARY KEY(device_id, operation_id, result_id, status),
 FOREIGN KEY(device_id, operation_id) REFERENCES broker_v06_operations(device_id, operation_id)
);
CREATE INDEX IF NOT EXISTS broker_v06_flow ON broker_v06_operations(device_id, flow_id, created_at);
CREATE INDEX IF NOT EXISTS broker_v06_artifact_expiry ON broker_v06_artifacts(expires_at);
CREATE INDEX IF NOT EXISTS broker_v06_event_expiry ON broker_v06_events(expires_at);
"""


class BrokerStore:
    def __init__(self, settings: BrokerSettings):
        self.settings = settings
        self._cipher = Fernet(settings.encryption_key.encode())
        self._mutex = threading.RLock()
        self.conn = psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row, client_encoding="UTF8"
        )
        self._dispatcher = False
        with self.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(608060600)")
            conn.execute(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self._mutex, self.conn.transaction():
            yield self.conn

    def close(self) -> None:
        with self._mutex:
            self.conn.close()

    def acquire_dispatcher(self) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(current_database() || current_schema(), 606)) AS ok"
            ).fetchone()
            if not row or not row["ok"]:
                raise BrokerError("dispatcher_active", "another broker dispatcher owns this ledger", 503)
            self._dispatcher = True

    def _device(self, conn: psycopg.Connection[dict[str, Any]], device: str) -> None:
        conn.execute("INSERT INTO broker_v06_devices(device_id) VALUES (%s) ON CONFLICT DO NOTHING", (device,))
        conn.execute("SELECT device_id FROM broker_v06_devices WHERE device_id=%s FOR UPDATE", (device,))

    def _encrypt(self, value: object) -> bytes:
        return self._cipher.encrypt(canonical(value))

    def _decrypt(self, value: bytes) -> Any:
        try:
            return json.loads(self._cipher.decrypt(bytes(value)))
        except (InvalidToken, ValueError):
            raise BrokerError("encryption_error", "stored content cannot be decrypted", 503) from None

    def _row(self, conn: psycopg.Connection[dict[str, Any]], device: str, operation: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM broker_v06_operations WHERE device_id=%s AND operation_id=%s", (device, operation)
        ).fetchone()
        if not row:
            raise BrokerError("not_found", "operation not found", 404)
        return row

    def _snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        result = None
        error = {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] else None
        if row["result_cipher"] is not None and row["result_expires_at"] > utcnow():
            result = self._decrypt(row["result_cipher"])
            if error is not None and "_gateway_error_details" in result:
                error["details"] = result["_gateway_error_details"]
                result = None
        elif row["result_expires_at"] is not None and row["result_expires_at"] <= utcnow():
            error = {"code": "result_expired", "message": "result retention period has expired"}
        return {
            "operation_id": str(row["operation_id"]),
            "flow_id": str(row["flow_id"]),
            "state": row["state"],
            "result_id": str(row["result_id"]) if row["result_id"] else None,
            "result": result,
            "error": error,
            "remote_task_id": row["remote_task_id"],
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
        }

    def _event(self, conn: psycopg.Connection[dict[str, Any]], device: str, operation: str) -> dict[str, Any]:
        snapshot = self._snapshot(self._row(conn, device, operation))
        row = conn.execute(
            "UPDATE broker_v06_devices SET sequence=sequence+1 WHERE device_id=%s RETURNING sequence", (device,)
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO broker_v06_events VALUES (%s,%s,%s,%s)",
            (
                device,
                row["sequence"],
                self._encrypt(snapshot),
                utcnow() + timedelta(seconds=min(self.settings.event_ttl_seconds, self.settings.result_ttl_seconds)),
            ),
        )
        return snapshot

    def accept(self, device: str, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        digest = hashlib.sha256(canonical(request)).hexdigest()
        operation = request["operation_id"]
        with self.transaction() as conn:
            self._device(conn, device)
            previous = conn.execute(
                "SELECT * FROM broker_v06_operations WHERE device_id=%s AND operation_id=%s", (device, operation)
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise BrokerError("id_conflict", "operation ID already has a different request", 409)
                return self._snapshot(previous), False
            count = conn.execute(
                "SELECT count(*) AS n FROM broker_v06_operations WHERE device_id=%s "
                "AND state IN ('accepted','running','outcome_unknown')",
                (device,),
            ).fetchone()
            assert count is not None
            if count["n"] >= self.settings.max_pending_per_device:
                raise BrokerError("admission_full", "device pending operation limit reached", 429)
            if len(set(request["artifact_ids"])) != len(request["artifact_ids"]):
                raise BrokerError("invalid_artifacts", "duplicate input artifact IDs are not allowed")
            total_bytes = len(request["prompt"].encode("utf-8"))
            for artifact_id in request["artifact_ids"]:
                descriptor, content = self._artifact(conn, device, artifact_id)
                if descriptor["content_type"].split(";", 1)[0].strip().lower() != "text/plain":
                    raise BrokerError(
                        "unsupported_artifact", "Hermes operation inputs support UTF-8 text/plain only", 422
                    )
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    raise BrokerError(
                        "unsupported_artifact", "input text artifact must contain valid UTF-8", 422
                    ) from None
                total_bytes += len(content)
            if total_bytes > self.settings.max_artifact_bytes:
                raise BrokerError(
                    "artifacts_too_large", "combined prompt and input artifacts exceed configured size limit", 413
                )
            now = utcnow()
            conn.execute(
                "INSERT INTO broker_v06_operations "
                "(device_id,operation_id,flow_id,digest,state,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'accepted',%s,%s)",
                (device, operation, request["flow_id"], digest, now, now),
            )
            conn.execute(
                "INSERT INTO broker_v06_payloads VALUES (%s,%s,%s,%s)",
                (
                    device,
                    operation,
                    self._encrypt(request),
                    now + timedelta(seconds=self.settings.payload_ttl_seconds),
                ),
            )
            conn.execute("INSERT INTO broker_v06_outbox(device_id,operation_id) VALUES (%s,%s)", (device, operation))
            return self._event(conn, device, operation), True

    def get(self, device: str, operation: str) -> dict[str, Any]:
        with self.transaction() as conn:
            return self._snapshot(self._row(conn, device, operation))

    def claim(self) -> dict[str, Any] | None:
        if not self._dispatcher:
            raise BrokerError("dispatcher_required", "dispatcher lock is required", 503)
        with self.transaction() as conn:
            # One dispatcher means no competing claim. Lock device first everywhere
            # to avoid device/operation lock inversions with HTTP acceptance/cancel.
            candidate = conn.execute(
                "SELECT o.device_id,o.operation_id FROM broker_v06_operations o "
                "JOIN broker_v06_outbox b USING(device_id,operation_id) "
                "WHERE b.state='pending' AND o.state='accepted' AND NOT EXISTS ("
                "SELECT 1 FROM broker_v06_operations active WHERE active.device_id=o.device_id "
                "AND active.flow_id=o.flow_id AND active.state IN ('running','outcome_unknown')) "
                "ORDER BY o.created_at,o.operation_id LIMIT 1"
            ).fetchone()
            if not candidate:
                return None
            device, operation = candidate["device_id"], str(candidate["operation_id"])
            self._device(conn, device)
            row = self._row(conn, device, operation)
            if row["state"] != "accepted":
                return None
            payload = conn.execute(
                "SELECT cipher,expires_at FROM broker_v06_payloads WHERE device_id=%s AND operation_id=%s",
                (device, operation),
            ).fetchone()
            if not payload or payload["expires_at"] <= utcnow():
                self._finish(conn, device, operation, "failed", None, "payload_expired", "dispatch payload expired")
                return None
            request = self._decrypt(payload["cipher"])
            # Resolve uploaded bytes before marking dispatch; expired attachments never
            # become an ambiguous upstream mutation. They are never fetched by URL.
            try:
                attachments = [self._artifact(conn, device, a) for a in request["artifact_ids"]]
            except BrokerError:
                self._finish(conn, device, operation, "failed", None, "artifact_expired", "input artifact unavailable")
                return None
            conn.execute(
                "UPDATE broker_v06_outbox SET state='claimed',claimed_at=%s WHERE device_id=%s AND operation_id=%s",
                (utcnow(), device, operation),
            )
            conn.execute(
                "UPDATE broker_v06_operations SET state='running',updated_at=%s WHERE device_id=%s AND operation_id=%s",
                (utcnow(), device, operation),
            )
            self._event(conn, device, operation)
            return {"device_id": device, **request, "attachments": attachments}

    def _finish(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        device: str,
        operation: str,
        state: str,
        result: dict[str, Any] | None,
        code: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        row = self._row(conn, device, operation)
        if row["state"] in FINAL:
            return self._snapshot(row)
        result_cipher = self._encrypt(result) if result is not None else None
        expires = utcnow() + timedelta(seconds=self.settings.result_ttl_seconds) if result is not None else None
        conn.execute(
            "UPDATE broker_v06_operations SET state=%s,result_id=COALESCE(result_id,%s),result_cipher=%s,"
            "result_expires_at=%s,error_code=%s,error_message=%s,updated_at=%s WHERE device_id=%s AND operation_id=%s",
            (state, uuid.uuid4(), result_cipher, expires, code, message, utcnow(), device, operation),
        )
        conn.execute(
            "UPDATE broker_v06_outbox SET state='done' WHERE device_id=%s AND operation_id=%s", (device, operation)
        )
        conn.execute("DELETE FROM broker_v06_payloads WHERE device_id=%s AND operation_id=%s", (device, operation))
        return self._event(conn, device, operation)

    def finish(
        self,
        device: str,
        operation: str,
        state: str,
        result: dict[str, Any] | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        if state not in TERMINAL:
            raise ValueError("invalid terminal state")
        if result is not None and len(canonical(result)) > self.settings.max_result_bytes:
            state, result, code, message = "failed", None, "result_too_large", "peer result exceeds configured limit"
        with self.transaction() as conn:
            self._device(conn, device)
            return self._finish(conn, device, operation, state, result, code, message)

    def save_handle(self, device: str, operation: str, handle: str) -> None:
        if not handle or len(handle) > 512:
            raise BrokerError("invalid_peer_handle", "peer returned an invalid task handle", 502)
        with self.transaction() as conn:
            self._device(conn, device)
            row = self._row(conn, device, operation)
            if row["remote_task_id"] and row["remote_task_id"] != handle:
                raise BrokerError("peer_handle_conflict", "peer changed task handle", 502)
            if row["remote_task_id"] == handle or row["state"] in FINAL:
                return
            conn.execute(
                "UPDATE broker_v06_operations SET remote_task_id=%s,updated_at=%s "
                "WHERE device_id=%s AND operation_id=%s",
                (handle, utcnow(), device, operation),
            )
            self._event(conn, device, operation)

    def recover(self) -> None:
        """Called after exclusive dispatcher acquisition, before any new claims."""
        if not self._dispatcher:
            raise BrokerError("dispatcher_required", "dispatcher lock is required", 503)
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT device_id,operation_id FROM broker_v06_operations WHERE state='running'"
            ).fetchall()
            for row in rows:
                self._device(conn, row["device_id"])
                self._finish(
                    conn,
                    row["device_id"],
                    str(row["operation_id"]),
                    "outcome_unknown",
                    None,
                    "restart_unknown",
                    "dispatcher restarted after dispatch was claimed; no resend",
                )

    def reconcilable(self) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT device_id,operation_id,remote_task_id FROM broker_v06_operations "
                "WHERE state IN ('running','outcome_unknown') AND remote_task_id IS NOT NULL "
                "ORDER BY polled_at NULLS FIRST,updated_at LIMIT 20"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE broker_v06_operations SET polled_at=%s WHERE device_id=%s AND operation_id=%s",
                    (utcnow(), row["device_id"], row["operation_id"]),
                )
            return rows

    def cancel_intent(self, device: str, operation: str) -> tuple[dict[str, Any], str | None]:
        with self.transaction() as conn:
            self._device(conn, device)
            row = self._row(conn, device, operation)
            if row["state"] == "accepted":
                return self._finish(conn, device, operation, "canceled", None), None
            if row["state"] in FINAL or row["cancel_requested"]:
                return self._snapshot(row), None
            conn.execute(
                "UPDATE broker_v06_operations SET cancel_requested=true,error_code='cancel_unconfirmed',"
                "error_message='cancellation is best effort; underlying execution may continue',updated_at=%s "
                "WHERE device_id=%s AND operation_id=%s",
                (utcnow(), device, operation),
            )
            return self._event(conn, device, operation), row["remote_task_id"]

    def claim_cancel(self) -> tuple[str, str, str] | None:
        if not self._dispatcher:
            raise BrokerError("dispatcher_required", "dispatcher lock is required", 503)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT device_id,operation_id FROM broker_v06_operations WHERE cancel_requested "
                "AND NOT cancel_claimed "
                "AND remote_task_id IS NOT NULL AND state IN ('running','outcome_unknown') ORDER BY updated_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            device, operation = row["device_id"], str(row["operation_id"])
            self._device(conn, device)
            current = self._row(conn, device, operation)
            if current["state"] in FINAL:
                return None
            conn.execute(
                "UPDATE broker_v06_operations SET cancel_claimed=true WHERE device_id=%s AND operation_id=%s",
                (device, operation),
            )
            return device, operation, current["remote_task_id"]

    def events(self, device: str, after: int, limit: int = 100) -> list[tuple[int, dict[str, Any]]]:
        with self.transaction() as conn:
            self._device(conn, device)
            self._purge_events(conn, device)
            row = conn.execute("SELECT sequence,floor FROM broker_v06_devices WHERE device_id=%s", (device,)).fetchone()
            assert row is not None
            if after < row["floor"]:
                raise BrokerError(
                    "retention_gap",
                    "event cursor expired; recover known operations with GET",
                    410,
                    {"resume_after": row["sequence"]},
                )
            if after > row["sequence"]:
                raise BrokerError("invalid_cursor", "event cursor is ahead of this device", 409)
            return [
                (r["sequence"], self._decrypt(r["cipher"]))
                for r in conn.execute(
                    "SELECT sequence,cipher FROM broker_v06_events WHERE device_id=%s AND sequence>%s "
                    "ORDER BY sequence LIMIT %s",
                    (device, after, limit),
                ).fetchall()
            ]

    def receipt(self, device: str, operation: str, result_id: str, status: str) -> None:
        if status not in {"stored", "delivered", "delivery_outcome_unknown"}:
            raise BrokerError("invalid_receipt", "invalid receipt status")
        with self.transaction() as conn:
            self._device(conn, device)
            row = self._row(conn, device, operation)
            if not row["result_id"] or str(row["result_id"]) != result_id:
                raise BrokerError("result_conflict", "receipt does not match the operation result", 409)
            conn.execute(
                "INSERT INTO broker_v06_receipts VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (device, operation, result_id, status, utcnow()),
            )

    @staticmethod
    def _descriptor(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": str(row["artifact_id"]),
            "sha256": row["sha256"],
            "size": row["size"],
            "content_type": row["content_type"],
            "expires_at": iso(row["expires_at"]),
        }

    def put_artifact(self, device: str, content: bytes, digest: str, content_type: str) -> dict[str, Any]:
        if len(content) > self.settings.max_artifact_bytes:
            raise BrokerError("artifact_too_large", "artifact exceeds configured size limit", 413)
        if hashlib.sha256(content).hexdigest() != digest:
            raise BrokerError("digest_mismatch", "artifact digest does not match bytes", 422)
        # Content types are metadata, never executable response types on download.
        if len(content_type) > 128 or any(ord(c) < 32 or ord(c) > 126 for c in content_type):
            raise BrokerError("invalid_content_type", "invalid artifact content type")
        with self.transaction() as conn:
            self._device(conn, device)
            conn.execute("DELETE FROM broker_v06_artifacts WHERE device_id=%s AND expires_at<=%s", (device, utcnow()))
            row = conn.execute(
                "SELECT COALESCE(sum(size),0) AS n FROM broker_v06_artifacts WHERE device_id=%s", (device,)
            ).fetchone()
            assert row is not None
            if row["n"] + len(content) > self.settings.device_quota_bytes:
                raise BrokerError("artifact_quota", "device artifact quota exceeded", 429)
            row = conn.execute(
                "INSERT INTO broker_v06_artifacts VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    device,
                    uuid.uuid4(),
                    digest,
                    len(content),
                    content_type,
                    self._cipher.encrypt(content),
                    utcnow() + timedelta(seconds=self.settings.artifact_ttl_seconds),
                ),
            ).fetchone()
            assert row is not None
            return self._descriptor(row)

    def _artifact(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        device: str,
        artifact: str,
    ) -> tuple[dict[str, Any], bytes]:
        row = conn.execute(
            "SELECT * FROM broker_v06_artifacts WHERE device_id=%s AND artifact_id=%s AND expires_at>%s",
            (device, artifact, utcnow()),
        ).fetchone()
        if not row:
            raise BrokerError("not_found", "artifact not found or expired", 404)
        try:
            content = self._cipher.decrypt(bytes(row["cipher"]))
        except InvalidToken:
            raise BrokerError("encryption_error", "stored artifact cannot be decrypted", 503) from None
        if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise BrokerError("artifact_corrupt", "stored artifact integrity check failed", 503)
        return self._descriptor(row), content

    def artifact(self, device: str, artifact: str) -> tuple[dict[str, Any], bytes]:
        with self.transaction() as conn:
            return self._artifact(conn, device, artifact)

    def _purge_events(self, conn: psycopg.Connection[dict[str, Any]], device: str) -> None:
        # Purge a prefix so a clock adjustment cannot leave an undetectable hole.
        row = conn.execute(
            "SELECT max(sequence) AS n FROM broker_v06_events WHERE device_id=%s AND expires_at<=%s",
            (device, utcnow()),
        ).fetchone()
        if row and row["n"] is not None:
            conn.execute("DELETE FROM broker_v06_events WHERE device_id=%s AND sequence<=%s", (device, row["n"]))
            conn.execute(
                "UPDATE broker_v06_devices SET floor=GREATEST(floor,%s) WHERE device_id=%s", (row["n"], device)
            )

    def purge(self) -> None:
        with self.transaction() as conn:
            # Follow the device-first locking order, also used by event readers.
            for row in conn.execute("SELECT device_id FROM broker_v06_devices ORDER BY device_id").fetchall():
                self._device(conn, row["device_id"])
                self._purge_events(conn, row["device_id"])
            now = utcnow()
            conn.execute("DELETE FROM broker_v06_payloads WHERE expires_at<=%s", (now,))
            conn.execute("DELETE FROM broker_v06_artifacts WHERE expires_at<=%s", (now,))
            conn.execute("UPDATE broker_v06_operations SET result_cipher=NULL WHERE result_expires_at<=%s", (now,))
