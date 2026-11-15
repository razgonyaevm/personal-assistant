from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from assistant.agent import AgentLoop
from assistant.config import settings
from assistant.context import ContextManager
from assistant.fs_tools import WorkspaceFS, register_fs_tools
from assistant.gateway import LLMGatewayClient
from assistant.integrations.calendar_tools import CalendarManager, register_calendar_tools
from assistant.integrations.github_tools import register_github_tools
from assistant.integrations.reminders import ReminderManager, register_reminder_tools
from assistant.integrations.web_search import register_web_tools
from assistant.mcp_client import MCPManager
from assistant.memory import MemoryStore, register_memory_tools
from assistant.observability import SpendTracker, init_tracing, log_event
from assistant.sandbox import DockerSandbox, register_sandbox_tools
from assistant.standards import build_standards_prompt, discover_skills, register_skill_tool
from assistant.task_queue import TaskQueue, register_queue_tools
from assistant.tools import ToolRegistry
from assistant.integrations.marketplace_tools import register_marketplace_tools


class AssistantRuntime:
    def __init__(self) -> None:
        init_tracing()

        self.db_path = Path(settings.db_path)
        self.workspace_dir = Path(settings.workspace_dir).resolve()

        self.spend_tracker = SpendTracker(self.db_path)
        self.gateway = LLMGatewayClient(spend_tracker=self.spend_tracker)
        self.memory = MemoryStore(self.db_path, self.gateway)
        self.queue = TaskQueue(self.db_path)

        self.fs = WorkspaceFS(self.workspace_dir)
        self.skills = discover_skills(self.workspace_dir / "skills")
        self.sandbox = DockerSandbox("assistant-sandbox")
        self.calendar = CalendarManager(self.db_path)
        self.reminders = ReminderManager(self.db_path)
        self.mcp = MCPManager()

        self.registry = ToolRegistry()
        register_fs_tools(self.registry, self.fs)
        register_skill_tool(self.registry, self.skills)
        register_sandbox_tools(self.registry, self.sandbox)
        register_memory_tools(self.registry, self.memory)
        register_queue_tools(self.registry, self.queue)
        register_web_tools(self.registry)
        register_github_tools(self.registry)
        register_calendar_tools(self.registry, self.calendar)
        register_reminder_tools(self.registry, self.reminders)
        register_marketplace_tools(self.registry)

        self.registry.register(
            name="get_current_time",
            description="Возвращает текущие локальные дату и время (ISO).",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: datetime.now().astimezone().isoformat(),
        )

        mcp_tools = self.mcp.connect_from_config("mcp_servers.json", self.registry)
        log_event(
            "runtime.initialized",
            mcp_tools=mcp_tools,
            skills=list(self.skills.keys()),
            total_tools=len(self.registry.schemas()),
        )

    def build_system_prompt(self) -> str:
        standards = build_standards_prompt(self.workspace_dir, self.skills)
        base = (
            "Ты персональный ИИ-ассистент.\n"
            "Доступные возможности:\n"
            "- Долговременная память (memory_save / memory_search): сохраняй важные факты и предпочтения.\n"
            "- Файловая система workspace (list_directory, read_file, write_file, edit_file).\n"
            "- Изолированная Docker-песочница (run_shell для bash/pwsh, run_python, get_system_info).\n"
            "- MCP-инструменты, поиск в интернете (web_search, fetch_webpage), GitHub API.\n"
            "- Личный календарь, внешний календарь .ics и напоминания.\n"
            "Перед планированием дат или расписания проверяй текущее время через get_current_time."
        )
        return f"{base}\n\n{standards}" if standards else base

    def run_turn(self, session_id: str, user_input: str, channel: str) -> str:
        self.gateway.channel = channel
        self.gateway.session_id = session_id

        ctx = ContextManager(self.gateway, max_context_tokens=32_000)
        agent = AgentLoop(
            gateway=self.gateway,
            tools=self.registry,
            context_manager=ctx,
            system_prompt=self.build_system_prompt(),
            memory_store=self.memory,
            session_id=session_id,
        )
        return agent.run_turn(user_input)

    def compact_session(self, session_id: str) -> dict[str, Any]:
        summary, msgs = self.memory.load_session(session_id)
        if len(msgs) < 4:
            return {"status": "skipped", "reason": "Слишком мало сообщений для сжатия."}

        ctx = ContextManager(self.gateway, max_context_tokens=32_000, keep_recent_messages=4)
        ctx.summary = summary
        full_msgs = [{"role": "system", "content": self.build_system_prompt()}, *msgs]
        compacted = ctx.compact(full_msgs)
        non_system = [m for m in compacted if m.get("role") != "system"]
        self.memory.save_session_state(session_id, ctx.summary, non_system)
        return {"status": "ok", "messages_after": len(non_system), "summary": ctx.summary}

    def close(self) -> None:
        self.mcp.close_all()
        self.gateway.close()
