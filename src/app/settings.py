"""Environment-driven application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv

    project_root = Path(__file__).resolve().parents[2]
    local_env_path = project_root / ".env"
    # deployed_env_path = Path("/usr/local/config/.env")
    environment = os.getenv("APP_ENV", "local").strip().lower()
    # env_path = local_env_path if environment == "local" else deployed_env_path
    if environment == "local":
        load_dotenv(local_env_path, override=False)
    # load_dotenv(env_path, override=False)
except ImportError:
    pass


@dataclass(frozen=True)
class Settings:
    """Store runtime settings used across the application."""

    app_name: str
    app_env: str
    log_level: str
    # ── Log file persistence ─────────────────────────────────────────
    # LOG_FILE — path to the rotating log file. Empty disables file logging
    #   (console only). LOG_MAX_BYTES / LOG_BACKUP_COUNT control rotation.
    log_file: str
    log_max_bytes: int
    log_backup_count: int
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
    # ── LLM call tuning (timeouts / token limits) 
    unique_llm_timeout_seconds: int
    unique_llm_max_tokens: int
    unique_http_client_timeout_seconds: int
    # ── Session / ChatService integration ──────────────────────────────────
    # UNIQUE_ASSISTANT_ID — the Unique assistant (space) ID; required for
    #   Message.create when persisting conversation turns.
    # UNIQUE_DEFAULT_SESSION_ID — used as chatId when the request omits session_id.
    unique_assistant_id: str
    unique_default_session_id: str
    # ── Webhook (Unique AI space integration) ───────────────────────────────
    # UNIQUE_WEBHOOK_ENDPOINT_SECRET — HMAC secret Unique signs each webhook with.
    #   When empty, signature verification is skipped (POC only; log a warning).
    # UNIQUE_DEFAULT_CUSTOMER_ID — customer_id used for webhook-driven queries when
    #   the event payload carries no explicit customer identifier.
    # UNIQUE_DEFAULT_PORTFOLIO_ID — portfolio_id used for statement tool calls when
    #   the request/event carries no explicit portfolio identifier (single-account
    #   statement POC; matches data/portfolio_data.json's portfolio_id).
    unique_webhook_endpoint_secret: str
    unique_default_customer_id: str
    unique_default_portfolio_id: str
    # ── MCP (Model Context Protocol) integration ─────────────────────────────
    # All MCP settings are OPTIONAL. When mcp_server_url is empty the MCP
    # Manager is disabled and the application behaves exactly as before.
    mcp_enabled: bool
    mcp_server_url: str
    mcp_auth_header: str
    mcp_auth_value: str
    mcp_timeout_seconds: int
    mcp_protocol_version: str
    sse_enabled: bool
    sse_webhook_url: str
    sse_max_concurrent: int
    # ── SSE upstream event-stream connection ────────────────────────────────
    # These were previously referenced by sse_listener.py but never declared
    # here, so the SSE background task crashed with AttributeError the instant
    # it started (SSE_ENABLED defaults to true) — the webhook-driven chat flow
    # was silently dead on every startup. sse_api_base defaults to the origin
    # (scheme+host, no path) of UNIQUE_API_BASE_URL since the event-socket
    # endpoint lives on the same host but under a different path.
    sse_api_base: str
    sse_api_key: str
    sse_app_id: str
    sse_company_id: str
    sse_ca_bundle: str
    # Field with a default MUST be declared last in a frozen dataclass.
    subscriptions: tuple[str, ...] = ()

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

        # ── SSE upstream connection resolution ───────────────────────────────
        # sse_api_base defaults to the origin (scheme+host) of UNIQUE_API_BASE_URL
        # with any path stripped, since the event-socket endpoint lives on the
        # same host but under /public/event-socket/... rather than /public/chat.
        unique_api_base_url = os.getenv("UNIQUE_API_BASE_URL", "").strip()
        split_base = urlsplit(unique_api_base_url)
        derived_sse_api_base = urlunsplit((split_base.scheme, split_base.netloc, "", "", "")) if split_base.netloc else ""

        return cls(
            app_name=os.getenv("APP_NAME", "relationship-manager-agentic-rag-poc"),
            app_env=os.getenv("APP_ENV", "local"),
            log_level=os.getenv("LOG_LEVEL", "DEBUG").upper(),
            log_file=os.getenv("LOG_FILE", "logs/app.log").strip(),
            log_max_bytes=int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024))),
            log_backup_count=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            unique_api_base_url=unique_api_base_url,
            unique_api_version=os.getenv("UNIQUE_API_VERSION", "2023-12-06").strip(),
            unique_app_id=os.getenv("UNIQUE_APP_ID", "").strip(),
            unique_app_key=os.getenv("UNIQUE_APP_KEY", os.getenv("UNIQUE_API_KEY", "")).strip(),
            unique_auth_company_id=os.getenv("UNIQUE_AUTH_COMPANY_ID", "").strip(),
            unique_auth_user_id=os.getenv("UNIQUE_AUTH_USER_ID", "").strip(),
            unique_model_name=os.getenv("UNIQUE_MODEL_NAME", "AZURE_GPT_4o_2024_1120").strip(),
            # Bumped from the original conservative defaults (3 / 3 / 6000) so
            # large, multi-tool queries have enough loop iterations and context
            # budget to complete instead of being cut off early.
            unique_agent_max_iterations=int(os.getenv("UNIQUE_AGENT_MAX_ITERATIONS", "8")),
            unique_max_tool_calls_per_iteration=int(os.getenv("UNIQUE_MAX_TOOL_CALLS_PER_ITERATION", "8")),
            unique_max_history_tokens=int(os.getenv("UNIQUE_MAX_HISTORY_TOKENS", "32000")),
            # ── LLM call tuning — raised to the highest safe defaults so large
            # queries/responses don't silently break on timeout or truncation.
            unique_llm_timeout_seconds=int(os.getenv("UNIQUE_LLM_TIMEOUT_SECONDS", "1600")),
            unique_llm_max_tokens=int(os.getenv("UNIQUE_LLM_MAX_TOKENS", "36000")),
            unique_http_client_timeout_seconds=int(os.getenv("UNIQUE_HTTP_CLIENT_TIMEOUT_SECONDS", "1900")),
            # ── Session / ChatService integration ────────────────────────────
            unique_assistant_id=os.getenv("UNIQUE_ASSISTANT_ID", "").strip(),
            unique_default_session_id=os.getenv("UNIQUE_DEFAULT_SESSION_ID", "poc-demo-session-001").strip(),
            # ── Webhook integration ──────────────────────────────────────────
            unique_webhook_endpoint_secret=os.getenv("UNIQUE_WEBHOOK_ENDPOINT_SECRET", "").strip(),
            unique_default_customer_id=os.getenv("UNIQUE_DEFAULT_CUSTOMER_ID", "CUST-1001").strip(),
            unique_default_portfolio_id=os.getenv("UNIQUE_DEFAULT_PORTFOLIO_ID", "GO00001").strip(),
            # ── MCP integration (see resolution logic above) ─────────────────
            mcp_enabled=mcp_enabled,
            mcp_server_url=mcp_server_url,
            # Optional auth header sent on every MCP request, e.g.
            #   MCP_AUTH_HEADER=Authorization  MCP_AUTH_VALUE="Bearer <token>"
            mcp_auth_header=os.getenv("MCP_AUTH_HEADER", "").strip(),
            mcp_auth_value=os.getenv("MCP_AUTH_VALUE", "").strip(),
            # Raised from 30s — external MCP tools fetching large datasets need
            # more headroom before the app gives up on the call.
            mcp_timeout_seconds=int(os.getenv("MCP_TIMEOUT_SECONDS", "1200")),
            mcp_protocol_version=os.getenv("MCP_PROTOCOL_VERSION", "2025-06-18").strip(),
            sse_enabled=os.getenv("SSE_ENABLED", "true").lower() == "true",
            # Default matches the documented `uvicorn ... --app-dir src` run command
            # (port 8000, no --port override). If you run on a different port, set
            # SSE_WEBHOOK_URL explicitly — otherwise the SSE listener silently fails
            # to reach the webhook (connection refused) and events are never answered.
            sse_webhook_url=os.getenv("SSE_WEBHOOK_URL", "http://127.0.0.1:8000/relationship-manager/webhook").strip(),
            sse_max_concurrent=int(os.getenv("SSE_MAX_CONCURRENT", "10")),
            sse_api_base=os.getenv("SSE_API_BASE", "").strip() or derived_sse_api_base,
            sse_api_key=os.getenv("SSE_API_KEY", "").strip() or os.getenv("UNIQUE_APP_KEY", os.getenv("UNIQUE_API_KEY", "")).strip(),
            sse_app_id=os.getenv("SSE_APP_ID", "").strip() or os.getenv("UNIQUE_APP_ID", "").strip(),
            sse_company_id=os.getenv("SSE_COMPANY_ID", "").strip() or os.getenv("UNIQUE_AUTH_COMPANY_ID", "").strip(),
            sse_ca_bundle=os.getenv("SSE_CA_BUNDLE", "").strip(),
            # Defaults to the event types the webhook handler actually answers
            # (see _ANSWERABLE_EVENTS in api/routes.py). Without this, the SSE
            # stream subscribes to nothing and silently never delivers events.
            subscriptions=tuple(
                s.strip()
                for s in os.getenv("SUBSCRIPTIONS", "module.chosen,user.message.created").split(",")
                if s.strip()
            ),
        )