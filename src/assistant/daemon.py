from __future__ import annotations

from contextlib import suppress
from assistant.telegram_bot import start_telegram_services

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from assistant.observability import log_event
from assistant.runtime import AssistantRuntime


class ChatRequest(BaseModel):
    message: str
    session_id: str = "main"
    channel: str = "cli"


class TaskRequest(BaseModel):
    prompt: str
    session_id: str = "main"
    channel: str = "cli"


runtime: AssistantRuntime | None = None
_waiters: dict[int, asyncio.Future[str]] = {}
_runtime_lock = asyncio.Lock()


async def enqueue_chat_and_wait(session_id: str, message: str, channel: str) -> str:
    rt = _rt()
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    task_id = rt.queue.enqueue(
        prompt=message,
        session_id=session_id,
        channel=channel,
        kind="chat",
    )
    _waiters[task_id] = fut
    return await fut


async def _task_worker(rt: AssistantRuntime) -> None:
    """Обрабатывает очередь задач (чат и фоновые задачи) последовательно."""
    loop = asyncio.get_running_loop()
    while True:
        task = rt.queue.claim_next()
        if task is None:
            await asyncio.sleep(0.25)
            continue

        task_id = int(task["id"])
        session_id = task["session_id"]
        channel = task["channel"]
        kind = task["kind"]
        prompt = task["prompt"]

        try:
            async with _runtime_lock:
                answer = await asyncio.to_thread(rt.run_turn, session_id, prompt, channel)
            rt.queue.complete(task_id, answer)
            log_event("queue.task_done", task_id=task_id, session_id=session_id, channel=channel)

            if kind == "background":
                rt.queue.push_notification(
                    source=f"task#{task_id}",
                    text=f"Задача #{task_id} завершена:\n{answer}",
                )

            fut = _waiters.pop(task_id, None)
            if fut and not fut.done():
                loop.call_soon_threadsafe(fut.set_result, answer)
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            rt.queue.fail(task_id, err_msg)
            log_event("queue.task_failed", level="ERROR", task_id=task_id, error=err_msg)

            if kind == "background":
                rt.queue.push_notification(
                    source=f"task#{task_id}",
                    text=f"Задача #{task_id} завершилась ошибкой: {err_msg}",
                )

            fut = _waiters.pop(task_id, None)
            if fut and not fut.done():
                loop.call_soon_threadsafe(fut.set_exception, exc)


async def _reminder_loop(rt: AssistantRuntime) -> None:
    """Каждые 10 секунд проверяет сработавшие напоминания и кладёт их в уведомления."""
    while True:
        try:
            due = await asyncio.to_thread(rt.reminders.pop_due_reminders)
            for item in due:
                log_event("reminder.triggered", reminder_id=item["id"], text=item["text"])
                rt.queue.push_notification(
                    source="reminder",
                    text=f"Напоминание: {item['text']}",
                )
        except Exception as exc:
            log_event("reminder.loop_error", level="ERROR", error=str(exc))
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global runtime
    runtime = AssistantRuntime()
    worker_task = asyncio.create_task(_task_worker(runtime))
    reminder_task = asyncio.create_task(_reminder_loop(runtime))
    tg_bundle = await start_telegram_services(runtime, enqueue_chat_and_wait, compact_session)
    try:
        yield
    finally:
        if tg_bundle is not None:
            bot, dp, tg_tasks = tg_bundle
            with suppress(Exception):
                await dp.stop_polling()
            for t in tg_tasks:
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t
            await bot.session.close()

        for t in (worker_task, reminder_task):
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
        runtime.close()


app = FastAPI(title="Personal Assistant Daemon", lifespan=lifespan)


def _rt() -> AssistantRuntime:
    if runtime is None:
        raise HTTPException(status_code=503, detail="Runtime not initialized")
    return runtime


@app.get("/health")
def health() -> dict[str, Any]:
    rt = _rt()
    return {"status": "ok", "tools": len(rt.registry.schemas())}


@app.post("/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    reply = await enqueue_chat_and_wait(req.session_id, req.message, req.channel)
    return {"session_id": req.session_id, "reply": reply}


@app.post("/tasks")
def create_task(req: TaskRequest) -> dict[str, Any]:
    rt = _rt()
    task_id = rt.queue.enqueue(
        prompt=req.prompt,
        session_id=req.session_id,
        channel=req.channel,
        kind="background",
    )
    return {"task_id": task_id, "status": "pending"}


@app.get("/tasks")
def list_tasks(limit: int = 15) -> list[dict[str, Any]]:
    return _rt().queue.list_recent(limit=limit)


@app.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    task = _rt().queue.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/notifications/{channel}")
def pop_notifications(channel: str) -> list[dict[str, Any]]:
    return _rt().queue.pop_notifications_for(channel)


@app.get("/history/{session_id}")
def get_history(session_id: str, limit: int = 12) -> dict[str, Any]:
    summary, msgs = _rt().memory.load_session(session_id)
    visible = [
        {"role": m.get("role"), "content": m.get("content")}
        for m in msgs
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return {"session_id": session_id, "summary": summary, "messages": visible[-limit:]}


@app.post("/compact/{session_id}")
async def compact_session(session_id: str) -> dict[str, Any]:
    rt = _rt()
    async with _runtime_lock:
        return await asyncio.to_thread(rt.compact_session, session_id)


@app.get("/stats")
def stats() -> dict[str, Any]:
    return _rt().spend_tracker.summary()


if __name__ == "__main__":
    import os
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("assistant.daemon:app", host=host, port=8080, reload=False)
