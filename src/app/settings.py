
"""Environment-driven application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass


@dataclass(frozen=True)
class Settings:
    """Store runtime settings used across the application."""

    app_name: str
    app_env: str
    log_level: str
    unique_api_base_url: str
    unique_api_version: str
    unique_app_id: str
    unique_app_key: str
    unique_auth_company_id: str
    unique_auth_user_id: str
    unique_model_name: str
    unique_agent_max_iterations: int
    unique_max_tool_calls_per_iteration: int
    unique_max_history_tokens: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with safe defaults."""
        return cls(
            app_name=os.getenv("APP_NAME", "relationship-manager-agentic-rag-poc"),
            app_env=os.getenv("APP_ENV", "local"),
            log_level=os.getenv("LOG_LEVEL", "DEBUG").upper(),
            unique_api_base_url=os.getenv("UNIQUE_API_BASE_URL", "").strip(),
            unique_api_version=os.getenv("UNIQUE_API_VERSION", "2023-12-06").strip(),
            unique_app_id=os.getenv("UNIQUE_APP_ID", "").strip(),
            unique_app_key=os.getenv("UNIQUE_APP_KEY", os.getenv("UNIQUE_API_KEY", "")).strip(),
            unique_auth_company_id=os.getenv("UNIQUE_AUTH_COMPANY_ID", "").strip(),
            unique_auth_user_id=os.getenv("UNIQUE_AUTH_USER_ID", "").strip(),
            unique_model_name=os.getenv("UNIQUE_MODEL_NAME", "AZURE_GPT_4o_2024_1120").strip(),
            unique_agent_max_iterations=int(os.getenv("UNIQUE_AGENT_MAX_ITERATIONS", "3")),
            unique_max_tool_calls_per_iteration=int(os.getenv("UNIQUE_MAX_TOOL_CALLS_PER_ITERATION", "3")),
            unique_max_history_tokens=int(os.getenv("UNIQUE_MAX_HISTORY_TOKENS", "6000")),
        )