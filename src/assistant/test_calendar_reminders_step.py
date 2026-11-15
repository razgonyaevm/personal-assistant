from datetime import datetime
import os
from pathlib import Path
from dotenv import load_dotenv

from assistant.agent import AgentLoop
from assistant.context import ContextManager
from assistant.gateway import LLMGatewayClient
from assistant.integrations.calendar_tools import CalendarManager, register_calendar_tools
from assistant.integrations.reminders import ReminderManager, register_reminder_tools
from assistant.observability import init_tracing
from assistant.tools import ToolRegistry


def create_sample_ics(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    ics_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//University Schedule//EN
BEGIN:VEVENT
UID:ai-lecture-1@uni.edu
DTSTART:{today}T100000
DTEND:{today}T113000
RRULE:FREQ=WEEKLY;COUNT=4
SUMMARY:Лекция: AI в разработке
LOCATION:Аудитория 305
END:VEVENT
BEGIN:VEVENT
UID:football-match-1@sport.org
DTSTART:{today}T200000
DTEND:{today}T220000
SUMMARY:Матч: Реал — Барселона
LOCATION:Стадион
END:VEVENT
END:VCALENDAR
"""
    path.write_text(ics_content, encoding="utf-8")


def main() -> None:
    load_dotenv()
    init_tracing()

    db_path = Path("data/test_cal_rem.db")
    if db_path.exists():
        db_path.unlink()

    env_ics = os.getenv("EXTERNAL_ICS_URL", "").strip()
    if env_ics:
        print(f"Используем внешний календарь из .env: {env_ics}")
        cal = CalendarManager(db_path=db_path)
    else:
        sample_ics = Path("workspace/sample_external.ics")
        create_sample_ics(sample_ics)
        print(f"EXTERNAL_ICS_URL не задан, используем тестовый: {sample_ics}")
        cal = CalendarManager(db_path=db_path, default_external_ics=str(sample_ics))

    rem = ReminderManager(db_path=db_path)

    registry = ToolRegistry()
    register_calendar_tools(registry, cal)
    register_reminder_tools(registry, rem)
    registry.register(
        name="get_current_time",
        description="Возвращает текущие дату и время (ISO).",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: datetime.now().astimezone().isoformat(),
    )

    gateway = LLMGatewayClient()
    ctx = ContextManager(gateway, max_context_tokens=32_000)
    agent = AgentLoop(
        gateway=gateway,
        tools=registry,
        context_manager=ctx,
        system_prompt="Ты персональный ассистент. Управляй календарями и напоминаниями через инструменты.",
    )

    try:
        ans = agent.run_turn(
            "1) Посмотри моё внешнее расписание из .ics на ближайшие 7 дней. "
            "2) Добавь в личный календарь на завтра в 15:00 'Созвон по лабораторной'. "
            "3) Поставь ежедневное напоминание в 09:30 'Проверить задачи на день' "
            "и ещё одно разовое напоминание через 0 минут 'Тест срабатывания'."
        )
        print("Ответ агента:\n", ans)

        print("\n=== Проверка сработавших напоминаний (pop_due_reminders) ===")
        print(rem.pop_due_reminders())
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
