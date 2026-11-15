from __future__ import annotations

from datetime import datetime
from typing import Any

from assistant.config import settings
from assistant.context import ContextManager
from assistant.gateway import LLMGatewayClient
from assistant.memory import MemoryStore
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
            memory_store: MemoryStore | None = None,
            session_id: str = "default",
    ) -> None:
        self.gateway = gateway
        self.tools = tools
        self.context = context_manager
        self.base_system_prompt = system_prompt
        self.model = model or settings.default_model
        self.max_steps = max_steps
        self.memory_store = memory_store
        self.session_id = session_id

        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

        if self.memory_store is not None:
            saved_summary, saved_msgs = self.memory_store.load_session(self.session_id)
            if saved_summary:
                self.context.summary = saved_summary
                self.messages.append(
                    {
                        "role": "system",
                        "content": f"[Сжатый контекст предыдущего диалога]\n{saved_summary}",
                    }
                )
            self.messages.extend(saved_msgs)

    def _inject_relevant_memories(self, user_input: str) -> None:
        if self.memory_store is None:
            return
        hits = self.memory_store.search_memory(user_input, limit=3)
        relevant = [h for h in hits if h["score"] >= 0.45]
        # Убираем старый временный блок воспоминаний перед добавлением свежего
        self.messages = [
            m
            for m in self.messages
            if not (
                    m.get("role") == "system"
                    and str(m.get("content", "")).startswith("[Релевантные факты из долговременной памяти]")
            )
        ]
        if relevant:
            lines = "\n".join(
                f"- [id={h['id']}, {h['category']}] {h['content']}" for h in relevant
            )
            self.messages.insert(
                1,
                {
                    "role": "system",
                    "content": f"[Релевантные факты из долговременной памяти]\n{lines}",
                },
            )

    def _persist_session(self) -> None:
        if self.memory_store is None:
            return
        non_system = [m for m in self.messages if m.get("role") != "system"]
        self.memory_store.save_session_state(
            session_id=self.session_id,
            summary=self.context.summary,
            non_system_messages=non_system,
        )

    def run_turn(self, user_input: str) -> str:
        self._inject_relevant_memories(user_input)
        self.messages.append({"role": "user", "content": user_input})

        if self.context.needs_compaction(self.messages):
            print("[compaction] Контекст превысил порог — сжимаем историю...")
            self.messages = self.context.compact(self.messages)

        schemas = self.tools.schemas() or None

        try:
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
        finally:
            self._persist_session()
