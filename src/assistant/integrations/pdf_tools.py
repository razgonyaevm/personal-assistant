from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any
from pypdf import PdfReader

from assistant.tools import ToolRegistry


def _resolve_pdf(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        ws = Path(os.getenv("ASSISTANT_WORKSPACE_DIR", "workspace")).resolve()
        candidate = (ws / path_str).resolve()
        if candidate.exists():
            return candidate
    resolved = p.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"PDF файл не найден: '{path_str}'")
    return resolved


def pdf_info(path: str) -> dict[str, Any]:
    target = _resolve_pdf(path)
    reader = PdfReader(str(target))
    meta = reader.metadata or {}

    outlines: list[str] = []
    try:
        def walk_outline(outline_items: list[Any], depth: int = 0) -> None:
            if depth > 2:
                return
            for item in outline_items:
                if isinstance(item, list):
                    walk_outline(item, depth + 1)
                elif hasattr(item, "title"):
                    outlines.append(f"{'  ' * depth}• {item.title}")

        walk_outline(reader.outline)
    except Exception:
        pass

    return {
        "file_name": target.name,
        "total_pages": len(reader.pages),
        "file_size_kb": round(target.stat().st_size / 1024, 1),
        "title": meta.get("/Title") or meta.get("title"),
        "author": meta.get("/Author") or meta.get("author"),
        "table_of_contents": outlines[:30] if outlines else None,
    }


def read_pdf(
        path: str,
        start_page: int = 1,
        end_page: int | None = None,
        max_chars: int = 8000,
) -> str:
    target = _resolve_pdf(path)
    reader = PdfReader(str(target))
    total = len(reader.pages)

    start = max(1, min(total, int(start_page)))
    end = min(total, int(end_page)) if end_page is not None else min(total, start + 3)

    pages_text: list[str] = []
    current_chars = 0

    for pno in range(start, end + 1):
        page = reader.pages[pno - 1]
        raw_text = page.extract_text() or ""
        clean_text = re.sub(r"[ \t]+", " ", raw_text).strip()
        header = f"=== Страница {pno} из {total} ==="
        chunk = f"{header}\n{clean_text}"

        if current_chars + len(chunk) > max_chars and pages_text:
            pages_text.append(
                f"\n[...достигнут лимит {max_chars} символов. Вызовите read_pdf с start_page={pno}...]"
            )
            break

        pages_text.append(chunk)
        current_chars += len(chunk)

    return f"Файл: {target.name} (страницы {start}..{end} из {total})\n\n" + "\n\n".join(pages_text)


def search_pdf(
        path: str,
        query: str,
        max_matches: int = 5,
) -> list[dict[str, Any]]:
    target = _resolve_pdf(path)
    reader = PdfReader(str(target))
    q_lower = query.lower().strip()

    results: list[dict[str, Any]] = []
    for idx, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lower = text.lower()
        if q_lower in lower:
            pos = lower.find(q_lower)
            start_pos = max(0, pos - 120)
            end_pos = min(len(text), pos + len(q_lower) + 160)
            excerpt = text[start_pos:end_pos].replace("\n", " ").strip()
            results.append({
                "page": idx,
                "excerpt": f"...{excerpt}...",
            })
            if len(results) >= max_matches:
                break

    return results


def register_pdf_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="pdf_info",
        description="Возвращает метаданные PDF-документа: количество страниц, автора и оглавление.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Имя файла в workspace или путь к PDF."}
            },
            "required": ["path"],
        },
        handler=pdf_info,
    )

    registry.register(
        name="read_pdf",
        description="Читает текст из диапазона страниц PDF-документа.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Имя файла в workspace или путь к PDF."},
                "start_page": {"type": "integer", "description": "Начальная страница (с 1, по умолчанию 1)."},
                "end_page": {"type": "integer", "description": "Конечная страница (включительно)."},
                "max_chars": {"type": "integer", "description": "Максимум символов (по умолчанию 8000)."},
            },
            "required": ["path"],
        },
        handler=read_pdf,
    )

    registry.register(
        name="search_pdf",
        description="Ищет ключевые слова или фразу в PDF-файле и возвращает номера страниц с цитатами.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Имя файла в workspace или путь к PDF."},
                "query": {"type": "string", "description": "Поисковая фраза или термин."},
                "max_matches": {"type": "integer", "description": "Сколько совпадений вернуть (по умолчанию 5)."},
            },
            "required": ["path", "query"],
        },
        handler=search_pdf,
    )
