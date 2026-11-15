from pathlib import Path

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.gateway import LLMGatewayClient
from assistant.memory import MemoryStore, register_memory_tools
from assistant.tools import ToolRegistry


def make_agent(gateway: LLMGatewayClient, memory: MemoryStore, session_id: str) -> AgentLoop:
    registry = ToolRegistry()
    register_memory_tools(registry, memory)
    ctx = ContextManager(gateway, max_context_tokens=32_000)
    return AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt=(
            "Ты персональный ассистент с долговременной памятью. "
            "Когда пользователь просит запомнить информацию или сообщает важные предпочтения, "
            "обязательно вызывай memory_save."
        ),
        memory_store=memory,
        session_id=session_id,
    )


def main() -> None:
    db_file = Path("data/test_assistant.db")
    if db_file.exists():
        db_file.unlink()

    gateway = LLMGatewayClient()
    memory = MemoryStore(db_file, gateway)

    try:
        print("=== Сессия 1: запоминаем факты ===")
        a1 = make_agent(gateway, memory, "session-1")
        print("Ответ 1:", a1.run_turn("Запомни, пожалуйста: мой основной язык разработки — Python, я работаю на MacBook (macOS), а код пишу в PyCharm."))

        print("\n=== Сессия 2 (новая пустая сессия): проверяем долговременный векторный поиск ===")
        a2 = make_agent(gateway, memory, "session-2")
        print("Ответ 2:", a2.run_turn("Напомни, какая у меня IDE и операционная система?"))

        print("\n=== Возврат в Сессию 1 (новый объект AgentLoop): проверяем переносимость контекста ===")
        a1_reloaded = make_agent(gateway, memory, "session-1")
        print("Сообщений загружено из БД:", len(a1_reloaded.messages))
        print("Ответ 3:", a1_reloaded.run_turn("О чём мы с тобой говорили в первом сообщении этой сессии?"))
    finally:
        gateway.close()


if __name__ == "__main__":
    main()