from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    BridgeError,
    ContextRecord,
    TaskRecord,
    now_iso,
    result_receipt,
)

SCHEMA_VERSION = 5


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._migrate()
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def _migrate(self) -> None:
        with self._lock, self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contexts (
                    context_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'default',
                    endpoint TEXT NOT NULL,
                    tenant TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('open','closed')),
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    last_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_context_open_conversation
                ON contexts(conversation_key, profile) WHERE status='open';

                CREATE TABLE IF NOT EXISTS tasks (
                    bridge_task_id TEXT PRIMARY KEY,
                    a2a_task_id TEXT UNIQUE,
                    context_id TEXT NOT NULL REFERENCES contexts(context_id),
                    conversation_key TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'default',
                    endpoint TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_fingerprint TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_text TEXT NOT NULL DEFAULT '',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT,
                    error_message TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    hop_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    execution_metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_idempotency
                ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_task_conversation_created
                ON tasks(conversation_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_state_created
                ON tasks(state, created_at DESC);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_task_id TEXT NOT NULL REFERENCES tasks(bridge_task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    state TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(bridge_task_id, id);

                CREATE TABLE IF NOT EXISTS inbound_messages (
                    message_id TEXT PRIMARY KEY,
                    bridge_task_id TEXT NOT NULL REFERENCES tasks(bridge_task_id) ON DELETE CASCADE,
                    context_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_messages_task
                ON inbound_messages(bridge_task_id, created_at);
                """
            )
            context_columns = {row[1] for row in con.execute("PRAGMA table_info(contexts)")}
            if "direction" not in context_columns:
                con.execute("ALTER TABLE contexts ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound'")
            if "codex_thread_id" not in context_columns:
                con.execute("ALTER TABLE contexts ADD COLUMN codex_thread_id TEXT")
            if "backend" not in context_columns:
                con.execute("ALTER TABLE contexts ADD COLUMN backend TEXT")
            task_columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            if "direction" not in task_columns:
                con.execute("ALTER TABLE tasks ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound'")
            if "codex_turn_id" not in task_columns:
                con.execute("ALTER TABLE tasks ADD COLUMN codex_turn_id TEXT")
            if "execution_metadata_json" not in task_columns:
                con.execute("ALTER TABLE tasks ADD COLUMN execution_metadata_json TEXT NOT NULL DEFAULT '{}'")
            if "attempt_number" not in task_columns:
                con.execute("ALTER TABLE tasks ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1")
            if "origin_json" not in task_columns:
                con.execute("ALTER TABLE tasks ADD COLUMN origin_json TEXT NOT NULL DEFAULT '{}'")
            con.execute(
                "CREATE TABLE IF NOT EXISTS outbound_attempts (idempotency_key TEXT PRIMARY KEY, "
                "bridge_task_id TEXT NOT NULL REFERENCES tasks(bridge_task_id), fingerprint TEXT NOT NULL, "
                "message_id TEXT NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS result_receipts (result_id TEXT PRIMARY KEY, "
                "bridge_task_id TEXT NOT NULL REFERENCES tasks(bridge_task_id), acknowledged_at TEXT NOT NULL)"
            )
            con.execute(
                "INSERT OR IGNORE INTO inbound_messages("
                "message_id,bridge_task_id,context_id,request_fingerprint,created_at) "
                "SELECT message_id,bridge_task_id,context_id,request_fingerprint,created_at FROM tasks "
                "WHERE direction='inbound'"
            )
            con.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _context(row: sqlite3.Row | None) -> ContextRecord | None:
        return ContextRecord.model_validate(dict(row)) if row else None

    @staticmethod
    def _task(row: sqlite3.Row | None) -> TaskRecord | None:
        if not row:
            return None
        data = dict(row)
        data["origin"] = json.loads(data.pop("origin_json") or "{}")
        data["artifacts"] = json.loads(data.pop("artifacts_json") or "[]")
        data["execution_metadata"] = json.loads(data.pop("execution_metadata_json") or "{}")
        data["cancel_requested"] = bool(data["cancel_requested"])
        return TaskRecord.model_validate(data)

    def get_context(
        self, *, context_id: str | None = None, conversation_key: str | None = None
    ) -> ContextRecord | None:
        if not context_id and not conversation_key:
            return None
        with self._lock, self._connect() as con:
            if context_id:
                row = con.execute("SELECT * FROM contexts WHERE context_id=?", (context_id,)).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM contexts WHERE conversation_key=? AND profile='default' AND status='open' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (conversation_key,),
                ).fetchone()
        return self._context(row)

    def get_or_create_context(
        self,
        *,
        conversation_key: str,
        endpoint: str,
        context_id: str | None = None,
        profile: str = "default",
        tenant: str = "",
        direction: str = "outbound",
        backend: str | None = None,
    ) -> ContextRecord:
        now = now_iso()
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM contexts WHERE conversation_key=? AND profile=? AND status='open'",
                (conversation_key, profile),
            ).fetchone()
            if existing:
                rec = self._context(existing)
                assert rec is not None
                if context_id and rec.context_id != context_id:
                    raise BridgeError("context_conflict", "conversation_key is already bound to another open context")
                if rec.endpoint != endpoint or rec.tenant != tenant:
                    raise BridgeError("routing_conflict", "existing context is bound to a different Hermes route")
                return rec
            chosen_id = context_id or f"codex-{uuid.uuid4().hex}"
            row = con.execute("SELECT * FROM contexts WHERE context_id=?", (chosen_id,)).fetchone()
            if row:
                rec = self._context(row)
                assert rec is not None
                if rec.status != "open" or rec.conversation_key != conversation_key:
                    raise BridgeError("context_conflict", "context_id belongs to another or closed mapping")
                return rec
            con.execute(
                "INSERT INTO contexts("
                "context_id,conversation_key,profile,endpoint,tenant,status,turn_count,created_at,updated_at,"
                "direction,backend"
                ") "
                "VALUES(?,?,?,?,?,'open',0,?,?,?,?)",
                (chosen_id, conversation_key, profile, endpoint, tenant, now, now, direction, backend),
            )
            row = con.execute("SELECT * FROM contexts WHERE context_id=?", (chosen_id,)).fetchone()
        rec = self._context(row)
        assert rec is not None
        return rec

    def increment_turn(self, context_id: str, max_turns: int) -> ContextRecord:
        now = now_iso()
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM contexts WHERE context_id=?", (context_id,)).fetchone()
            rec = self._context(row)
            if not rec or rec.status != "open":
                raise BridgeError("context_not_open", "context is missing or closed")
            if rec.turn_count >= max_turns:
                raise BridgeError(
                    "turn_budget_exceeded",
                    f"context reached the v0.1 turn budget ({max_turns}); close it and start a new conversation",
                )
            con.execute(
                "UPDATE contexts SET turn_count=turn_count+1,updated_at=? WHERE context_id=?",
                (now, context_id),
            )
            row = con.execute("SELECT * FROM contexts WHERE context_id=?", (context_id,)).fetchone()
        out = self._context(row)
        assert out is not None
        return out

    def set_context_last_task(self, context_id: str, bridge_task_id: str) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE contexts SET last_task_id=?,updated_at=? WHERE context_id=?",
                (bridge_task_id, now_iso(), context_id),
            )

    def set_codex_thread(self, context_id: str, thread_id: str, backend: str) -> ContextRecord:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE contexts SET codex_thread_id=?,backend=?,updated_at=? WHERE context_id=?",
                (thread_id, backend, now_iso(), context_id),
            )
        row = self.get_context(context_id=context_id)
        if not row:
            raise BridgeError("context_not_found", "context mapping not found")
        return row

    def list_contexts(self, limit: int = 20, *, conversation_key: str | None = None) -> list[ContextRecord]:
        limit = max(1, min(limit, 100))
        sql = "SELECT * FROM contexts"
        params: list[Any] = []
        if conversation_key:
            sql += " WHERE conversation_key=?"
            params.append(conversation_key)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._context(row) for row in rows if row is not None]  # type: ignore[misc]

    def close_context(self, *, context_id: str | None = None, conversation_key: str | None = None) -> ContextRecord:
        rec = self.get_context(context_id=context_id, conversation_key=conversation_key)
        if not rec:
            raise BridgeError("context_not_found", "context mapping not found")
        if rec.status == "closed":
            return rec
        now = now_iso()
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE contexts SET status='closed',closed_at=?,updated_at=? WHERE context_id=?",
                (now, now, rec.context_id),
            )
        out = self.get_context(context_id=rec.context_id)
        assert out is not None
        return out

    def create_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock, self._connect() as con:
            try:
                con.execute(
                    """
                    INSERT INTO tasks(
                        bridge_task_id,a2a_task_id,context_id,conversation_key,profile,endpoint,
                        request_id,message_id,idempotency_key,request_fingerprint,mode,state,
                        result_text,artifacts_json,error_code,error_message,cancel_requested,hop_count,
                        created_at,updated_at,completed_at,direction,codex_turn_id,execution_metadata_json,origin_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task.bridge_task_id,
                        task.a2a_task_id,
                        task.context_id,
                        task.conversation_key,
                        task.profile,
                        task.endpoint,
                        task.request_id,
                        task.message_id,
                        task.idempotency_key,
                        task.request_fingerprint,
                        task.mode,
                        task.state,
                        task.result_text,
                        json.dumps(task.artifacts, ensure_ascii=False),
                        task.error_code,
                        task.error_message,
                        int(task.cancel_requested),
                        task.hop_count,
                        task.created_at,
                        task.updated_at,
                        task.completed_at,
                        task.direction,
                        task.codex_turn_id,
                        json.dumps(task.execution_metadata, ensure_ascii=False),
                        json.dumps(task.origin, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("task_conflict", "task or idempotency key already exists") from exc
        self.add_event(task.bridge_task_id, "created", task.state)
        self.set_context_last_task(task.context_id, task.bridge_task_id)
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM tasks WHERE bridge_task_id=? OR a2a_task_id=? LIMIT 1",
                (task_id, task_id),
            ).fetchone()
        return self._task(row)

    def get_task_by_idempotency(self, key: str) -> TaskRecord | None:
        with self._lock, self._connect() as con:
            attempt = con.execute("SELECT * FROM outbound_attempts WHERE idempotency_key=?", (key,)).fetchone()
            if attempt:
                task = self._task(
                    con.execute("SELECT * FROM tasks WHERE bridge_task_id=?", (attempt["bridge_task_id"],)).fetchone()
                )
                return task.model_copy(update={"request_fingerprint": attempt["fingerprint"]}) if task else None
            row = con.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
        return self._task(row)

    def acknowledge_result(self, task: TaskRecord, result_id: str) -> None:
        if result_receipt(task) != result_id:
            raise BridgeError("result_mismatch", "receipt does not identify the current result for this task")
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO result_receipts VALUES(?,?,?)", (result_id, task.bridge_task_id, now_iso())
            )

    def result_acknowledged(self, result_id: str | None) -> bool:
        with self._lock, self._connect() as con:
            return con.execute("SELECT 1 FROM result_receipts WHERE result_id=?", (result_id,)).fetchone() is not None

    def mark_interrupted_outbound(self) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE tasks SET state='outcome_unknown',error_code='bridge_restarted',updated_at=? "
                "WHERE direction='outbound' AND state IN ('queued','submitted','working')",
                (now_iso(),),
            )

    def active_context_task(self, context_id: str) -> TaskRecord | None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM tasks WHERE context_id=? AND state IN "
                "('queued','submitted','working','outcome_unknown') LIMIT 1",
                (context_id,),
            ).fetchone()
        return self._task(row)

    def continue_outbound(self, task_id: str, attempt: TaskRecord) -> TaskRecord:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = self._task(con.execute("SELECT * FROM tasks WHERE bridge_task_id=?", (task_id,)).fetchone())
            if not current or current.state != "input_required":
                raise BridgeError("invalid_task_state", "continuation requires input-required")
            for key, fingerprint, message_id in (
                (current.idempotency_key, current.request_fingerprint, current.message_id),
                (attempt.idempotency_key, attempt.request_fingerprint, attempt.message_id),
            ):
                if key:
                    con.execute(
                        "INSERT OR IGNORE INTO outbound_attempts VALUES(?,?,?,?)",
                        (key, task_id, fingerprint, message_id),
                    )
            con.execute(
                "UPDATE tasks SET state='queued',message_id=?,request_fingerprint=?,"
                "attempt_number=attempt_number+1,result_text='',"
                "artifacts_json='[]',error_code=NULL,error_message=NULL,updated_at=? WHERE bridge_task_id=?",
                (attempt.message_id, attempt.request_fingerprint, now_iso(), task_id),
            )
        result = self.get_task(task_id)
        assert result is not None
        return result

    def get_inbound_message(self, message_id: str) -> dict[str, str] | None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT message_id,bridge_task_id,context_id,request_fingerprint,created_at "
                "FROM inbound_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_inbound_message(
        self,
        *,
        message_id: str,
        bridge_task_id: str,
        context_id: str,
        request_fingerprint: str,
    ) -> None:
        with self._lock, self._connect() as con:
            try:
                con.execute(
                    "INSERT INTO inbound_messages(message_id,bridge_task_id,context_id,request_fingerprint,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (message_id, bridge_task_id, context_id, request_fingerprint, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("idempotency_conflict", "messageId is already registered") from exc

    @staticmethod
    def _increment_turn_in_transaction(con: sqlite3.Connection, context_id: str, max_turns: int, now: str) -> None:
        row = con.execute(
            "SELECT status,turn_count FROM contexts WHERE context_id=?",
            (context_id,),
        ).fetchone()
        if not row or row["status"] != "open":
            raise BridgeError("context_not_open", "context is missing or closed")
        if int(row["turn_count"]) >= max_turns:
            raise BridgeError(
                "turn_budget_exceeded",
                f"context reached the v0.1 turn budget ({max_turns}); close it and start a new conversation",
            )
        con.execute(
            "UPDATE contexts SET turn_count=turn_count+1,updated_at=? WHERE context_id=?",
            (now, context_id),
        )

    def create_inbound_task_atomic(self, task: TaskRecord, *, max_turns: int) -> TaskRecord:
        now = now_iso()
        with self._lock, self._connect() as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                self._increment_turn_in_transaction(con, task.context_id, max_turns, now)
                con.execute(
                    """
                    INSERT INTO tasks(
                        bridge_task_id,a2a_task_id,context_id,conversation_key,profile,endpoint,
                        request_id,message_id,idempotency_key,request_fingerprint,mode,state,
                        result_text,artifacts_json,error_code,error_message,cancel_requested,hop_count,
                        created_at,updated_at,completed_at,direction,codex_turn_id,execution_metadata_json,origin_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task.bridge_task_id,
                        task.a2a_task_id,
                        task.context_id,
                        task.conversation_key,
                        task.profile,
                        task.endpoint,
                        task.request_id,
                        task.message_id,
                        task.idempotency_key,
                        task.request_fingerprint,
                        task.mode,
                        task.state,
                        task.result_text,
                        json.dumps(task.artifacts, ensure_ascii=False),
                        task.error_code,
                        task.error_message,
                        int(task.cancel_requested),
                        task.hop_count,
                        task.created_at,
                        task.updated_at,
                        task.completed_at,
                        task.direction,
                        task.codex_turn_id,
                        json.dumps(task.execution_metadata, ensure_ascii=False),
                        json.dumps(task.origin, ensure_ascii=False),
                    ),
                )
                con.execute(
                    "INSERT INTO inbound_messages("
                    "message_id,bridge_task_id,context_id,request_fingerprint,created_at) VALUES(?,?,?,?,?)",
                    (task.message_id, task.bridge_task_id, task.context_id, task.request_fingerprint, now),
                )
                con.execute(
                    "INSERT INTO events(bridge_task_id,event_type,state,message,created_at) VALUES(?,?,?,?,?)",
                    (task.bridge_task_id, "created", task.state, "", now),
                )
                con.execute(
                    "UPDATE contexts SET last_task_id=?,updated_at=? WHERE context_id=?",
                    (task.bridge_task_id, now, task.context_id),
                )
                row = con.execute("SELECT * FROM tasks WHERE bridge_task_id=?", (task.bridge_task_id,)).fetchone()
            except sqlite3.IntegrityError as exc:
                raise BridgeError("task_conflict", "task, messageId, or idempotency key already exists") from exc
        created = self._task(row)
        assert created is not None
        return created

    def continue_inbound_task_atomic(
        self,
        *,
        bridge_task_id: str,
        context_id: str,
        message_id: str,
        request_fingerprint: str,
        max_turns: int,
    ) -> TaskRecord:
        now = now_iso()
        with self._lock, self._connect() as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT * FROM tasks WHERE bridge_task_id=? AND direction='inbound'",
                    (bridge_task_id,),
                ).fetchone()
                current = self._task(row)
                if not current:
                    raise BridgeError("task_not_found", "inbound taskId was not found")
                if current.context_id != context_id:
                    raise BridgeError("task_context_mismatch", "message taskId and contextId refer to different tasks")
                if current.state != "input_required":
                    raise BridgeError("invalid_task_state", "task is no longer waiting for input")
                self._increment_turn_in_transaction(con, context_id, max_turns, now)
                con.execute(
                    "INSERT INTO inbound_messages("
                    "message_id,bridge_task_id,context_id,request_fingerprint,created_at) VALUES(?,?,?,?,?)",
                    (message_id, bridge_task_id, context_id, request_fingerprint, now),
                )
                con.execute(
                    "UPDATE tasks SET state='queued',result_text='',error_code=NULL,error_message=NULL,"
                    "cancel_requested=0,updated_at=?,completed_at=NULL,codex_turn_id=NULL,message_id=? "
                    "WHERE bridge_task_id=?",
                    (now, message_id, bridge_task_id),
                )
                con.execute(
                    "INSERT INTO events(bridge_task_id,event_type,state,message,created_at) VALUES(?,?,?,?,?)",
                    (bridge_task_id, "state", "queued", "", now),
                )
                row = con.execute("SELECT * FROM tasks WHERE bridge_task_id=?", (bridge_task_id,)).fetchone()
            except sqlite3.IntegrityError as exc:
                raise BridgeError("idempotency_conflict", "messageId is already registered") from exc
        continued = self._task(row)
        assert continued is not None
        return continued

    def count_active_inbound_tasks(self) -> int:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE direction='inbound' "
                "AND state IN ('queued','submitted','working','outcome_unknown')"
            ).fetchone()
        return int(row[0])

    def requeue_inbound_after_restart(self, bridge_task_id: str) -> TaskRecord:
        now = now_iso()
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM tasks WHERE bridge_task_id=?", (bridge_task_id,)).fetchone()
            current = self._task(row)
            if (
                not current
                or current.direction != "inbound"
                or current.state != "failed"
                or current.error_code != "gateway_restarted_before_start"
            ):
                if not current:
                    raise BridgeError("task_not_found", "inbound task not found")
                return current
            con.execute(
                "UPDATE tasks SET state='queued',error_code=NULL,error_message=NULL,cancel_requested=0,"
                "updated_at=?,completed_at=NULL WHERE bridge_task_id=?",
                (now, bridge_task_id),
            )
            con.execute(
                "INSERT INTO events(bridge_task_id,event_type,state,message,created_at) VALUES(?,?,?,?,?)",
                (bridge_task_id, "replayed_after_restart", "queued", "", now),
            )
            row = con.execute("SELECT * FROM tasks WHERE bridge_task_id=?", (bridge_task_id,)).fetchone()
        requeued = self._task(row)
        assert requeued is not None
        return requeued

    def update_task(
        self,
        bridge_task_id: str,
        *,
        state: str | None = None,
        a2a_task_id: str | None = None,
        result_text: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        cancel_requested: bool | None = None,
        codex_turn_id: str | None = None,
        execution_metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        clear_codex_turn_id: bool = False,
    ) -> TaskRecord:
        current = self.get_task(bridge_task_id)
        if not current:
            raise BridgeError("task_not_found", "bridge task not found")
        if current.state in TERMINAL_STATES:
            return current
        target_state = state or current.state
        if state and state != current.state:
            allowed = ALLOWED_TRANSITIONS.get(current.state, set())
            if state not in allowed:
                raise BridgeError("invalid_state_transition", f"cannot transition {current.state} -> {state}")
        now = now_iso()
        completed_at = current.completed_at
        if current.state == "input_required" and target_state not in TERMINAL_STATES:
            completed_at = None
        if target_state in TERMINAL_STATES and not completed_at:
            completed_at = now
        values = {
            "state": target_state,
            "a2a_task_id": a2a_task_id if a2a_task_id is not None else current.a2a_task_id,
            "result_text": result_text if result_text is not None else current.result_text,
            "artifacts_json": json.dumps(artifacts if artifacts is not None else current.artifacts, ensure_ascii=False),
            "error_code": error_code if error_code is not None else current.error_code,
            "error_message": error_message if error_message is not None else current.error_message,
            "cancel_requested": int(cancel_requested if cancel_requested is not None else current.cancel_requested),
            "updated_at": now,
            "completed_at": completed_at,
            "codex_turn_id": (
                None if clear_codex_turn_id else codex_turn_id if codex_turn_id is not None else current.codex_turn_id
            ),
            "message_id": message_id if message_id is not None else current.message_id,
            "execution_metadata_json": json.dumps(
                execution_metadata if execution_metadata is not None else current.execution_metadata,
                ensure_ascii=False,
            ),
        }
        with self._lock, self._connect() as con:
            con.execute(
                """
                UPDATE tasks SET state=:state,a2a_task_id=:a2a_task_id,result_text=:result_text,
                    artifacts_json=:artifacts_json,error_code=:error_code,error_message=:error_message,
                    cancel_requested=:cancel_requested,updated_at=:updated_at,completed_at=:completed_at,
                    codex_turn_id=:codex_turn_id,message_id=:message_id,
                    execution_metadata_json=:execution_metadata_json
                WHERE bridge_task_id=:bridge_task_id
                """,
                {**values, "bridge_task_id": current.bridge_task_id},
            )
        if state and state != current.state:
            self.add_event(current.bridge_task_id, "state", state, result_text or error_message or "")
        out = self.get_task(current.bridge_task_id)
        assert out is not None
        return out

    def append_task_result(self, bridge_task_id: str, text: str) -> TaskRecord:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE tasks SET result_text=result_text || ?,updated_at=? WHERE bridge_task_id=? "
                "AND state NOT IN ('completed','failed','canceled','rejected')",
                (text, now_iso(), bridge_task_id),
            )
        task = self.get_task(bridge_task_id)
        if not task:
            raise BridgeError("task_not_found", "bridge task not found")
        return task

    def fail_active_inbound_tasks(self, *, error_code: str, error_message: str) -> int:
        now = now_iso()
        active = ("submitted", "working", "outcome_unknown")
        placeholders = ",".join("?" for _ in active)
        with self._lock, self._connect() as con:
            queued = con.execute(
                "UPDATE tasks SET state='failed',error_code='gateway_restarted_before_start',"
                "error_message=?,updated_at=?,completed_at=? "
                "WHERE direction='inbound' AND state='queued'",
                (error_message, now, now),
            ).rowcount
            active_cursor = con.execute(
                f"UPDATE tasks SET state='outcome_unknown',error_code=?,error_message=?,updated_at=?,completed_at=NULL "
                f"WHERE direction='inbound' AND state IN ({placeholders})",
                (error_code, error_message, now, *active),
            ).rowcount
        return queued + active_cursor

    def list_tasks_page(
        self,
        *,
        context_id: str | None,
        states: tuple[str, ...] | None,
        updated_after: str | None,
        cursor: tuple[str, str] | None,
        page_size: int,
    ) -> tuple[list[TaskRecord], int, bool]:
        clauses = ["direction='inbound'"]
        params: list[Any] = []
        if context_id:
            clauses.append("context_id=?")
            params.append(context_id)
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            params.extend(states)
        if updated_after:
            clauses.append("updated_at>=?")
            params.append(updated_after)
        base_where = " AND ".join(clauses)
        with self._lock, self._connect() as con:
            total = int(con.execute(f"SELECT COUNT(*) FROM tasks WHERE {base_where}", params).fetchone()[0])
            page_clauses = list(clauses)
            page_params = list(params)
            if cursor:
                page_clauses.append("(updated_at < ? OR (updated_at = ? AND bridge_task_id < ?))")
                page_params.extend((cursor[0], cursor[0], cursor[1]))
            rows = con.execute(
                "SELECT * FROM tasks WHERE "
                + " AND ".join(page_clauses)
                + " ORDER BY updated_at DESC,bridge_task_id DESC LIMIT ?",
                (*page_params, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        tasks = [self._task(row) for row in rows[:page_size]]
        return [task for task in tasks if task is not None], total, has_more

    def list_tasks(
        self,
        *,
        conversation_key: str | None = None,
        context_id: str | None = None,
        state: str | None = None,
        direction: str | None = None,
        limit: int = 20,
    ) -> list[TaskRecord]:
        limit = max(1, min(limit, 100))
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_key:
            clauses.append("conversation_key=?")
            params.append(conversation_key)
        if context_id:
            clauses.append("context_id=?")
            params.append(context_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        if direction:
            clauses.append("direction=?")
            params.append(direction)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._task(row) for row in rows if row is not None]  # type: ignore[misc]

    def add_event(self, bridge_task_id: str, event_type: str, state: str | None = None, message: str = "") -> None:
        message = (message or "")[:500]
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO events(bridge_task_id,event_type,state,message,created_at) VALUES(?,?,?,?,?)",
                (bridge_task_id, event_type, state, message, now_iso()),
            )
            con.execute(
                "DELETE FROM events WHERE bridge_task_id=? AND id NOT IN "
                "(SELECT id FROM events WHERE bridge_task_id=? ORDER BY id DESC LIMIT 100)",
                (bridge_task_id, bridge_task_id),
            )

    def list_events(self, bridge_task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT event_type,state,message,created_at FROM events WHERE bridge_task_id=? "
                "ORDER BY id DESC LIMIT ?",
                (bridge_task_id, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def counts(self) -> dict[str, int]:
        with self._lock, self._connect() as con:
            contexts = con.execute("SELECT COUNT(*) FROM contexts").fetchone()[0]
            tasks = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            active = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('queued','submitted','working','outcome_unknown')"
            ).fetchone()[0]
        return {"contexts": contexts, "tasks": tasks, "active_tasks": active}
