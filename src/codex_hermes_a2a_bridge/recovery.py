from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import A2ATaskResult, TaskRecord, TaskState


class ConversationRecovery:
    """Read Hermes' documented local A2A conversation persistence safely."""

    def __init__(self, directory: Path, *, max_bytes: int = 16 * 1024 * 1024):
        self.directory = Path(directory)
        self.max_bytes = max_bytes

    @staticmethod
    def _safe_name(context_id: str) -> str:
        return "".join(char for char in (context_id or "default") if char.isalnum() or char in "-_") or "default"

    @staticmethod
    def _created_epoch(task: TaskRecord) -> float:
        return datetime.fromisoformat(task.created_at.replace("Z", "+00:00")).timestamp()

    def recover(
        self,
        task: TaskRecord,
        *,
        assigned_task_ids: set[str],
        unresolved_count: int,
    ) -> A2ATaskResult | None:
        """Return one unambiguous late agent record; never infer among peers."""
        if unresolved_count != 1:
            return None
        path = self.directory / f"{self._safe_name(task.context_id)}.jsonl"
        try:
            if not path.is_file() or path.stat().st_size > self.max_bytes:
                return None
            records: list[dict[str, object]] = []
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            return None

        created = self._created_epoch(task)
        candidates: dict[str, str] = {}
        user_ids: set[str] = set()
        for record in records:
            task_id = str(record.get("task_id") or "")
            if not task_id or task_id in assigned_task_ids:
                continue
            raw_timestamp = record.get("ts")
            try:
                timestamp = float(raw_timestamp) if isinstance(raw_timestamp, (int, float, str)) else 0.0
            except (TypeError, ValueError):
                continue
            if timestamp < created - 5:
                continue
            role = str(record.get("role") or "")
            if role == "user":
                user_ids.add(task_id)
            elif role == "agent" and isinstance(record.get("text"), str):
                candidates[task_id] = str(record["text"])

        matched = [(task_id, text) for task_id, text in candidates.items() if task_id in user_ids]
        if len(matched) != 1:
            return None
        task_id, text = matched[0]
        return A2ATaskResult(
            task_id=task_id,
            context_id=task.context_id,
            state=TaskState.COMPLETED.value,
            text=text,
        )
