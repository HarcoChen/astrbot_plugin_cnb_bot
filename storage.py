"""SQLite-backed report state and recovery metadata."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = {
    "WAITING_LOG",
    "PREPARING_LOG",
    "CREATING_ISSUE",
    "TRIGGERING_NPC",
    "WAITING_NPC",
    "DELIVERING",
    "AWAITING_RECOVERY",
    "CLOSING_ISSUE",
    "UNCERTAIN",
}

TERMINAL_STATUSES = {"DONE", "EXPIRED", "CANCELLED", "FAILED"}


class TaskStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS report_tasks (
                    id TEXT PRIMARY KEY,
                    active_key TEXT,
                    status TEXT NOT NULL,
                    platform_name TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    unified_msg_origin TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    deadline REAL NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_active_key "
                "ON report_tasks(active_key) WHERE active_key IS NOT NULL"
            )

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        task = json.loads(row["payload"])
        task.update(
            {
                "id": row["id"],
                "active_key": row["active_key"],
                "status": row["status"],
                "platform_name": row["platform_name"],
                "bot_id": row["bot_id"],
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "unified_msg_origin": row["unified_msg_origin"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "deadline": row["deadline"],
            }
        )
        return task

    def create_waiting(self, task: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        now = time.time()
        key = str(task["active_key"])
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT * FROM report_tasks WHERE active_key = ?", (key,)
                ).fetchone()
                if existing:
                    self._db.commit()
                    return False, self._decode(existing)
                payload = {
                    key: value
                    for key, value in task.items()
                    if key
                    not in {
                        "id",
                        "active_key",
                        "status",
                        "platform_name",
                        "bot_id",
                        "group_id",
                        "user_id",
                        "unified_msg_origin",
                        "created_at",
                        "updated_at",
                        "deadline",
                    }
                }
                self._db.execute(
                    """
                    INSERT INTO report_tasks
                    (id, active_key, status, platform_name, bot_id, group_id, user_id,
                     unified_msg_origin, created_at, updated_at, deadline, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["id"],
                        key,
                        "WAITING_LOG",
                        task["platform_name"],
                        task["bot_id"],
                        task["group_id"],
                        task["user_id"],
                        task["unified_msg_origin"],
                        now,
                        now,
                        float(task["deadline"]),
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return True, self.get(str(task["id"]))

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM report_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._decode(row)

    def find_waiting(
        self, platform_name: str, bot_id: str, group_id: str, user_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """
                SELECT * FROM report_tasks
                WHERE platform_name = ? AND bot_id = ? AND group_id = ? AND user_id = ?
                  AND status = 'WAITING_LOG'
                ORDER BY created_at DESC LIMIT 1
                """,
                (platform_name, bot_id, group_id, user_id),
            ).fetchone()
        return self._decode(row)

    def find_current(
        self, platform_name: str, bot_id: str, group_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Find this user's active report, or their latest completed report."""
        with self._lock:
            row = self._db.execute(
                """
                SELECT * FROM report_tasks
                WHERE platform_name = ? AND bot_id = ? AND group_id = ? AND user_id = ?
                ORDER BY (active_key IS NOT NULL) DESC, created_at DESC, rowid DESC
                LIMIT 1
                """,
                (platform_name, bot_id, group_id, user_id),
            ).fetchone()
        return self._decode(row)

    def claim_waiting_log(self, task_id: str, user_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE report_tasks SET status = 'PREPARING_LOG', updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'WAITING_LOG'
                """,
                (time.time(), task_id, user_id),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        fields: dict[str, Any] | None = None,
        release_active: bool = False,
        expected_statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM report_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                task = self._decode(row) or {}
                if expected_statuses is not None and task["status"] not in expected_statuses:
                    self._db.commit()
                    return task
                payload = {
                    key: value
                    for key, value in task.items()
                    if key
                    not in {
                        "id",
                        "active_key",
                        "status",
                        "platform_name",
                        "bot_id",
                        "group_id",
                        "user_id",
                        "unified_msg_origin",
                        "created_at",
                        "updated_at",
                        "deadline",
                    }
                }
                if fields:
                    payload.update(fields)
                new_status = status or task["status"]
                active_key = None if release_active else task["active_key"]
                self._db.execute(
                    """
                    UPDATE report_tasks
                    SET active_key = ?, status = ?, updated_at = ?, payload = ?
                    WHERE id = ?
                    """,
                    (
                        active_key,
                        new_status,
                        now,
                        json.dumps(payload, ensure_ascii=False),
                        task_id,
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.get(task_id)

    def list_statuses(self, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = self._db.execute(
                    f"SELECT * FROM report_tasks WHERE status IN ({placeholders}) ORDER BY created_at",
                    tuple(statuses),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM report_tasks ORDER BY created_at"
                ).fetchall()
        return [self._decode(row) for row in rows if row is not None]

    def prune_terminal(self, older_than: float) -> int:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._lock:
            cursor = self._db.execute(
                f"DELETE FROM report_tasks WHERE status IN ({placeholders}) "
                "AND active_key IS NULL AND updated_at < ?",
                (*TERMINAL_STATUSES, float(older_than)),
            )
            self._db.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
