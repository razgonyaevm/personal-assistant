from __future__ import annotations

from array import array
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from assistant.gateway import LLMGatewayClient
from assistant.tools import ToolRegistry

TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pack_vector(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack_vector(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, dot / (na * nb))


def _sanitize_fts_query(query: str) -> str:
    tokens = TOKEN_RE.findall(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:12])


class MemoryStore:
    def __init__(
        self,
        db_path: str | Path,
        gateway: LLMGatewayClient,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.gateway = gateway
        self._fts_enabled = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    msg_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_session_messages_sid
                    ON session_messages(session_id, id);

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'fact',
                    tags TEXT NOT NULL DEFAULT '',
                    importance INTEGER NOT NULL DEFAULT 3,
                    embedding BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                    USING fts5(content, tags, category, content='memories', content_rowid='id');
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

    # --- Краткосрочный контекст (сессии) ---

    def ensure_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(session_id, summary, updated_at) VALUES (?, '', ?)",
                (session_id, _utc_now()),
            )

    def load_session(self, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        self.ensure_session(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            summary = row["summary"] if row else ""

            msg_rows = conn.execute(
                "SELECT msg_json FROM session_messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            messages = [json.loads(r["msg_json"]) for r in msg_rows]
        return summary, messages

    def save_session_state(
        self,
        session_id: str,
        summary: str,
        non_system_messages: list[dict[str, Any]],
    ) -> None:
        self.ensure_session(session_id)
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET summary = ?, updated_at = ? WHERE session_id = ?",
                (summary, now, session_id),
            )
            conn.execute(
                "DELETE FROM session_messages WHERE session_id = ?",
                (session_id,),
            )
            conn.executemany(
                "INSERT INTO session_messages(session_id, msg_json, created_at) VALUES (?, ?, ?)",
                [
                    (session_id, json.dumps(m, ensure_ascii=False), now)
                    for m in non_system_messages
                ],
            )

    # --- Долговременная память (векторный поиск + FTS5) ---

    def save_memory(
        self,
        content: str,
        category: str = "fact",
        tags: str = "",
        importance: int = 3,
    ) -> dict[str, Any]:
        clean_content = content.strip()
        if not clean_content:
            raise ValueError("Пустой текст нельзя сохранить в память.")

        importance = max(1, min(5, int(importance)))
        vector = self.gateway.create_embedding(clean_content)
        blob = _pack_vector(vector)
        now = _utc_now()

        with self._connect() as conn:
            # Проверяем на очень похожий дубликат
            existing = conn.execute("SELECT id, content, embedding FROM memories").fetchall()
            for row in existing:
                sim = _cosine_similarity(vector, _unpack_vector(row["embedding"]))
                if sim > 0.96 and row["content"].strip().lower() == clean_content.lower():
                    return {"status": "already_exists", "id": row["id"], "content": row["content"]}

            cur = conn.execute(
                """
                INSERT INTO memories(content, category, tags, importance, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (clean_content, category, tags, importance, blob, now),
            )
            mem_id = int(cur.lastrowid)

            if self._fts_enabled:
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content, tags, category) VALUES (?, ?, ?, ?)",
                    (mem_id, clean_content, tags, category),
                )

        return {"status": "saved", "id": mem_id, "category": category, "importance": importance}

    def search_memory(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query_vec = self.gateway.create_embedding(query)

        with self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE category = ?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM memories").fetchall()

            if not rows:
                return []

            # 1. Полнотекстовые оценки через FTS5 (BM25)
            fts_scores: dict[int, float] = {}
            fts_query = _sanitize_fts_query(query)
            if self._fts_enabled and fts_query:
                try:
                    fts_rows = conn.execute(
                        """
                        SELECT rowid, bm25(memories_fts) AS rank
                        FROM memories_fts
                        WHERE memories_fts MATCH ?
                        ORDER BY rank
                        LIMIT 20
                        """,
                        (fts_query,),
                    ).fetchall()
                    for idx, fr in enumerate(fts_rows):
                        # В SQLite bm25 отрицателен (чем меньше, тем релевантнее) -> нормируем в (0..1]
                        fts_scores[int(fr["rowid"])] = 1.0 / (1.0 + idx)
                except sqlite3.OperationalError:
                    pass

        # 2. Гибридный скоринг: vector + FTS5 + лёгкий бонус за важность
        scored: list[dict[str, Any]] = []
        for row in rows:
            mem_id = int(row["id"])
            vec_score = _cosine_similarity(query_vec, _unpack_vector(row["embedding"]))
            kw_score = fts_scores.get(mem_id, 0.0)
            imp_bonus = (int(row["importance"]) - 1) * 0.02
            hybrid_score = 0.70 * vec_score + 0.30 * kw_score + imp_bonus

            scored.append(
                {
                    "id": mem_id,
                    "content": row["content"],
                    "category": row["category"],
                    "tags": row["tags"],
                    "importance": row["importance"],
                    "created_at": row["created_at"],
                    "vector_score": round(vec_score, 4),
                    "keyword_score": round(kw_score, 4),
                    "score": round(hybrid_score, 4),
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def delete_memory(self, memory_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not row:
                return {"status": "not_found", "id": memory_id}
            if self._fts_enabled:
                conn.execute(
                    "INSERT INTO memories_fts(memories_fts, rowid, content, tags, category) VALUES ('delete', ?, ?, ?, ?)",
                    (memory_id, row["content"], row["tags"], row["category"]),
                )
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return {"status": "deleted", "id": memory_id}


def register_memory_tools(registry: ToolRegistry, memory: MemoryStore) -> None:
    registry.register(
        name="memory_save",
        description=(
            "Сохраняет важный факт, предпочтение пользователя, решение или контекст проекта "
            "в долговременную память."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Факт или заметка для запоминания."},
                "category": {
                    "type": "string",
                    "enum": ["fact", "preference", "project", "episode"],
                    "description": "Категория записи.",
                },
                "tags": {"type": "string", "description": "Теги через запятую (опционально)."},
                "importance": {
                    "type": "integer",
                    "description": "Важность от 1 до 5 (по умолчанию 3).",
                },
            },
            "required": ["content"],
        },
        handler=memory.save_memory,
    )

    registry.register(
        name="memory_search",
        description="Ищет факты и прошлые решения в долговременной памяти (гибридный векторный + текстовый поиск).",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос."},
                "category": {
                    "type": "string",
                    "enum": ["fact", "preference", "project", "episode"],
                    "description": "Фильтр по категории (опционально).",
                },
                "limit": {"type": "integer", "description": "Сколько записей вернуть (по умолчанию 5)."},
            },
            "required": ["query"],
        },
        handler=memory.search_memory,
    )

    registry.register(
        name="memory_delete",
        description="Удаляет устаревшую или ошибочную запись из долговременной памяти по её id.",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "ID записи для удаления."}
            },
            "required": ["memory_id"],
        },
        handler=memory.delete_memory,
    )