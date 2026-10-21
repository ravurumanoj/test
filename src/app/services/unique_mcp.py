"""Unique MCP connector check — call a tool delivered in the webhook's ``mcpServers``.

Unique puts the MCP connectors enabled in the space (e.g. ``pms.mcp.dev.echonet``)
into the event payload as ``payload.mcpServers``. We pick a tool from there and run
it through ``unique_sdk.MCP.call_tool``; Unique proxies the call to the MCP server,
so no server URL or credentials are needed in this app.

Triggered from the webhook when the user types "get ping".
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.services.unique_client import UniqueAIClient
from app.settings import Settings

logger = logging.getLogger(__name__)

try:
    import unique_sdk
except ImportError:
    unique_sdk = None

PING_TRIGGER = "get ping"


def is_ping_request(text: str) -> bool:
    """Return True when the user asked for the MCP connectivity check."""
    return PING_TRIGGER in text.lower()


def _pick_tool(mcp_servers: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return (server, tool): a tool with 'ping' in its name if any, else the first tool offered."""
    candidates = [(server, tool) for server in mcp_servers for tool in server.get("tools") or []]
    for server, tool in candidates:
        if "ping" in str(tool.get("name", "")).lower():
            return server, tool
    return candidates[0] if candidates else None


def _result_text(result: Any) -> str:
    """Join the text items of an MCP call result; fall back to the raw result as JSON."""
    items = result.get("content") or []
    texts = [item.get("text") for item in items if item.get("type") == "text" and item.get("text")]
    return "\n\n".join(texts) if texts else json.dumps(result, default=str)


async def run_mcp_ping(
    settings: Settings,
    *,
    mcp_servers: list[dict[str, Any]],
    user_id: str,
    company_id: str,
    chat_id: str,
    message_id: str,
) -> str:
    """Call one MCP tool from the payload and return a chat-ready answer (never raises)."""
    logger.info(
        "MCP ping: servers in payload",
        extra={
            "server_count": len(mcp_servers),
            "servers": [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "tools": [t.get("name") for t in s.get("tools") or []],
                }
                for s in mcp_servers
            ],
        },
    )

    if unique_sdk is None or not hasattr(unique_sdk, "MCP"):
        logger.warning("MCP ping: unique_sdk.MCP not available")
        return "MCP check failed: the installed unique_sdk has no MCP support."

    picked = _pick_tool(mcp_servers)
    if picked is None:
        logger.warning("MCP ping: no MCP tools in payload")
        return "MCP check failed: no MCP tools were sent with this message. Is the connector enabled in this space?"
    server, tool = picked
    server_name, tool_name = server.get("name", ""), tool.get("name", "")

    logger.info(
        "MCP ping: calling tool",
        extra={
            "server_id": server.get("id"),
            "server_name": server_name,
            "tool_name": tool_name,
            "is_connected": tool.get("isConnected"),
            "input_schema": tool.get("inputSchema"),
        },
    )

    try:
        UniqueAIClient(settings)._configure_sdk(unique_sdk)
        result = await asyncio.to_thread(
            unique_sdk.MCP.call_tool,
            user_id=user_id or settings.unique_auth_user_id,
            company_id=company_id or settings.unique_auth_company_id,
            name=tool_name,
            messageId=message_id,
            chatId=chat_id,
            arguments={},
        )
    except Exception as exc:
        logger.exception("MCP ping: tool call failed", extra={"tool_name": tool_name})
        return f"MCP check failed: calling `{tool_name}` on `{server_name}` raised: {exc}"

    text = _result_text(result)
    is_error = bool(result.get("isError"))
    logger.info(
        "MCP ping: tool result",
        extra={"tool_name": tool_name, "is_error": is_error, "result": text},
    )
    status = "returned an error" if is_error else "returned"
    return f"MCP tool `{tool_name}` on connector `{server_name}` {status}:\n\n{text}"
