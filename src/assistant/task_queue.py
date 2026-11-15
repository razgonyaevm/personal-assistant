from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from assistant.tools import ToolRegistry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskQueue:
    def __init__(self, db_path: str | Path = "data/assistant.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks
                (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT    NOT NULL DEFAULT 'main',
                    channel     TEXT    NOT NULL DEFAULT 'cli',
                    kind        TEXT    NOT NULL DEFAULT 'background',
                    prompt      TEXT    NOT NULL,
                    status      TEXT    NOT NULL DEFAULT 'pending',
                    result      TEXT    NOT NULL DEFAULT '',
                    error       TEXT    NOT NULL DEFAULT '',
                    notified_tg INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT    NOT NULL,
                    started_at  TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS notifications
                (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    source        TEXT    NOT NULL,
                    text          TEXT    NOT NULL,
                    delivered_cli INTEGER NOT NULL DEFAULT 0,
                    delivered_tg  INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT    NOT NULL
                );
                """
            )

    def enqueue(
            self,
            prompt: str,
            session_id: str = "main",
            channel: str = "cli",
            kind: str = "background",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks(session_id, channel, kind, prompt, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (session_id, channel, kind, prompt, _utc_now()),
            )
            return int(cur.lastrowid)

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE status = 'pending'
                ORDER BY CASE WHEN kind = 'chat' THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                conn.commit()
                return None

            conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?",
                (_utc_now(), row["id"]),
            )
            conn.commit()
            return dict(row)

    def complete(self, task_id: int, result: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'done', result = ?, finished_at = ? WHERE id = ?",
                (result, _utc_now(), task_id),
            )

    def fail(self, task_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (error, _utc_now(), task_id),
            )

    def get(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_recent(self, limit: int = 15) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def push_notification(self, source: str, text: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notifications(source, text, created_at) VALUES (?, ?, ?)",
                (source, text, _utc_now()),
            )
            return int(cur.lastrowid)

    def pop_notifications_for(self, channel: str) -> list[dict[str, Any]]:
        col = "delivered_tg" if channel == "telegram" else "delivered_cli"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            rows = conn.execute(
                f"SELECT * FROM notifications WHERE {col} = 0 ORDER BY id ASC"
            ).fetchall()
            if rows:
                ids = [(r["id"],) for r in rows]
                conn.executemany(f"UPDATE notifications SET {col} = 1 WHERE id = ?", ids)
            conn.commit()
            return [dict(r) for r in rows]


def register_queue_tools(
        registry: ToolRegistry,
        queue: TaskQueue,
        default_session: str = "main",
) -> None:
    registry.register(
        name="enqueue_task",
        description="Ставит отложенную/фоновую задачу в общую очередь ассистента.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Формулировка задачи."},
                "session_id": {"type": "string", "description": "ID сессии (по умолчанию 'main')."},
            },
            "required": ["prompt"],
        },
        handler=lambda prompt, session_id=default_session: {
            "task_id": queue.enqueue(
                prompt=prompt, session_id=session_id, channel="agent", kind="background"
            ),
            "status": "pending",
        },
    )

    registry.register(
        name="list_tasks",
        description="Показывает последние задачи из общей очереди задач.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Количество задач (по умолчанию 10)."}
            },
            "required": [],
        },
        handler=lambda limit=10: queue.list_recent(limit=limit),
    )
