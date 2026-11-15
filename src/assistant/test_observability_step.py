import json
from pathlib import Path

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.fs_tools import WorkspaceFS, register_fs_tools
from assistant.gateway import LLMGatewayClient
from assistant.memory import MemoryStore, register_memory_tools
from assistant.observability import SpendTracker, init_tracing
from assistant.tools import ToolRegistry


def main() -> None:
    init_tracing()
    spend = SpendTracker("data/assistant.db")
    gateway = LLMGatewayClient(spend_tracker=spend, channel="cli-test", session_id="obs-test")
    memory = MemoryStore("data/assistant.db", gateway)
    fs = WorkspaceFS(Path("workspace"))

    registry = ToolRegistry()
    register_fs_tools(registry, fs)
    register_memory_tools(registry, memory)

    ctx = ContextManager(gateway, max_context_tokens=32_000)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt="Ты ассистент. Посмотри файлы в workspace и кратко ответь пользователю.",
        memory_store=memory,
        session_id="obs-test",
    )

    try:
        ans = agent.run_turn("Посмотри дерево файлов в workspace и перечисли, что там есть.")
        print("Ответ:", ans)
        print("\n=== Сводка расхода (SpendTracker) ===")
        print(json.dumps(spend.summary(), ensure_ascii=False, indent=2))
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
