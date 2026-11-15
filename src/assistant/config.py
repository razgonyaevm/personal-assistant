from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    litellm_base_url: str = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000")
    litellm_api_key: str = os.getenv("LITELLM_VIRTUAL_KEY", "")
    default_model: str = os.getenv("ASSISTANT_MODEL", "assistant-main")
    fast_model: str = os.getenv("ASSISTANT_FAST_MODEL", "assistant-fast")
    embed_model: str = os.getenv("ASSISTANT_EMBED_MODEL", "assistant-embed")

    otel_enabled: bool = os.getenv("OTEL_ENABLED", "true").lower() == "true"
    phoenix_endpoint: str = os.getenv(
        "PHOENIX_OTLP_ENDPOINT", "http://127.0.0.1:6006/v1/traces"
    )
    service_name: str = os.getenv("OTEL_SERVICE_NAME", "personal-assistant")
    log_file: str = os.getenv("ASSISTANT_LOG_FILE", "logs/assistant.jsonl")

    db_path: str = os.getenv("ASSISTANT_DB_PATH", "data/assistant.db")
    workspace_dir: str = os.getenv("ASSISTANT_WORKSPACE_DIR", "workspace")
    daemon_url: str = os.getenv("ASSISTANT_DAEMON_URL", "http://127.0.0.1:8080")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_allowed_chat_id: str = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "")


settings = Settings()
