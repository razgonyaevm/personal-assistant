from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import Any
from ddgs import DDGS
import httpx

from assistant.tools import ToolRegistry


class _TextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self._chunks.append(cleaned)

    def get_text(self) -> str:
        joined = "\n".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", joined)


def web_search(
        query: str,
        kind: str = "text",
        max_results: int = 5,
) -> list[dict[str, Any]]:
    max_results = max(1, min(10, int(max_results)))
    backends = ("duckduckgo", "bing", "google", "brave", "auto")
    last_error: Exception | None = None

    for backend in backends:
        try:
            with DDGS(timeout=7) as ddgs:
                if kind == "news":
                    raw = list(
                        ddgs.news(
                            query,
                            region="ru-ru",
                            max_results=max_results,
                            backend=backend,
                        )
                    )
                    if raw:
                        return [
                            {
                                "title": item.get("title", ""),
                                "url": item.get("url", ""),
                                "date": item.get("date", ""),
                                "source": item.get("source", ""),
                                "snippet": item.get("body", ""),
                            }
                            for item in raw
                        ]
                else:
                    raw = list(
                        ddgs.text(
                            query,
                            region="ru-ru",
                            max_results=max_results,
                            backend=backend,
                        )
                    )
                    if raw:
                        return [
                            {
                                "title": item.get("title", ""),
                                "url": item.get("href", ""),
                                "snippet": item.get("body", ""),
                            }
                            for item in raw
                        ]
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    return []


def fetch_webpage(url: str, max_chars: int = 6000) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or "<html" in resp.text[:500].lower():
            parser = _TextExtractor()
            parser.feed(resp.text)
            text = parser.get_text()
        else:
            text = resp.text

    truncated = len(text) > max_chars
    return {
        "url": str(resp.url),
        "status_code": resp.status_code,
        "truncated": truncated,
        "content": text[:max_chars],
    }


def register_web_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="web_search",
        description="Ищет актуальную информацию или новости в интернете.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос."},
                "kind": {
                    "type": "string",
                    "enum": ["text", "news"],
                    "description": "Тип поиска: 'text' (обычный) или 'news' (новости).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Количество результатов (1–10, по умолчанию 5).",
                },
            },
            "required": ["query"],
        },
        handler=web_search,
    )

    registry.register(
        name="fetch_webpage",
        description="Загружает веб-страницу по URL и извлекает её основной текст.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL страницы (https://...)."},
                "max_chars": {
                    "type": "integer",
                    "description": "Максимум символов текста (по умолчанию 6000).",
                },
            },
            "required": ["url"],
        },
        handler=fetch_webpage,
    )
