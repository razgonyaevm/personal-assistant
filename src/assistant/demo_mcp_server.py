from __future__ import annotations

import json
import sys
from typing import Any


TOOLS = [
    {
        "name": "text_metrics",
        "description": "Считает количество символов, слов и строк в переданном тексте.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст для анализа."}
            },
            "required": ["text"],
        },
    },
    {
        "name": "pomodoro_plan",
        "description": "Разбивает задачу на Pomodoro-интервалы по 25 минут.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Название задачи."},
                "hours": {"type": "number", "description": "Сколько часов выделено."},
            },
            "required": ["task", "hours"],
        },
    },
]


def handle_tool(name: str, args: dict[str, Any]) -> str:
    if name == "text_metrics":
        text = str(args.get("text", ""))
        result = {
            "chars": len(text),
            "words": len(text.split()),
            "lines": len(text.splitlines()) if text else 0,
        }
        return json.dumps(result, ensure_ascii=False)

    if name == "pomodoro_plan":
        task = str(args.get("task", "Задача"))
        hours = float(args.get("hours", 1))
        sessions = max(1, int((hours * 60) // 30))
        return json.dumps(
            {
                "task": task,
                "pomodoro_sessions_25m": sessions,
                "break_5m_each": sessions,
                "tip": "После каждых 4 сессий делай перерыв 15-20 минут.",
            },
            ensure_ascii=False,
        )

    raise ValueError(f"Unknown tool: {name}")


def main() -> None:
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        req = json.loads(raw_line)
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Уведомления (без id) ответа не требуют
        if req_id is None:
            continue

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "demo-local-mcp", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                text = handle_tool(params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}]}
            else:
                result = {}

            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()