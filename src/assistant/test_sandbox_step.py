from pathlib import Path

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.fs_tools import WorkspaceFS, register_fs_tools
from assistant.gateway import LLMGatewayClient
from assistant.sandbox import DockerSandbox, register_sandbox_tools
from assistant.standards import build_standards_prompt, discover_skills, register_skill_tool
from assistant.tools import ToolRegistry


def main() -> None:
    workspace = Path("workspace").resolve()
    fs = WorkspaceFS(workspace)
    skills = discover_skills(workspace / "skills")
    sandbox = DockerSandbox("assistant-sandbox")

    registry = ToolRegistry()
    register_fs_tools(registry, fs)
    register_skill_tool(registry, skills)
    register_sandbox_tools(registry, sandbox)

    system_prompt = (
            "Ты персональный ИИ-ассистент. Все shell-команды и код выполняются в изолированной "
            "Docker-песочнице в /workspace.\n\n"
            + build_standards_prompt(workspace, skills)
    )

    gateway = LLMGatewayClient()
    ctx = ContextManager(gateway, max_context_tokens=32_000)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt=system_prompt,
    )

    try:
        print("=== Тест 1: ОС, bash и PowerShell 7 в песочнице ===")
        ans = agent.run_turn(
            "Проверь информацию о системе, выполни 'ls -la' через bash в песочнице "
            "и узнай точную версию PowerShell через pwsh."
        )
        print("Ответ 1:\n", ans)

        print("\n=== Тест 2: Запуск Python в песочнице и сохранение результата ===")
        ans2 = agent.run_turn(
            "Выполни Python-код в песочнице, который вычислит первые 10 чисел Фибоначчи "
            "и сохранит их в файл fib.txt в /workspace, а потом прочитай этот файл через read_file."
        )
        print("Ответ 2:\n", ans2)
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
