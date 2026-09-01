from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
DEFAULT_DATABASE_PATH = str(Path(__file__).resolve().parents[3] / "data" / "mosscode.db")


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("LLM_API_KEY", "")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    model: str = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    default_workspace: str = os.getenv("DEFAULT_WORKSPACE", "")
    max_turns: int = 18
    command_timeout_seconds: int = 30
    database_path: str = os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATH
    context_budget_chars: int = int(os.getenv("CONTEXT_BUDGET_CHARS", "12000"))
    context_recent_turns: int = int(os.getenv("CONTEXT_RECENT_TURNS", "3"))
    context_summary_chars: int = int(os.getenv("CONTEXT_SUMMARY_CHARS", "4000"))
    app_username: str = os.getenv("APP_USERNAME", "moss")
    app_password: str = os.getenv("APP_PASSWORD", "mosscode")
    approval_timeout_seconds: int = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "180"))


settings = Settings()
