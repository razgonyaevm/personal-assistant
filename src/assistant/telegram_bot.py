from __future__ import annotations

import html
import re
import io

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from assistant.config import settings
from assistant.observability import log_event
from assistant.runtime import AssistantRuntime


def _markdown_to_telegram_html(text: str) -> str:
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _save_code_block(match: re.Match[str]) -> str:
        code = html.escape(match.group(1).strip("\n"), quote=False)
        idx = len(code_blocks)
        code_blocks.append(f"<pre><code>{code}</code></pre>")
        return f"\x00CODEBLOCK{idx}\x00"

    def _save_inline_code(match: re.Match[str]) -> str:
        code = html.escape(match.group(1), quote=False)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{code}</code>")
        return f"\x00INLINECODE{idx}\x00"

    # 1. Сохраняем блоки кода до экранирования
    text = re.sub(
        r"```(?:[A-Za-z0-9_+-]*)\n?(.*?)```", _save_code_block, text, flags=re.DOTALL
    )
    text = re.sub(r"`([^`\n]+?)`", _save_inline_code, text)

    # 2. Экранируем <, >, & в обычном тексте
    text = html.escape(text, quote=False)

    # 3. Заголовки ### -> жирный текст, разделители --- -> ———
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^-{3,}\s*$", "———", text)

    # 4. Списки * / - -> аккуратные маркеры •
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)

    # 5. Ссылки [текст](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)

    # 6. Жирный (**text**) и курсив (*text*), не ломая snake_case
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)

    # 7. Возвращаем код
    for idx, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{idx}\x00", code)

    return text


async def _send_chunked(message: Message, text: str, chunk_size: int = 3300) -> None:
    clean = (text or "").strip() or "(пустой ответ)"
    chunks: list[str] = []
    current = ""
    for line in clean.splitlines(keepends=True):
        if len(current) + len(line) > chunk_size and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    for chunk in chunks:
        formatted = _markdown_to_telegram_html(chunk)
        try:
            await message.answer(
                formatted, parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception:
            await message.answer(chunk)


async def _keep_typing(bot: Bot, chat_id: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        with suppress(Exception):
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


def _is_authorized(message: Message) -> tuple[bool, str | None]:
    allowed = settings.telegram_allowed_chat_id.strip()
    chat_id = str(message.chat.id)
    if not allowed:
        return (
            False,
            f"Доступ пока не настроен.\n"
            f"Ваш chat_id: {chat_id}\n"
            f"Добавьте в .env:\nTELEGRAM_ALLOWED_CHAT_ID={chat_id}\n"
            f"и перезапустите python -m assistant.daemon",
        )
    if chat_id != allowed:
        return False, "Доступ запрещён."
    return True, None


async def _notification_loop(bot: Bot, rt: AssistantRuntime) -> None:
    """Доставляет в Telegram сработавшие напоминания и результаты фоновых задач."""
    while True:
        try:
            allowed = settings.telegram_allowed_chat_id.strip()
            if allowed:
                notes = rt.queue.pop_notifications_for("telegram")
                for n in notes:
                    header = (
                        "**⏰ Напоминание**"
                        if n["source"] == "reminder"
                        else f"**📌 {n['source']}**"
                    )
                    full_text = f"{header}\n\n{n['text']}"

                    chunks: list[str] = []
                    current = ""
                    for line in full_text.splitlines(keepends=True):
                        if len(current) + len(line) > 3300 and current:
                            chunks.append(current)
                            current = line
                        else:
                            current += line
                    if current:
                        chunks.append(current)

                    for chunk in chunks:
                        try:
                            await bot.send_message(
                                chat_id=int(allowed),
                                text=_markdown_to_telegram_html(chunk),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        except Exception:
                            await bot.send_message(chat_id=int(allowed), text=chunk)
        except Exception as exc:
            log_event("telegram.notify_error", level="ERROR", error=str(exc))
        await asyncio.sleep(3.0)


def build_dispatcher(
        rt: AssistantRuntime,
        run_chat: Callable[[str, str, str], Awaitable[str]],
        compact_fn: Callable[[str], Awaitable[dict[str, Any]]],
) -> Dispatcher:
    router = Router()
    chat_sessions: dict[int, str] = {}

    def get_session(chat_id: int) -> str:
        return chat_sessions.get(chat_id, "main")

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        sid = get_session(message.chat.id)
        await message.answer(
            f"Personal Assistant на связи (сессия: {sid}).\n\n"
            "Команды:\n"
            "/task <текст> — поставить фоновую задачу в общую очередь\n"
            "/tasks — список последних задач очереди\n"
            "/history — последние сообщения общей сессии\n"
            "/stats — расход токенов и USD\n"
            "/compact — сжать контекст сессии\n"
            "/session <имя> — переключить сессию (по умолчанию main)\n"
            "/reset — очистить историю текущей сессии\n"
            "/id — показать chat_id"
        )

    @router.message(F.document)
    async def handle_document(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return

        doc = message.document
        if not doc or not doc.file_name:
            return

        file_name = Path(doc.file_name).name
        target_path = rt.workspace_dir / file_name

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id, stop_event))
        try:
            # 1. Скачиваем файл в workspace
            await message.bot.download(doc.file_id, destination=target_path)

            caption = (message.caption or "").strip()
            if caption:
                # Чёткий запрос пользователя: отвечаем СТРОГО на него
                prompt = (
                    f"Файл '{file_name}' сохранён в workspace.\n"
                    f"Запрос пользователя: \"{caption}\"\n"
                    f"Инструкция: Ответь строго и точно на поставленный вопрос по этому файлу, "
                    f"не делай лишних выжимок и пересказов, если об этом прямо не просили."
                )
            else:
                # Если файл отправлен без текста: просто смотрим инфо и ждём указаний
                prompt = (
                    f"Файл '{file_name}' успешно сохранён в workspace. "
                    f"Посмотри базовую информацию (pdf_info) и просто подтверди получение: "
                    f"назови количество страниц и спроси, какую задачу выполнить."
                )

            sid = get_session(message.chat.id)
            reply = await run_chat(sid, prompt, "telegram")
            await _send_chunked(message, reply)
        except Exception as exc:
            await message.answer(f"Ошибка при сохранении/обработке документа: {exc}")
        finally:
            stop_event.set()
            with suppress(asyncio.CancelledError):
                await typing_task

    @router.message(F.voice | F.audio)
    async def handle_voice_or_audio(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return

        audio_obj = message.voice or message.audio
        if not audio_obj:
            return

        filename = "voice.ogg" if message.voice else (message.audio.file_name or "audio.mp3")

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id, stop_event))
        try:
            voice_buf = io.BytesIO()
            await message.bot.download(audio_obj.file_id, destination=voice_buf)
            audio_bytes = voice_buf.getvalue()

            sid = get_session(message.chat.id)
            transcribed_text = await asyncio.to_thread(
                rt.gateway.transcribe_audio,
                audio_bytes,
                filename,
            )
            clean_text = (transcribed_text or "").strip()
            if not clean_text:
                await message.answer("Не удалось разобрать слова в аудиосообщении.")
                return

            # Показываем распознанный текст
            await message.answer(f"🗣️ <i>«{html.escape(clean_text)}»</i>", parse_mode="HTML")

            # Передаём распознанный текст в цикл агента
            reply = await run_chat(sid, clean_text, "telegram")
            await _send_chunked(message, reply)
        except Exception as exc:
            await message.answer(f"Ошибка при обработке голосового сообщения: {exc}")
        finally:
            stop_event.set()
            with suppress(asyncio.CancelledError):
                await typing_task

    @router.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        await message.answer(f"Ваш chat_id: {message.chat.id}")

    @router.message(Command("reset"))
    async def cmd_reset(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        sid = get_session(message.chat.id)
        rt.memory.clear_session(sid)
        await message.answer(f"История сессии '{sid}' очищена (долговременная память сохранена).")

    @router.message(Command("session"))
    async def cmd_session(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        parts = (message.text or "").split(maxsplit=1)
        new_sid = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "main"
        chat_sessions[message.chat.id] = new_sid
        await message.answer(f"Текущая сессия переключена на: {new_sid}")

    @router.message(Command("tasks"))
    async def cmd_tasks(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        tasks = rt.queue.list_recent(limit=10)
        if not tasks:
            await message.answer("Очередь задач пуста.")
            return
        lines = [
            f"#{t['id']} [{t['status']}] ({t['channel']}/{t['kind']}): {t['prompt'][:55]}"
            for t in tasks
        ]
        await _send_chunked(message, "Общая очередь задач:\n\n" + "\n".join(lines))

    @router.message(Command("task"))
    async def cmd_task(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Использование: /task <текст задачи>")
            return
        sid = get_session(message.chat.id)
        task_id = rt.queue.enqueue(
            prompt=parts[1].strip(),
            session_id=sid,
            channel="telegram",
            kind="background",
        )
        await message.answer(f"Задача #{task_id} поставлена в общую очередь (сессия {sid}).")

    @router.message(Command("history"))
    async def cmd_history(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        sid = get_session(message.chat.id)
        summary, msgs = rt.memory.load_session(sid)
        visible = [
            m for m in msgs if m.get("role") in ("user", "assistant") and m.get("content")
        ][-8:]
        lines = []
        if summary:
            lines.append(f"[Сжатый контекст]\n{summary}\n")
        for m in visible:
            who = "Вы" if m["role"] == "user" else "Ассистент"
            lines.append(f"{who}: {m['content']}")
        await _send_chunked(message, "\n\n".join(lines) or "История сессии пуста.")

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        s = rt.spend_tracker.summary()
        lines = [
            f"Запросов: {s['requests']}",
            f"Токенов: {s['total_tokens']}",
            f"Расход: ${s['total_cost_usd']}",
            f"Средняя задержка: {s['avg_latency_ms']} мс",
        ]
        for m in s.get("by_model", []):
            lines.append(f"- {m['model']}: {m['requests']} запр., {m['tokens']} ток., ${round(m['cost_usd'], 6)}")
        await message.answer("\n".join(lines))

    @router.message(Command("compact"))
    async def cmd_compact(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        sid = get_session(message.chat.id)
        res = await compact_fn(sid)
        await message.answer(f"Compaction: {res.get('status')} ({res.get('reason') or res.get('messages_after', '')})")

    @router.message(F.text)
    async def handle_text(message: Message) -> None:
        ok, err = _is_authorized(message)
        if not ok:
            await message.answer(err or "Доступ запрещён.")
            return
        text = (message.text or "").strip()
        if not text:
            return

        sid = get_session(message.chat.id)
        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id, stop_event))
        try:
            reply = await run_chat(sid, text, "telegram")
            await _send_chunked(message, reply)
        except Exception as exc:
            await message.answer(f"Ошибка обработки: {exc}")
        finally:
            stop_event.set()
            with suppress(asyncio.CancelledError):
                await typing_task

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def start_telegram_services(
        rt: AssistantRuntime,
        run_chat: Callable[[str, str, str], Awaitable[str]],
        compact_fn: Callable[[str], Awaitable[dict[str, Any]]],
) -> tuple[Bot, Dispatcher, list[asyncio.Task[Any]]] | None:
    token = settings.telegram_bot_token.strip()
    if not token:
        log_event("telegram.disabled", reason="TELEGRAM_BOT_TOKEN не задан")
        return None

    bot = Bot(token=token)
    dp = build_dispatcher(rt, run_chat, compact_fn)

    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Список команд"),
            BotCommand(command="task", description="Поставить фоновую задачу"),
            BotCommand(command="tasks", description="Очередь задач"),
            BotCommand(command="history", description="История общей сессии"),
            BotCommand(command="stats", description="Расход токенов и USD"),
            BotCommand(command="compact", description="Сжать контекст"),
            BotCommand(command="reset", description="Очистить историю текущей сессии"),
            BotCommand(command="session", description="Переключить сессию"),
        ]
    )

    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )
    notify_task = asyncio.create_task(_notification_loop(bot, rt))
    log_event("telegram.started")
    return bot, dp, [polling_task, notify_task]
