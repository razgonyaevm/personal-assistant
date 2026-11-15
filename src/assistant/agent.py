from __future__ import annotations

from datetime import datetime
from typing import Any

from assistant.config import settings
from assistant.context import ContextManager
from assistant.gateway import LLMGatewayClient
from assistant.tools import ToolRegistry


class AgentLoop:
    def __init__(
            self,
            gateway: LLMGatewayClient,
            tools: ToolRegistry,
            context_manager: ContextManager,
            system_prompt: str,
            model: str | None = None,
            max_steps: int = 12,
    ) -> None:
        self.gateway = gateway
        self.tools = tools
        self.context = context_manager
        self.model = model or settings.default_model
        self.max_steps = max_steps
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    def run_turn(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})

        if self.context.needs_compaction(self.messages):
            print("[compaction] Контекст превысил порог — сжимаем историю...")
            self.messages = self.context.compact(self.messages)

        schemas = self.tools.schemas() or None

        for step in range(1, self.max_steps + 1):
            response = self.gateway.chat_completion(
                model=self.model,
                messages=self.messages,
                tools=schemas,
            )

            usage = response.get("usage") or {}
            if "prompt_tokens" in usage:
                self.context.last_prompt_tokens = int(usage["prompt_tokens"])

            choice = response["choices"][0]
            assistant_msg = choice["message"]

            # Сохраняем assistant_msg целиком (важно для thought_signatures в Gemini)
            self.messages.append(assistant_msg)

            tool_calls = assistant_msg.get("tool_calls") or []
            if not tool_calls:
                return (assistant_msg.get("content") or "").strip()

            for tc in tool_calls:
                fn = tc["function"]
                tool_name = fn["name"]
                raw_args = fn.get("arguments", "{}")
                print(f"  [step {step}] -> tool: {tool_name}({raw_args})")

                result = self.tools.execute(tool_name, raw_args)
                print(f"  [step {step}] <- result: {result[:150]}")

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tool_name,
                        "content": result,
                    }
                )

            if self.context.needs_compaction(self.messages):
                print("[compaction] Сжатие истории внутри цикла агента...")
                self.messages = self.context.compact(self.messages)

        return "Достигнут лимит шагов агента без финального ответа."


if __name__ == "__main__":
    gateway = LLMGatewayClient()
    registry = ToolRegistry()

    registry.register(
        name="get_current_time",
        description="Возвращает текущие дату и время на компьютере.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    registry.register(
        name="multiply",
        description="Умножает два числа a и b.",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "Первое число"},
                "b": {"type": "number", "description": "Второе число"},
            },
            "required": ["a", "b"],
        },
        handler=lambda a, b: {"result": a * b},
    )

    # Ставим маленький max_context_tokens=1200 только для проверки compaction
    ctx = ContextManager(gateway, max_context_tokens=1200, keep_recent_messages=4)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt="Ты полезный персональный ассистент. Используй инструменты, когда нужны точные данные.",
    )

    try:
        print("Ответ 1:", agent.run_turn("Который сейчас час и сколько будет 37 * 43?"))
        print("Сообщений в истории:", len(agent.messages), "| Токенов:", ctx.current_token_count(agent.messages))
    finally:
        gateway.close()
