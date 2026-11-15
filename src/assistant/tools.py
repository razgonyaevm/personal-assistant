from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
            self,
            name: str,
            description: str,
            parameters: dict[str, Any],
            handler: Callable[..., Any],
    ) -> None:
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def execute(self, name: str, raw_arguments: str | dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps(
                {"error": f"Неизвестный инструмент: {name}"},
                ensure_ascii=False,
            )

        try:
            args = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else raw_arguments
            )
            if not isinstance(args, dict):
                raise ValueError("Аргументы инструмента должны быть JSON-объектом")
            result = tool.handler(**args)
        except Exception as exc:
            # Ошибку возвращаем модели, чтобы агент мог исправиться и повторить вызов
            return json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)
