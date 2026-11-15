from __future__ import annotations

import json
from typing import Any

from assistant.config import settings
from assistant.gateway import LLMGatewayClient


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Грубая оценка токенов (на случай, если ещё не было ответа от шлюза)."""
    total_chars = 0
    for msg in messages:
        total_chars += len(json.dumps(msg, ensure_ascii=False, default=str))
    # ~3.5 символа на токен для смешанного RU/EN текста и JSON
    return max(1, int(total_chars / 3.5))


class ContextManager:
    def __init__(
            self,
            gateway: LLMGatewayClient,
            max_context_tokens: int = 32_000,
            compact_trigger_ratio: float = 0.75,
            keep_recent_messages: int = 8,
    ) -> None:
        self.gateway = gateway
        self.max_context_tokens = max_context_tokens
        self.compact_trigger_ratio = compact_trigger_ratio
        self.keep_recent_messages = keep_recent_messages
        self.last_prompt_tokens: int = 0
        self.summary: str = ""

    def current_token_count(self, messages: list[dict[str, Any]]) -> int:
        # Берём максимум из реального счётчика последнего ответа и локальной оценки
        return max(self.last_prompt_tokens, estimate_tokens(messages))

    def needs_compaction(self, messages: list[dict[str, Any]]) -> bool:
        if len(messages) <= self.keep_recent_messages + 2:
            return False
        limit = int(self.max_context_tokens * self.compact_trigger_ratio)
        return self.current_token_count(messages) >= limit

    def _find_safe_split_index(self, non_system: list[dict[str, Any]]) -> int:
        """
        Выбирает индекс разделения так, чтобы недавняя история всегда
        начиналась с сообщения 'user' (и не разрывала tool_calls / tool).
        """
        split_idx = max(0, len(non_system) - self.keep_recent_messages)
        while split_idx > 0 and non_system[split_idx].get("role") != "user":
            split_idx -= 1
        return split_idx

    def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Сжимает старую часть диалога в краткое резюме через assistant-fast."""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        split_idx = self._find_safe_split_index(non_system)
        if split_idx <= 1:
            return messages

        to_summarize = non_system[:split_idx]
        recent = non_system[split_idx:]

        serialized = json.dumps(to_summarize, ensure_ascii=False, indent=2)
        prev_summary_block = (
            f"Предыдущее резюме диалога:\n{self.summary}\n\n"
            if self.summary
            else ""
        )

        prompt = (
            f"{prev_summary_block}"
            "Сожми следующую часть переписки и вызовов инструментов в структурированное "
            "резюме на русском языке. Обязательно сохрани: цели пользователя, принятые решения, "
            "важные факты, пути к файлам, результаты команд и незавершённые задачи. "
            "Убери воду и повторяющиеся технические детали.\n\n"
            f"История:\n{serialized}"
        )

        res = self.gateway.chat_completion(
            model=settings.fast_model,
            messages=[
                {
                    "role": "system",
                    "content": "Ты модуль сжатия памяти ассистента. Пиши сжато и точно.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        self.summary = (res["choices"][0]["message"].get("content") or "").strip()

        # Собираем обновлённый список сообщений
        base_system = [
            m for m in system_msgs
            if not str(m.get("content", "")).startswith("[Сжатый контекст")
        ]
        summary_msg = {
            "role": "system",
            "content": f"[Сжатый контекст предыдущего диалога]\n{self.summary}",
        }
        new_messages = [*base_system, summary_msg, *recent]
        # Сбрасываем last_prompt_tokens на актуальную оценку после сжатия
        self.last_prompt_tokens = estimate_tokens(new_messages)
        return new_messages
