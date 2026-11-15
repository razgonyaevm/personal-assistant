from __future__ import annotations

from pathlib import Path
import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import completion_is_selected, has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from assistant.config import settings

console = Console()

# (команда, описание, нужен_ли_аргумент)
COMMANDS: list[tuple[str, str, bool]] = [
    ("/task", "Поставить фоновую задачу в общую очередь", True),
    ("/tasks", "Показать общую очередь задач", False),
    ("/show", "Показать результат задачи по ID", True),
    ("/history", "Показать историю текущей сессии", False),
    ("/stats", "Статистика токенов и расхода", False),
    ("/compact", "Сжать историю текущей сессии", False),
    ("/session", "Переключить сессию (по умолчанию main)", True),
    ("/help", "Список доступных команд", False),
    ("/exit", "Выйти из CLI", False),
]


class SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Показываем подсказки только в начале строки, пока не нажат пробел
        if not text.startswith("/") or " " in text:
            return
        for cmd, desc, takes_arg in COMMANDS:
            if cmd.startswith(text):
                insert_text = f"{cmd} " if takes_arg else cmd
                yield Completion(
                    text=insert_text,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


def _build_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter", filter=has_completions & completion_is_selected)
    def _(event) -> None:
        buf = event.current_buffer
        cs = buf.complete_state
        if cs and cs.current_completion:
            chosen = cs.current_completion.text
            buf.apply_completion(cs.current_completion)
            # Если команда не требует аргумента (без пробела в конце) — сразу отправляем
            if not chosen.endswith(" "):
                buf.validate_and_handle()

    @kb.add("tab", filter=has_completions)
    def _(event) -> None:
        buf = event.current_buffer
        cs = buf.complete_state
        if cs and cs.completions:
            comp = cs.current_completion or cs.completions[0]
            buf.apply_completion(comp)

    return kb


CLI_STYLE = Style.from_dict(
    {
        "completion-menu": "bg:default",
        "completion-menu.completion": "bg:default ansiblue bold",
        "completion-menu.completion.current": "reverse bold",
        "completion-menu.meta.completion": "bg:default ansibrightblack",
        "completion-menu.meta.completion.current": "reverse",
        "scrollbar.background": "bg:default",
        "scrollbar.button": "bg:ansibrightblack",
    }
)


def print_banner(session_id: str, tools_count: int) -> None:
    console.print(
        Panel(
            "[bold cyan]Personal Assistant CLI[/bold cyan]\n"
            f"[dim]Сессия: {session_id} | Инструментов: {tools_count} | Сервер: {settings.daemon_url} | Введите / для команд[/dim]",
            border_style="cyan",
        )
    )


def show_notifications(client: httpx.Client) -> None:
    notes = client.get("/notifications/cli").json()
    for n in notes:
        console.print(
            Panel(n["text"], title=f"Уведомление ({n['source']})", border_style="yellow")
        )


def show_tasks(client: httpx.Client) -> None:
    tasks = client.get("/tasks", params={"limit": 12}).json()
    table = Table(title="Общая очередь задач")
    table.add_column("ID", style="cyan", width=5)
    table.add_column("Канал", width=10)
    table.add_column("Тип", width=11)
    table.add_column("Статус", width=10)
    table.add_column("Задача")
    for t in tasks:
        table.add_row(str(t["id"]), t["channel"], t["kind"], t["status"], t["prompt"][:60])
    console.print(table)


def show_history(client: httpx.Client, session_id: str) -> None:
    data = client.get(f"/history/{session_id}", params={"limit": 10}).json()
    if data.get("summary"):
        console.print(Panel(data["summary"], title="Сжатый контекст", border_style="yellow"))
    for msg in data.get("messages", []):
        role = "Вы" if msg["role"] == "user" else "Ассистент"
        color = "green" if msg["role"] == "user" else "cyan"
        console.print(f"[bold {color}]{role}:[/bold {color}] {msg['content']}")


def main() -> None:
    session_id = "main"
    client = httpx.Client(base_url=settings.daemon_url, timeout=180.0)

    try:
        health = client.get("/health").json()
        if health.get("status") != "ok":
            raise RuntimeError("Некорректный ответ сервера")
        tools_count = int(health.get("tools", 0))
    except Exception as exc:
        console.print(
            f"[red]Не удалось подключиться к daemon ({settings.daemon_url}): {exc}[/red]\n"
            "Сначала запустите в отдельном терминале: [bold]python -m assistant.daemon[/bold]"
        )
        return

    history_file = Path("data/cli_history.txt")
    history_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_session = PromptSession(
        history=FileHistory(str(history_file)),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        reserve_space_for_menu=6,
        key_bindings=_build_key_bindings(),
        style=CLI_STYLE,
    )

    print_banner(session_id, tools_count)

    try:
        while True:
            show_notifications(client)
            try:
                raw_text = prompt_session.prompt(f"\nВы ({session_id}) > ")
                user_text = raw_text.encode("utf-8", errors="ignore").decode("utf-8").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nДо связи!")
                break

            if not user_text:
                continue
            if user_text in ("/exit", "/quit"):
                console.print("До связи!")
                break

            if user_text == "/help":
                for cmd, desc, takes_arg in COMMANDS:
                    arg_hint = " <...>" if takes_arg else ""
                    console.print(f"[cyan]{cmd}{arg_hint:<7}[/cyan] — {desc}")
                continue

            if user_text == "/tasks":
                show_tasks(client)
                continue

            if user_text.startswith("/show "):
                tid = user_text.split(maxsplit=1)[1].strip()
                resp = client.get(f"/tasks/{tid}")
                if resp.status_code == 404:
                    console.print("[red]Задача не найдена[/red]")
                else:
                    t = resp.json()
                    console.print(
                        Panel(
                            f"Статус: [bold]{t['status']}[/bold]\n\n"
                            f"{t.get('result') or t.get('error') or '(ещё выполняется)'}",
                            title=f"Задача #{t['id']} ({t['channel']})",
                        )
                    )
                continue

            if user_text.startswith("/task "):
                prompt = user_text[6:].strip()
                res = client.post(
                    "/tasks",
                    json={"prompt": prompt, "session_id": session_id, "channel": "cli"},
                ).json()
                console.print(f"[green]Поставлена фоновая задача #{res['task_id']} в очередь.[/green]")
                continue

            if user_text == "/history":
                show_history(client, session_id)
                continue

            if user_text == "/stats":
                console.print(client.get("/stats").json())
                continue

            if user_text == "/compact":
                console.print(client.post(f"/compact/{session_id}").json())
                continue

            if user_text.startswith("/session "):
                session_id = user_text.split(maxsplit=1)[1].strip() or "main"
                print_banner(session_id, tools_count)
                continue

            try:
                with console.status("[cyan]Думаю...[/cyan]"):
                    resp = client.post(
                        "/chat",
                        json={"message": user_text, "session_id": session_id, "channel": "cli"},
                    )
                    resp.raise_for_status()
                    res = resp.json()

                console.print(
                    Panel(
                        Markdown(res["reply"]),
                        title="[bold cyan]Ассистент[/bold cyan]",
                        border_style="cyan",
                    )
                )
            except Exception as exc:
                console.print(f"[red]Ошибка запроса: {exc}[/red]")
    finally:
        client.close()


if __name__ == "__main__":
    main()
