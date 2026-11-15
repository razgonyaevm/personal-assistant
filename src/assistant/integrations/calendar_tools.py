from __future__ import annotations

from datetime import date, datetime, time, timedelta
import os
from pathlib import Path
import sqlite3
from typing import Any
from dotenv import load_dotenv
import httpx
import icalendar
import recurring_ical_events

from assistant.tools import ToolRegistry

load_dotenv()


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt


def _to_aware_dt(val: Any) -> datetime:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=_local_tz())
        return val.astimezone(_local_tz())
    if isinstance(val, date):
        return datetime.combine(val, time.min, tzinfo=_local_tz())
    raise TypeError(f"Unsupported date type: {type(val)}")


class CalendarManager:
    def __init__(
            self,
            db_path: str | Path = "data/assistant.db",
            default_external_ics: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_external_ics = (
            default_external_ics
            if default_external_ics is not None
            else os.getenv("EXTERNAL_ICS_URL", "")
        )
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
                CREATE TABLE IF NOT EXISTS personal_events
                (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    title    TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at   TEXT,
                    location TEXT NOT NULL DEFAULT '',
                    notes    TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def add_personal_event(
            self,
            title: str,
            start_at: str,
            end_at: str | None = None,
            location: str = "",
            notes: str = "",
    ) -> dict[str, Any]:
        start_dt = _parse_dt(start_at)
        end_dt = _parse_dt(end_at) if end_at else (start_dt + timedelta(hours=1))

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO personal_events(title, start_at, end_at, location, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title, start_dt.isoformat(), end_dt.isoformat(), location, notes),
            )
            event_id = int(cur.lastrowid)

        return {
            "status": "created",
            "id": event_id,
            "title": title,
            "start_at": start_dt.isoformat(),
            "end_at": end_dt.isoformat(),
        }

    def list_personal_events(
            self,
            start_date: str | None = None,
            end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now().astimezone()
        start_dt = (
            _parse_dt(start_date)
            if start_date
            else now.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        end_dt = _parse_dt(end_date) if end_date else (start_dt + timedelta(days=7))

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM personal_events ORDER BY start_at ASC"
            ).fetchall()

        result: list[dict[str, Any]] = []
        for r in rows:
            ev_start = _parse_dt(r["start_at"])
            if start_dt <= ev_start <= end_dt:
                result.append(dict(r))
        return result

    def delete_personal_event(self, event_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM personal_events WHERE id = ?", (event_id,))
            if cur.rowcount == 0:
                return {"status": "not_found", "id": event_id}
        return {"status": "deleted", "id": event_id}

    def read_external_events(
            self,
            source: str | None = None,
            days_ahead: int = 7,
    ) -> list[dict[str, Any]]:
        ics_source = (source or self.default_external_ics).strip()
        if not ics_source:
            raise ValueError(
                "Не указан источник .ics (передайте source или задайте EXTERNAL_ICS_URL в .env)."
            )

        if ics_source.startswith(("http://", "https://", "webcal://")):
            url = ics_source.replace("webcal://", "https://", 1)
            with httpx.Client(follow_redirects=True, timeout=15.0) as client:
                resp = client.get(url)
                resp.raise_for_status()
                raw_ics = resp.content
        else:
            raw_ics = Path(ics_source).read_bytes()

        cal = icalendar.Calendar.from_ical(raw_ics)
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=max(1, min(60, int(days_ahead))))

        events = recurring_ical_events.of(cal).between(start, end)
        parsed: list[dict[str, Any]] = []
        for ev in events:
            dtstart = _to_aware_dt(ev.get("DTSTART").dt)
            dtend_prop = ev.get("DTEND")
            dtend = _to_aware_dt(dtend_prop.dt) if dtend_prop else None
            parsed.append(
                {
                    "summary": str(ev.get("SUMMARY", "")),
                    "start": dtstart.isoformat(),
                    "end": dtend.isoformat() if dtend else None,
                    "location": str(ev.get("LOCATION", "")),
                    "description": str(ev.get("DESCRIPTION", ""))[:300],
                }
            )

        parsed.sort(key=lambda x: x["start"])
        return parsed


def register_calendar_tools(registry: ToolRegistry, cal: CalendarManager) -> None:
    registry.register(
        name="calendar_add_event",
        description="Добавляет событие в личный календарь (даты в ISO, например '2026-11-16T15:00:00').",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название события."},
                "start_at": {"type": "string", "description": "Время начала в ISO."},
                "end_at": {"type": "string", "description": "Время окончания в ISO (опционально)."},
                "location": {"type": "string", "description": "Место или ссылка (опционально)."},
                "notes": {"type": "string", "description": "Заметки (опционально)."},
            },
            "required": ["title", "start_at"],
        },
        handler=cal.add_personal_event,
    )

    registry.register(
        name="calendar_list_events",
        description="Показывает события из личного календаря (по умолчанию на ближайшие 7 дней).",
        parameters={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Начало в ISO (опционально)."},
                "end_date": {"type": "string", "description": "Конец в ISO (опционально)."},
            },
            "required": [],
        },
        handler=cal.list_personal_events,
    )

    registry.register(
        name="calendar_delete_event",
        description="Удаляет событие из личного календаря по ID.",
        parameters={
            "type": "object",
            "properties": {"event_id": {"type": "integer"}},
            "required": ["event_id"],
        },
        handler=cal.delete_personal_event,
    )

    registry.register(
        name="calendar_external_events",
        description=(
            "Читает события из внешнего календаря .ics (URL или локальный файл — "
            "например расписание ВУЗа или матчей)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "URL или путь к .ics (опционально)."},
                "days_ahead": {"type": "integer", "description": "На сколько дней вперёд (по умолчанию 7)."},
            },
            "required": [],
        },
        handler=cal.read_external_events,
    )
