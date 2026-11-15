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


settings = Settings()