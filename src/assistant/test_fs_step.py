from pathlib import Path

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.fs_tools import WorkspaceFS, register_fs_tools
from assistant.gateway import LLMGatewayClient
from assistant.standards import (
    build_standards_prompt,
    discover_skills,
    register_skill_tool,
)
from assistant.tools import ToolRegistry


def main() -> None:
    workspace = Path("workspace").resolve()
    fs = WorkspaceFS(workspace)
    skills = discover_skills(workspace / "skills")

    registry = ToolRegistry()
    register_fs_tools(registry, fs)
    register_skill_tool(registry, skills)

    base_prompt = (
        "Ты персональный ИИ-ассистент. Для работы с файлами используй инструменты ФС.\n\n"
        + build_standards_prompt(workspace, skills)
    )

    gateway = LLMGatewayClient()
    ctx = ContextManager(gateway, max_context_tokens=32_000)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt=base_prompt,
    )

    try:
        print("=== Тест 1: обзор дерева и создание заметки по SKILL.md + AGENTS.md ===")
        ans1 = agent.run_turn(
            "Посмотри структуру workspace и оформи заметку по встрече: "
            "мы выбрали Python и LiteLLM, следующий шаг — поднять Docker-песочницу."
        )
        print("Ответ 1:\n", ans1)

        print("\n=== Тест 2: точечное редактирование файла ===")
        ans2 = agent.run_turn(
            "Добавь в раздел следующих шагов созданной заметки ещё один пункт: '- [ ] Настроить MCP'."
        )
        print("Ответ 2:\n", ans2)
    finally:
        gateway.close()


if __name__ == "__main__":
    main()