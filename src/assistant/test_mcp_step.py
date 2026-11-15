from pathlib import Path

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.fs_tools import WorkspaceFS, register_fs_tools
from assistant.gateway import LLMGatewayClient
from assistant.mcp_client import MCPManager
from assistant.tools import ToolRegistry


def main() -> None:
    workspace = Path("workspace").resolve()
    fs = WorkspaceFS(workspace)

    registry = ToolRegistry()
    register_fs_tools(registry, fs)

    mcp = MCPManager()
    mcp_tools = mcp.connect_from_config("mcp_servers.json", registry)
    print("Подключены MCP-инструменты:", mcp_tools)

    gateway = LLMGatewayClient()
    ctx = ContextManager(gateway, max_context_tokens=32_000)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt="Ты персональный ИИ-ассистент. Используй доступные инструменты ФС и MCP.",
    )

    try:
        ans = agent.run_turn(
            "Прочитай файл fib.txt (если он есть) и посчитай его метрики через MCP-инструмент text_metrics, "
            "а также составь pomodoro-план на 2.5 часа для изучения протокола MCP."
        )
        print("\nОтвет агента:\n", ans)
    finally:
        mcp.close_all()
        gateway.close()


if __name__ == "__main__":
    main()