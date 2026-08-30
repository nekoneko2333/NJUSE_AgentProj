from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("LLM_API_KEY", "")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    model: str = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    default_workspace: str = os.getenv("DEFAULT_WORKSPACE", "")
    max_turns: int = 18
    command_timeout_seconds: int = 30


settings = Settings()
