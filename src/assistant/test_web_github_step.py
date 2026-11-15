import subprocess

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.gateway import LLMGatewayClient
from assistant.integrations.github_tools import register_github_tools
from assistant.integrations.web_search import register_web_tools
from assistant.observability import init_tracing
from assistant.tools import ToolRegistry


def detect_github_repo() -> str | None:
    """Пробует определить owner/repo из git remote origin."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], text=True
        ).strip()
        if "github.com" not in url:
            return None
        part = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        return part if "/" in part else None
    except Exception:
        return None


def main() -> None:
    init_tracing()
    gateway = LLMGatewayClient()
    registry = ToolRegistry()
    register_web_tools(registry)
    register_github_tools(registry)

    ctx = ContextManager(gateway, max_context_tokens=32_000)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt=(
            "Ты персональный ИИ-ассистент. Для актуального поиска используй web_search, "
            "а для работы с GitHub — github_* инструменты."
        ),
    )

    try:
        print("=== Тест 1: поиск в интернете ===")
        print(
            agent.run_turn(
                "Найди в интернете, какая последняя стабильная версия Python вышла в 2026 году."
            )
        )

        repo_slug = detect_github_repo()
        print(f"\n=== Тест 2: чтение моего GitHub-репозитория ({repo_slug}) ===")
        if repo_slug:
            print(
                agent.run_turn(
                    f"Посмотри мой репозиторий {repo_slug} на GitHub: "
                    "список файлов в корне и 3 последних коммита."
                )
            )

            # print("\n=== Тест 3: ветка + коммит + PR ===")
            # print(
            #     agent.run_turn(
            #         f"В репозитории {repo_slug} создай ветку test-agent-pr от main, "
            #         "добавь в неё файл docs/agent_test.md с текстом 'Проверка записи агентом' "
            #         "и коммитом 'docs: тест записи агентом', а затем открой PR в main."
            #     )
            # )
        else:
            print(
                agent.run_turn(
                    "Посмотри репозиторий BerriAI/litellm на GitHub и назови 3 последних коммита."
                )
            )
    finally:
        gateway.close()


if __name__ == "__main__":
    main()