from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from assistant.tools import ToolRegistry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TodoManager:
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
                CREATE TABLE IF NOT EXISTS todos
                (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    title        TEXT NOT NULL,
                    notes        TEXT NOT NULL DEFAULT '',
                    category     TEXT NOT NULL DEFAULT 'Общее',
                    priority     TEXT NOT NULL DEFAULT 'medium',
                    due_date     TEXT,
                    status       TEXT NOT NULL DEFAULT 'todo',
                    created_at   TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (status);
                """
            )

    def add_todo(
            self,
            title: str,
            category: str = "Общее",
            priority: str = "medium",
            due_date: str | None = None,
            notes: str = "",
    ) -> dict[str, Any]:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Название задачи не может быть пустым.")

        prio = priority.lower().strip()
        if prio not in ("low", "medium", "high"):
            prio = "medium"

        clean_due = due_date.strip()[:16] if due_date and due_date.strip() else None

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO todos(title, notes, category, priority, due_date, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'todo', ?)
                """,
                (clean_title, notes.strip(), category.strip() or "Общее", prio, clean_due, _utc_now()),
            )
            todo_id = int(cur.lastrowid)

        return {
            "status": "created",
            "id": todo_id,
            "title": clean_title,
            "category": category.strip() or "Общее",
            "priority": prio,
            "due_date": clean_due,
        }

    def list_todos(
            self,
            status: str = "open",
            category: str | None = None,
            priority: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        st = (status or "open").lower().strip()
        if st == "open":
            clauses.append("status IN ('todo', 'in_progress')")
        elif st in ("todo", "in_progress", "done"):
            clauses.append("status = ?")
            params.append(st)

        if category and category.strip():
            clauses.append("LOWER(category) = LOWER(?)")
            params.append(category.strip())

        if priority and priority.strip():
            clauses.append("LOWER(priority) = LOWER(?)")
            params.append(priority.strip())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM todos
            {where}
            ORDER BY
                CASE WHEN status = 'done' THEN 1 ELSE 0 END ASC,
                CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END ASC,
                CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END ASC,
                due_date ASC,
                id DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def update_todo(
            self,
            todo_id: int,
            status: str | None = None,
            priority: str | None = None,
            title: str | None = None,
            due_date: str | None = None,
            category: str | None = None,
            notes: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
            if not row:
                return {"status": "not_found", "id": todo_id}

            new_status = row["status"]
            completed_at = row["completed_at"]
            if status is not None:
                st = status.lower().strip()
                if st not in ("todo", "in_progress", "done"):
                    raise ValueError("status должен быть 'todo', 'in_progress' или 'done'.")
                new_status = st
                completed_at = _utc_now() if st == "done" else None

            new_prio = row["priority"]
            if priority is not None and priority.lower().strip() in ("low", "medium", "high"):
                new_prio = priority.lower().strip()

            new_title = title.strip() if title is not None and title.strip() else row["title"]
            new_due = due_date.strip()[:16] if due_date is not None else row["due_date"]
            if new_due == "":
                new_due = None
            new_cat = category.strip() if category is not None and category.strip() else row["category"]
            new_notes = notes.strip() if notes is not None else row["notes"]

            conn.execute(
                """
                UPDATE todos
                SET title        = ?,
                    notes        = ?,
                    category     = ?,
                    priority     = ?,
                    due_date     = ?,
                    status       = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (new_title, new_notes, new_cat, new_prio, new_due, new_status, completed_at, todo_id),
            )
            updated = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
            return {"status": "updated", "todo": dict(updated)}

    def toggle_todo(self, todo_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM todos WHERE id = ?", (todo_id,)).fetchone()
            if not row:
                return {"status": "not_found", "id": todo_id}
            next_status = "todo" if row["status"] == "done" else "done"
        return self.update_todo(todo_id=todo_id, status=next_status)

    def delete_todo(self, todo_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            if cur.rowcount == 0:
                return {"status": "not_found", "id": todo_id}
        return {"status": "deleted", "id": todo_id}


def register_todo_tools(registry: ToolRegistry, todos: TodoManager) -> None:
    registry.register(
        name="todo_add",
        description=(
            "Добавляет задачу в личный To-Do список (с категорией, приоритетом и дедлайном). "
            "Если пользователь перечислил несколько дел сразу — вызови todo_add для каждого."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Короткая формулировка задачи."},
                "category": {
                    "type": "string",
                    "description": "Категория (например: Учёба, Т-Академия, Проект, Личное, Покупки).",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Приоритет задачи (по умолчанию medium).",
                },
                "due_date": {
                    "type": "string",
                    "description": "Дедлайн в формате YYYY-MM-DD или YYYY-MM-DD HH:MM (опционально).",
                },
                "notes": {"type": "string", "description": "Доп. детали или ссылка (опционально)."},
            },
            "required": ["title"],
        },
        handler=todos.add_todo,
    )

    registry.register(
        name="todo_list",
        description="Показывает задачи из To-Do списка (по умолчанию активные 'open', отсортированные по приоритету и дедлайну).",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "todo", "in_progress", "done", "all"],
                    "description": "Фильтр по статусу (по умолчанию 'open').",
                },
                "category": {"type": "string", "description": "Фильтр по категории (опционально)."},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": [],
        },
        handler=todos.list_todos,
    )

    registry.register(
        name="todo_update",
        description="Обновляет задачу в To-Do списке (отметить выполненной status='done', поменять приоритет или дедлайн).",
        parameters={
            "type": "object",
            "properties": {
                "todo_id": {"type": "integer", "description": "ID задачи."},
                "status": {"type": "string", "enum": ["todo", "in_progress", "done"]},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                "title": {"type": "string"},
                "due_date": {"type": "string"},
                "category": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["todo_id"],
        },
        handler=todos.update_todo,
    )

    registry.register(
        name="todo_delete",
        description="Удаляет задачу из To-Do списка по ID.",
        parameters={
            "type": "object",
            "properties": {"todo_id": {"type": "integer"}},
            "required": ["todo_id"],
        },
        handler=todos.delete_todo,
    )
