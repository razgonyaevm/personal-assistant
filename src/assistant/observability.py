from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from assistant.config import settings

_TRACER_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()


def init_tracing() -> trace.Tracer:
    global _TRACER_INITIALIZED
    with _INIT_LOCK:
        if not _TRACER_INITIALIZED:
            if settings.otel_enabled:
                resource = Resource.create(
                    {
                        "service.name": settings.service_name,
                        "openinference.project.name": settings.service_name,
                    }
                )
                provider = TracerProvider(resource=resource)
                exporter = OTLPSpanExporter(
                    endpoint=settings.phoenix_endpoint,
                    timeout=3,
                )
                processor = BatchSpanProcessor(exporter)
                provider.add_span_processor(processor)
                trace.set_tracer_provider(provider)
                atexit.register(provider.shutdown)
            _TRACER_INITIALIZED = True
    return trace.get_tracer(settings.service_name)


def get_tracer() -> trace.Tracer:
    if not _TRACER_INITIALIZED:
        return init_tracing()
    return trace.get_tracer(settings.service_name)


def log_event(event: str, level: str = "INFO", **fields: Any) -> None:
    """Пишет структурированную JSON-строку в logs/assistant.jsonl."""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    trace_id = format(ctx.trace_id, "032x") if ctx and ctx.is_valid else None
    span_id = format(ctx.span_id, "016x") if ctx and ctx.is_valid else None

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        "trace_id": trace_id,
        "span_id": span_id,
        **fields,
    }

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOG_LOCK:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class SpendTracker:
    """Локальный учёт расхода токенов и USD в SQLite (для дашборда и команды /stats)."""

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
                CREATE TABLE IF NOT EXISTS usage_events
                (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                TEXT    NOT NULL,
                    channel           TEXT    NOT NULL DEFAULT 'cli',
                    session_id        TEXT    NOT NULL DEFAULT 'default',
                    model             TEXT    NOT NULL,
                    operation         TEXT    NOT NULL,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens      INTEGER NOT NULL DEFAULT 0,
                    cost_usd          REAL    NOT NULL DEFAULT 0.0,
                    latency_ms        INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def record(
            self,
            *,
            model: str,
            operation: str,
            prompt_tokens: int,
            completion_tokens: int,
            total_tokens: int,
            cost_usd: float,
            latency_ms: int,
            channel: str = "cli",
            session_id: str = "default",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events(ts, channel, session_id, model, operation,
                                         prompt_tokens, completion_tokens, total_tokens, cost_usd, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    channel,
                    session_id,
                    model,
                    operation,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cost_usd,
                    latency_ms,
                ),
            )

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*)                       AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cost_usd), 0.0)   AS total_cost_usd,
                       COALESCE(AVG(latency_ms), 0)   AS avg_latency_ms
                FROM usage_events
                """
            ).fetchone()
            by_model = conn.execute(
                """
                SELECT model,
                       COUNT(*)                       AS requests,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(cost_usd), 0.0)   AS cost_usd
                FROM usage_events
                GROUP BY model
                ORDER BY cost_usd DESC
                """
            ).fetchall()

        return {
            "requests": int(totals["requests"]),
            "total_tokens": int(totals["total_tokens"]),
            "total_cost_usd": round(float(totals["total_cost_usd"]), 6),
            "avg_latency_ms": int(totals["avg_latency_ms"]),
            "by_model": [dict(r) for r in by_model],
        }
