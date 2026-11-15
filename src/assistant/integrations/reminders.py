from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any

from assistant.tools import ToolRegistry


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_now().tzinfo)
    return dt


class ReminderManager:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders
                (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    text             TEXT    NOT NULL,
                    schedule_type    TEXT    NOT NULL DEFAULT 'once',
                    next_run_at      TEXT    NOT NULL,
                    interval_minutes INTEGER,
                    daily_time       TEXT,
                    active           INTEGER NOT NULL DEFAULT 1,
                    created_at       TEXT    NOT NULL
                );
                """
            )

    def create_reminder(
            self,
            text: str,
            schedule_type: str = "once",
            run_at: str | None = None,
            delay_minutes: int | None = None,
            interval_minutes: int | None = None,
            daily_time: str | None = None,
    ) -> dict[str, Any]:
        now = _local_now()

        if schedule_type == "once":
            if run_at:
                next_dt = _parse_dt(run_at)
            elif delay_minutes is not None:
                next_dt = now + timedelta(minutes=int(delay_minutes))
            else:
                raise ValueError("Для разового напоминания укажите run_at (ISO) или delay_minutes.")
        elif schedule_type == "interval":
            if not interval_minutes or int(interval_minutes) < 1:
                raise ValueError("Для interval укажите interval_minutes >= 1.")
            next_dt = _parse_dt(run_at) if run_at else (now + timedelta(minutes=int(interval_minutes)))
        elif schedule_type == "daily":
            if not daily_time or ":" not in daily_time:
                raise ValueError("Для daily укажите daily_time в формате 'HH:MM'.")
            hh, mm = map(int, daily_time.split(":", 1))
            next_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if next_dt <= now:
                next_dt += timedelta(days=1)
        else:
            raise ValueError("schedule_type должен быть 'once', 'interval' или 'daily'.")

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO reminders(text, schedule_type, next_run_at, interval_minutes, daily_time, active,
                                      created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    text,
                    schedule_type,
                    next_dt.isoformat(),
                    interval_minutes,
                    daily_time,
                    now.isoformat(),
                ),
            )
            rem_id = int(cur.lastrowid)

        return {
            "status": "scheduled",
            "id": rem_id,
            "text": text,
            "schedule_type": schedule_type,
            "next_run_at": next_dt.isoformat(),
        }

    def list_reminders(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if include_inactive:
                rows = conn.execute("SELECT * FROM reminders ORDER BY next_run_at ASC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE active = 1 ORDER BY next_run_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def cancel_reminder(self, reminder_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE reminders SET active = 0 WHERE id = ?",
                (reminder_id,),
            )
            if cur.rowcount == 0:
                return {"status": "not_found", "id": reminder_id}
        return {"status": "cancelled", "id": reminder_id}

    def pop_due_reminders(self) -> list[dict[str, Any]]:
        now = _local_now()
        due: list[dict[str, Any]] = []

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            rows = conn.execute(
                "SELECT * FROM reminders WHERE active = 1 ORDER BY next_run_at ASC"
            ).fetchall()

            for row in rows:
                next_dt = _parse_dt(row["next_run_at"])
                if next_dt > now:
                    continue

                due.append(dict(row))
                stype = row["schedule_type"]
                if stype == "once":
                    conn.execute("UPDATE reminders SET active = 0 WHERE id = ?", (row["id"],))
                elif stype == "interval":
                    mins = int(row["interval_minutes"] or 60)
                    new_next = now + timedelta(minutes=mins)
                    conn.execute(
                        "UPDATE reminders SET next_run_at = ? WHERE id = ?",
                        (new_next.isoformat(), row["id"]),
                    )
                elif stype == "daily":
                    new_next = next_dt + timedelta(days=1)
                    while new_next <= now:
                        new_next += timedelta(days=1)
                    conn.execute(
                        "UPDATE reminders SET next_run_at = ? WHERE id = ?",
                        (new_next.isoformat(), row["id"]),
                    )
            conn.commit()

        return due


def register_reminder_tools(registry: ToolRegistry, rem: ReminderManager) -> None:
    registry.register(
        name="reminder_create",
        description=(
            "Создаёт напоминание: разовое ('once' по run_at или delay_minutes), "
            "повторяющееся ('interval' каждые N минут) или ежедневное ('daily' в daily_time 'HH:MM')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст напоминания."},
                "schedule_type": {
                    "type": "string",
                    "enum": ["once", "interval", "daily"],
                },
                "run_at": {"type": "string", "description": "Время в ISO (опционально)."},
                "delay_minutes": {"type": "integer", "description": "Через сколько минут (для once)."},
                "interval_minutes": {"type": "integer", "description": "Интервал в минутах (для interval)."},
                "daily_time": {"type": "string", "description": "Время 'HH:MM' (для daily)."},
            },
            "required": ["text", "schedule_type"],
        },
        handler=rem.create_reminder,
    )

    registry.register(
        name="reminder_list",
        description="Показывает активные запланированные напоминания.",
        parameters={
            "type": "object",
            "properties": {
                "include_inactive": {"type": "boolean"}
            },
            "required": [],
        },
        handler=rem.list_reminders,
    )

    registry.register(
        name="reminder_cancel",
        description="Отменяет напоминание по ID.",
        parameters={
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
        handler=rem.cancel_reminder,
    )
