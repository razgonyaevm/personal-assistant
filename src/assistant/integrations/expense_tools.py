from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from pypdf import PdfReader

from assistant.tools import ToolRegistry


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _normalize_date(date_str: str | None) -> str:
    if not date_str or not date_str.strip():
        return _local_now().strftime("%Y-%m-%d")
    clean = date_str.strip()
    # Поддержка DD.MM.YYYY и YYYY-MM-DD
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", clean):
        dt = datetime.strptime(clean, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    return clean[:10]


def _make_dedup_hash(tx_date: str, amount: float, description: str) -> str:
    raw = f"{tx_date}|{amount:.2f}|{description.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ExpenseTracker:
    def __init__(self, db_path: str | Path = "data/assistant.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = Path(os.getenv("ASSISTANT_WORKSPACE_DIR", "workspace")).resolve()
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
                CREATE TABLE IF NOT EXISTS expenses
                (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    tx_date     TEXT NOT NULL,
                    amount      REAL NOT NULL,
                    category    TEXT NOT NULL DEFAULT 'Другое',
                    description TEXT NOT NULL DEFAULT '',
                    source      TEXT NOT NULL DEFAULT 'manual',
                    dedup_hash  TEXT,
                    created_at  TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (tx_date);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_dedup
                    ON expenses (dedup_hash) WHERE dedup_hash IS NOT NULL;
                """
            )

    def add_expenses(
            self,
            items: list[dict[str, Any]],
            source: str = "manual",
    ) -> dict[str, Any]:
        """Добавляет один или несколько расходов сразу."""
        now_iso = _local_now().isoformat()
        added: list[dict[str, Any]] = []
        skipped_dups = 0

        with self._connect() as conn:
            for item in items:
                amount = round(float(item["amount"]), 2)
                if amount <= 0:
                    continue
                category = str(item.get("category") or "Другое").strip()
                description = str(item.get("description") or "").strip()
                tx_date = _normalize_date(item.get("tx_date"))
                dedup = _make_dedup_hash(tx_date, amount, description) if source != "manual" else None

                try:
                    cur = conn.execute(
                        """
                        INSERT INTO expenses(tx_date, amount, category, description, source, dedup_hash, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (tx_date, amount, category, description, source, dedup, now_iso),
                    )
                    added.append(
                        {
                            "id": int(cur.lastrowid),
                            "tx_date": tx_date,
                            "amount": amount,
                            "category": category,
                            "description": description,
                        }
                    )
                except sqlite3.IntegrityError:
                    skipped_dups += 1

        return {
            "status": "ok",
            "added_count": len(added),
            "skipped_duplicates": skipped_dups,
            "added": added,
        }

    def list_expenses(
            self,
            month: str | None = None,
            category: str | None = None,
            limit: int = 25,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        clauses: list[str] = []
        params: list[Any] = []

        if month and month.strip():
            clauses.append("tx_date LIKE ?")
            params.append(f"{month.strip()[:7]}%")
        if category and category.strip():
            clauses.append("LOWER(category) = LOWER(?)")
            params.append(category.strip())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT id, tx_date, amount, category, description, source FROM expenses {where} ORDER BY tx_date DESC, id DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def monthly_summary(self, month: str | None = None) -> dict[str, Any]:
        target_month = (month or _local_now().strftime("%Y-%m")).strip()[:7]

        # Предыдущий месяц для сравнения
        year, mon = map(int, target_month.split("-"))
        prev_year, prev_mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
        prev_month = f"{prev_year:04d}-{prev_mon:02d}"

        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0.0) AS total
                FROM expenses
                WHERE tx_date LIKE ?
                """,
                (f"{target_month}%",),
            ).fetchone()

            prev_totals = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0.0) AS total
                FROM expenses
                WHERE tx_date LIKE ?
                """,
                (f"{prev_month}%",),
            ).fetchone()

            by_cat = conn.execute(
                """
                SELECT category, COUNT(*) AS count, ROUND(SUM(amount), 2) AS sum_rub
                FROM expenses
                WHERE tx_date LIKE ?
                GROUP BY category
                ORDER BY sum_rub DESC
                """,
                (f"{target_month}%",),
            ).fetchall()

            top_expenses = conn.execute(
                """
                SELECT id, tx_date, amount, category, description
                FROM expenses
                WHERE tx_date LIKE ?
                ORDER BY amount DESC
                LIMIT 5
                """,
                (f"{target_month}%",),
            ).fetchall()

            active_days_row = conn.execute(
                "SELECT COUNT(DISTINCT tx_date) AS days FROM expenses WHERE tx_date LIKE ?",
                (f"{target_month}%",),
            ).fetchone()

        total_rub = round(float(totals["total"]), 2)
        prev_rub = round(float(prev_totals["total"]), 2)
        days_count = max(1, int(active_days_row["days"]))

        categories_breakdown = []
        for r in by_cat:
            val = float(r["sum_rub"])
            pct = round((val / total_rub * 100), 1) if total_rub > 0 else 0.0
            categories_breakdown.append(
                {
                    "category": r["category"],
                    "count": r["count"],
                    "sum_rub": val,
                    "percent": pct,
                }
            )

        diff_pct = None
        if prev_rub > 0:
            diff_pct = round(((total_rub - prev_rub) / prev_rub) * 100, 1)

        return {
            "month": target_month,
            "transactions_count": int(totals["cnt"]),
            "total_spent_rub": total_rub,
            "avg_per_active_day_rub": round(total_rub / days_count, 2) if total_rub > 0 else 0.0,
            "previous_month": prev_month,
            "previous_month_spent_rub": prev_rub,
            "change_vs_prev_month_pct": diff_pct,
            "by_category": categories_breakdown,
            "top_5_expenses": [dict(r) for r in top_expenses],
        }

    def delete_expense(self, expense_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            if cur.rowcount == 0:
                return {"status": "not_found", "id": expense_id}
        return {"status": "deleted", "id": expense_id}

    def import_sber_pdf(self, path: str) -> dict[str, Any]:
        """
        Парсит PDF-выписку СберБанка из workspace/ (формат: Дата Время Категория Сумма Остаток).
        Пополнения (+...) пропускает, сумму берёт из предпоследней колонки, а не из остатка.
        """
        p = Path(path)
        if not p.is_absolute():
            candidate = (self.workspace_dir / path).resolve()
            if candidate.exists():
                p = candidate
        if not p.exists():
            raise FileNotFoundError(f"Файл выписки не найден: '{path}'")

        reader = PdfReader(str(p))
        full_text = "\n".join((page.extract_text() or "") for page in reader.pages)
        lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

        # Строка 1: 30.10.2026 22:58 Супермаркеты 485,94 5 838,91
        tx_line_re = re.compile(
            r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})\s+(.+?)\s+"
            r"([+-]?\d{1,3}(?:[\s\u00a0]\d{3})*[.,]\d{2})\s+"
            r"([+-]?\d{1,3}(?:[\s\u00a0]\d{3})*[.,]\d{2})$"
        )
        # Строка 2: 30.10.2026 442721 Lenta-0012 SANKT-PETERBU RUS...
        desc_line_re = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{6})\s+(.+)$")
        stop_prefixes = (
            "Продолжение", "Выписка", "Дата ", "ДАТА ", "*", "ПАО Сбербанк", "ИТОГО"
        )

        parsed_items: list[dict[str, Any]] = []
        skipped_income = 0
        total_amount = 0.0

        i = 0
        while i < len(lines):
            m = tx_line_re.match(lines[i])
            if not m:
                i += 1
                continue

            tx_date, tx_time, category, raw_amount, _raw_balance = m.groups()
            i += 1

            # Забираем описание со следующей строки (и возможный перенос, например ****1167)
            auth_code = ""
            desc_parts: list[str] = []
            if i < len(lines):
                dm = desc_line_re.match(lines[i])
                if dm:
                    auth_code = dm.group(2)
                    desc_parts.append(dm.group(3))
                    i += 1
                    if (
                            i < len(lines)
                            and not tx_line_re.match(lines[i])
                            and not re.match(r"^\d{2}\.\d{2}\.\d{4}\b", lines[i])
                            and not lines[i].startswith(stop_prefixes)
                    ):
                        desc_parts.append(lines[i])
                        i += 1

            # Пополнения (+...) не заносим в расходы
            if raw_amount.strip().startswith("+"):
                skipped_income += 1
                continue

            cleaned_num = re.sub(r"[\s\u00a0]", "", raw_amount).replace(",", ".").lstrip("-")
            val = round(float(cleaned_num), 2)
            description = " ".join(desc_parts).strip() or category
            # Уникальный ключ с временем и кодом авторизации (чтобы одинаковые чеки в один день не склеивались)
            unique_desc = f"[{tx_time} #{auth_code}] {description}" if auth_code else f"[{tx_time}] {description}"

            total_amount += val
            parsed_items.append(
                {
                    "tx_date": tx_date,
                    "amount": val,
                    "category": category.strip()[:40],
                    "description": unique_desc[:160],
                }
            )

        if not parsed_items:
            return {
                "status": "parsed_zero",
                "message": "Не удалось распознать операции. Прочитайте файл через read_pdf.",
            }

        res = self.add_expenses(parsed_items, source="sber_pdf")
        res["file"] = p.name
        res["skipped_income_operations"] = skipped_income
        res["parsed_expenses_sum_rub"] = round(total_amount, 2)
        return res


def register_expense_tools(registry: ToolRegistry, tracker: ExpenseTracker) -> None:
    registry.register(
        name="expense_add",
        description=(
            "Записывает один или несколько расходов в трекер. "
            "Категории желательно нормализовать (например: Еда, Кофе, Транспорт, Подписки, Учёба, Спорт, Покупки, Развлечения)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "number", "description": "Сумма в рублях."},
                            "category": {"type": "string", "description": "Категория расхода."},
                            "description": {"type": "string", "description": "Краткое описание (где/что купили)."},
                            "tx_date": {"type": "string", "description": "Дата YYYY-MM-DD (по умолчанию сегодня)."},
                        },
                        "required": ["amount", "category"],
                    },
                    "description": "Список расходов для сохранения.",
                }
            },
            "required": ["items"],
        },
        handler=tracker.add_expenses,
    )

    registry.register(
        name="expense_list",
        description="Показывает последние расходы (можно фильтровать по месяцу 'YYYY-MM' или категории).",
        parameters={
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Месяц в формате 'YYYY-MM' (опционально)."},
                "category": {"type": "string", "description": "Категория (опционально)."},
                "limit": {"type": "integer", "description": "Максимум записей (по умолчанию 25)."},
            },
            "required": [],
        },
        handler=tracker.list_expenses,
    )

    registry.register(
        name="expense_monthly_summary",
        description=(
            "Возвращает полную финансовую аналитику за месяц: общие траты, разбивку по категориям с процентами, "
            "средний расход в день, топ-5 крупных покупок и сравнение с прошлым месяцем."
        ),
        parameters={
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Месяц 'YYYY-MM' (по умолчанию текущий)."}
            },
            "required": [],
        },
        handler=tracker.monthly_summary,
    )

    registry.register(
        name="expense_delete",
        description="Удаляет ошибочно добавленный расход по его ID.",
        parameters={
            "type": "object",
            "properties": {"expense_id": {"type": "integer"}},
            "required": ["expense_id"],
        },
        handler=tracker.delete_expense,
    )

    registry.register(
        name="expense_import_sber_pdf",
        description="Импортирует списания из PDF-выписки СберБанка, лежащей в workspace/.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к PDF-выписке в workspace."}
            },
            "required": ["path"],
        },
        handler=tracker.import_sber_pdf,
    )
