from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl
from fastapi import HTTPException, Request

from assistant.config import settings


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> dict:
    """Проверяет подпись HMAC-SHA256 данных из Telegram Mini App."""
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", "")
    if not received_hash:
        raise ValueError("Missing hash in initData")

    auth_date = int(parsed.get("auth_date", "0") or 0)
    if auth_date and (time.time() - auth_date > max_age_sec):
        raise ValueError("Telegram initData expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("Invalid Telegram initData signature")

    user_raw = parsed.get("user")
    if not user_raw:
        raise ValueError("Missing user in initData")
    return json.loads(user_raw)


def require_dashboard_auth(request: Request) -> None:
    """
    Пускает либо:
    1) Авторизованный запрос из Telegram Mini App (валидный X-Telegram-Init-Data + совпадение chat_id),
    2) Локальный запрос с вашего Mac (без прокси-заголовков туннеля и с Host localhost/127.0.0.1),
    3) Запрос с опциональным WEB_DASHBOARD_TOKEN.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if init_data:
        token = settings.telegram_bot_token.strip()
        allowed = settings.telegram_allowed_chat_id.strip()
        if not token or not allowed:
            raise HTTPException(status_code=403, detail="Telegram auth not configured")
        try:
            user = verify_telegram_init_data(init_data, token)
        except Exception as exc:
            raise HTTPException(status_code=403, detail=f"Invalid Telegram auth: {exc}") from exc

        if str(user.get("id", "")) != allowed:
            raise HTTPException(status_code=403, detail="Access denied for this Telegram user")
        return

    web_token = os.getenv("WEB_DASHBOARD_TOKEN", "").strip()
    auth_hdr = request.headers.get("X-Dashboard-Token", "").strip()
    if web_token and auth_hdr == web_token:
        return

    # Разрешаем локальный браузер на Mac (если запрос не пришёл извне через Cloudflare/ngrok туннель)
    forwarded = request.headers.get("X-Forwarded-For") or request.headers.get("CF-Connecting-IP")
    host = (request.headers.get("Host") or "").split(":")[0].lower()
    if not forwarded and host in ("127.0.0.1", "localhost"):
        return

    raise HTTPException(
        status_code=403,
        detail="Требуется авторизация через Telegram Mini App или локальный localhost.",
    )
