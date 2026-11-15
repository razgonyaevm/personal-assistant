from __future__ import annotations

from datetime import datetime
from typing import Any

import time
from assistant.observability import get_tracer, log_event

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
            self.messages.extend(self._sanitize_history(saved_msgs))

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

    def _sanitize_history(self, msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned = list(msgs)
        # Начало истории всегда должно быть с user
        while cleaned and cleaned[0].get("role") != "user":
            cleaned.pop(0)
        # Если прошлый ход оборвался на user / tool / незакрытом tool_calls — откатываем хвост
        while cleaned and (
                cleaned[-1].get("role") != "assistant" or cleaned[-1].get("tool_calls")
        ):
            cleaned.pop()
        return cleaned

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
        tracer = get_tracer()
        t0 = time.perf_counter()

        with tracer.start_as_current_span("agent.turn") as turn_span:
            turn_span.set_attribute("openinference.span.kind", "AGENT")
            turn_span.set_attribute("session.id", self.session_id)
            turn_span.set_attribute("input.value", user_input)

            log_event("agent.turn_start", session_id=self.session_id, user_input=user_input[:200])

            snapshot = list(self.messages)
            self._inject_relevant_memories(user_input)
            self.messages.append({"role": "user", "content": user_input})

            if self.context.needs_compaction(self.messages):
                log_event("agent.compaction", session_id=self.session_id, trigger="pre_turn")
                self.messages = self.context.compact(self.messages)

            schemas = self.tools.schemas() or None
            final_answer = "Достигнут лимит шагов агента без финального ответа."

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
                    raw_msg = choice["message"]
                    assistant_msg: dict[str, Any] = {"role": "assistant"}
                    content = raw_msg.get("content")
                    tool_calls = raw_msg.get("tool_calls") or []

                    # Для Gemini: при tool_calls пустой content должен быть None, а не ""
                    if content:
                        assistant_msg["content"] = content
                    elif not tool_calls:
                        assistant_msg["content"] = ""

                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    if raw_msg.get("provider_specific_fields"):
                        assistant_msg["provider_specific_fields"] = raw_msg["provider_specific_fields"]

                    self.messages.append(assistant_msg)

                    tool_calls = assistant_msg.get("tool_calls") or []
                    if not tool_calls:
                        final_answer = (assistant_msg.get("content") or "").strip()
                        break

                    for tc in tool_calls:
                        fn = tc["function"]
                        tool_name = fn["name"]
                        raw_args = fn.get("arguments", "{}")
                        print(f"  [step {step}] -> tool: {tool_name}({raw_args})")

                        with tracer.start_as_current_span(f"tool.{tool_name}") as tool_span:
                            tool_span.set_attribute("openinference.span.kind", "TOOL")
                            tool_span.set_attribute("tool.name", tool_name)
                            tool_span.set_attribute("input.value", str(raw_args)[:1000])

                            t_tool = time.perf_counter()
                            result = self.tools.execute(tool_name, raw_args)
                            tool_ms = int((time.perf_counter() - t_tool) * 1000)

                            tool_span.set_attribute("output.value", result[:1000])
                            log_event(
                                "agent.tool_call",
                                session_id=self.session_id,
                                step=step,
                                tool=tool_name,
                                duration_ms=tool_ms,
                            )

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
                        log_event("agent.compaction", session_id=self.session_id, trigger="in_loop")
                        self.messages = self.context.compact(self.messages)

                turn_span.set_attribute("output.value", final_answer[:2000])
                log_event(
                    "agent.turn_end",
                    session_id=self.session_id,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                return final_answer
            except Exception:
                self.messages = snapshot
                raise
            finally:
                self._persist_session()
