from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from assistant.config import settings
from assistant.gateway import LLMGatewayClient
from assistant.tools import ToolRegistry

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _resolve_image(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        ws = Path(os.getenv("ASSISTANT_WORKSPACE_DIR", "workspace")).resolve()
        candidate = (ws / path_str).resolve()
        if candidate.exists():
            return candidate
    resolved = p.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Изображение не найдено: '{path_str}'")
    return resolved


def guess_mime_type(path: str | Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return mime
    ext = Path(path).suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "image/jpeg"


def register_vision_tools(registry: ToolRegistry, gateway: LLMGatewayClient) -> None:
    def analyze_image(
            path: str,
            question: str = "Распознай весь текст на изображении и подробно ответь, что на нём.",
    ) -> dict[str, Any]:
        target = _resolve_image(path)
        mime = guess_mime_type(target)
        b64 = base64.b64encode(target.read_bytes()).decode("ascii")

        res = gateway.chat_completion(
            model=settings.default_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
            temperature=0.1,
        )
        answer = (res["choices"][0]["message"].get("content") or "").strip()
        return {"file": target.name, "analysis": answer}

    registry.register(
        name="analyze_image",
        description=(
            "Анализирует и распознаёт текст/уравнения/код на сохранённом изображении (png/jpg/webp) "
            "из папки workspace (например uploads/... или attachments/...)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Относительный путь к картинке в workspace."},
                "question": {"type": "string",
                             "description": "Что нужно распознать, решить или проверить на картинке."},
            },
            "required": ["path"],
        },
        handler=analyze_image,
    )
