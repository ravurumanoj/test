
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
    # ── Session / ChatService integration ──────────────────────────────────
    # UNIQUE_ASSISTANT_ID — the Unique assistant (space) ID; required for
    #   Message.create when persisting conversation turns.
    # UNIQUE_DEFAULT_SESSION_ID — used as chatId when the request omits session_id.
    unique_assistant_id: str
    unique_default_session_id: str
    # ── MCP (Model Context Protocol) integration ─────────────────────────────
    # All MCP settings are OPTIONAL. When mcp_server_url is empty the MCP
    # Manager is disabled and the application behaves exactly as before.
    mcp_enabled: bool
    mcp_server_url: str
    mcp_auth_header: str
    mcp_auth_value: str
    mcp_timeout_seconds: int
    mcp_protocol_version: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with safe defaults."""
        # ── MCP enablement resolution ────────────────────────────────────────
        # Set MCP_SERVER_URL to point the app at YOUR running MCP server. This is
        # the ONLY value you must change to connect a different MCP server.
        # MCP_ENABLED accepts: true/false/1/0/yes/no/on/off, or "auto" (default)
        # which enables MCP automatically whenever MCP_SERVER_URL is provided.
        mcp_server_url = os.getenv("MCP_SERVER_URL", "").strip()
        mcp_enabled_flag = os.getenv("MCP_ENABLED", "auto").strip().lower()
        # Default ("auto"): enable MCP only when a server URL is configured.
        mcp_enabled = bool(mcp_server_url)
        if mcp_enabled_flag in ("1", "true", "yes", "on"):
            mcp_enabled = True
        elif mcp_enabled_flag in ("0", "false", "no", "off"):
            mcp_enabled = False

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
            # ── Session / ChatService integration ────────────────────────────
            unique_assistant_id=os.getenv("UNIQUE_ASSISTANT_ID", "").strip(),
            unique_default_session_id=os.getenv("UNIQUE_DEFAULT_SESSION_ID", "poc-demo-session-001").strip(),
            # ── MCP integration (see resolution logic above) ─────────────────
            mcp_enabled=mcp_enabled,
            mcp_server_url=mcp_server_url,
            # Optional auth header sent on every MCP request, e.g.
            #   MCP_AUTH_HEADER=Authorization  MCP_AUTH_VALUE="Bearer <token>"
            mcp_auth_header=os.getenv("MCP_AUTH_HEADER", "").strip(),
            mcp_auth_value=os.getenv("MCP_AUTH_VALUE", "").strip(),
            mcp_timeout_seconds=int(os.getenv("MCP_TIMEOUT_SECONDS", "30")),
            mcp_protocol_version=os.getenv("MCP_PROTOCOL_VERSION", "2025-06-18").strip(),
        )