"""Separate MCP integration service — the **MCP Manager**.

Mirrors the Unique Toolkit *Tool Manager — MCP source* concept documented in
``docs/unique_toolkit_agentic_framework_core.md`` (Tool Manager → Tool Sources →
"MCP tools — Model Context Protocol tools"). In a full Unique platform deployment
the Tool Manager loads MCP tools from a Space configuration; here we provide the
equivalent capability for a **standalone MCP server reachable by URL**.

Design goals
------------
* **Self-contained** — speaks the Model Context Protocol (JSON-RPC 2.0 over the
  *Streamable HTTP* transport) directly, using only the Python standard library
  (``urllib``). It adds **no new runtime dependency** and cannot break existing
  imports even if optional packages are missing.
* **Non-intrusive** — nothing here imports the agents/orchestrator. It is a leaf
  service. Callers inject it optionally, so existing code paths are untouched.

Where do I put my MCP server URL?
---------------------------------
Set the ``MCP_SERVER_URL`` environment variable (read in ``app/settings.py``).
That is the **only** value you must change to connect a different MCP server::

    MCP_SERVER_URL=https://my-mcp-host.example.com/mcp
    # optional auth sent on every request:
    MCP_AUTH_HEADER=Authorization
    MCP_AUTH_VALUE=Bearer <token>

When ``MCP_SERVER_URL`` is empty the manager is disabled and the app behaves
exactly as before.

How does it work with *different* MCP tools?
--------------------------------------------
Tool names are **never hard-coded**. ``list_tools()`` calls the server's
``tools/list`` method to discover every tool the server advertises (each with a
name, description and JSON input schema). ``call_tool(name, arguments)`` can then
invoke *any* of them. Arguments are automatically filtered to the tool's declared
input schema, so passing extra context (e.g. ``question``) never breaks a tool
that only expects ``customer_id``.

Protocol flow (Streamable HTTP)
-------------------------------
1. ``initialize``               — handshake; server may return an ``Mcp-Session-Id`` header.
2. ``notifications/initialized`` — client acknowledges (a JSON-RPC notification).
3. ``tools/list``               — discover available tools.
4. ``tools/call``               — invoke a tool by name with arguments.

Response bodies may be ``application/json`` **or** ``text/event-stream`` (SSE);
both are parsed transparently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.errors import McpIntegrationError

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"


# ─── Value objects ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpToolInfo:
    """Describe a single tool advertised by the MCP server (from ``tools/list``)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolResult:
    """Normalized result of a ``tools/call`` invocation."""

    tool_name: str
    is_error: bool
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


# ─── MCP Manager ─────────────────────────────────────────────────────────────


class McpManager:
    """Connect to a standalone MCP server and expose its tools to the app.

    The manager is safe to construct once at application startup and reuse
    across requests. The MCP ``initialize`` handshake is performed lazily on the
    first call and its session is cached (guarded by an :class:`asyncio.Lock`).

    All network I/O uses blocking ``urllib`` wrapped in :func:`asyncio.to_thread`,
    so the public API stays ``async`` and never blocks the event loop.
    """

    def __init__(
        self,
        *,
        server_url: str,
        auth_header: str = "",
        auth_value: str = "",
        timeout_seconds: int = 30,
        protocol_version: str = _DEFAULT_PROTOCOL_VERSION,
        client_name: str = "relationship-manager-agentic-rag-poc",
        client_version: str = "0.1.0",
    ) -> None:
        """Initialize the MCP Manager with connection settings."""
        self._server_url = (server_url or "").strip()
        self._auth_header = (auth_header or "").strip()
        self._auth_value = (auth_value or "").strip()
        self._timeout = max(1, int(timeout_seconds))
        self._protocol_version = protocol_version
        self._client_name = client_name
        self._client_version = client_version

        self._session_id: str | None = None
        self._initialized = False
        self._rpc_id = 0
        self._tools_cache: list[McpToolInfo] = []
        self._lock = asyncio.Lock()

        logger.debug(
            "McpManager initialized",
            extra={
                "server_url": self._server_url,
                "has_auth": bool(self._auth_header and self._auth_value),
                "timeout_seconds": self._timeout,
                "protocol_version": self._protocol_version,
            },
        )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """Return True when a server URL is set (i.e. the manager can be used)."""
        return bool(self._server_url)

    async def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        """Discover the tools the MCP server exposes via ``tools/list``.

        Results are cached; pass ``refresh=True`` to force a fresh discovery.

        Raises:
            McpIntegrationError: if the server is unreachable or returns an error.
        """
        if self._tools_cache and not refresh:
            return self._tools_cache

        await self._ensure_initialized()
        result = await self._request("tools/list", {})
        raw_tools = (result or {}).get("tools", []) or []
        tools: list[McpToolInfo] = []
        for entry in raw_tools:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            tools.append(
                McpToolInfo(
                    name=str(name),
                    description=str(entry.get("description") or ""),
                    input_schema=entry.get("inputSchema") or {},
                )
            )
        self._tools_cache = tools
        logger.info(
            "McpManager.list_tools: discovered MCP tools",
            extra={"tool_count": len(tools), "tool_names": [t.name for t in tools]},
        )
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        filter_to_schema: bool = True,
    ) -> McpToolResult:
        """Invoke an MCP tool by name via ``tools/call`` and return its result.

        Args:
            name:             The MCP tool name (as returned by ``list_tools``).
            arguments:        Arguments to pass to the tool.
            filter_to_schema: When True, drop any argument keys not declared in
                              the tool's ``inputSchema`` so extra context never
                              causes a validation error on tools with strict
                              schemas. This is what makes the same call site work
                              with *different* MCP tools.

        Raises:
            McpIntegrationError: if the server is unreachable or returns an error.
        """
        await self._ensure_initialized()
        call_args = dict(arguments or {})
        if filter_to_schema:
            call_args = await self._filter_arguments(name, call_args)

        logger.info(
            "McpManager.call_tool: invoking MCP tool",
            extra={"tool_name": name, "argument_keys": list(call_args.keys())},
        )
        result = await self._request("tools/call", {"name": name, "arguments": call_args})
        parsed = self._parse_tool_result(name, result)
        logger.info(
            "McpManager.call_tool: MCP tool returned",
            extra={
                "tool_name": name,
                "is_error": parsed.is_error,
                "text_length": len(parsed.text),
                "text_preview": parsed.text[:200],
            },
        )
        return parsed

    # ── Handshake ─────────────────────────────────────────────────────────────

    async def _ensure_initialized(self) -> None:
        """Perform the MCP ``initialize`` handshake once (lazily, thread-safe)."""
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:  # re-check inside the lock
                return
            logger.info("McpManager: performing MCP initialize handshake")
            await self._request(
                "initialize",
                {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self._client_name,
                        "version": self._client_version,
                    },
                },
            )
            # Acknowledge the handshake (JSON-RPC notification — no response id).
            await self._notify("notifications/initialized")
            self._initialized = True
            logger.info(
                "McpManager: MCP session established",
                extra={"has_session_id": bool(self._session_id)},
            )

    # ── JSON-RPC helpers ──────────────────────────────────────────────────────

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return its ``result`` object."""
        self._rpc_id += 1
        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }
        status, content_type, session_id, body = await asyncio.to_thread(self._post_sync, payload)
        if session_id:
            self._session_id = session_id

        if status < 200 or status >= 300:
            raise McpIntegrationError(
                f"MCP server returned HTTP {status} for method '{method}'.",
                {"method": method, "status": status, "body_preview": body[:300]},
            )

        message = self._parse_body(content_type, body)
        if message is None:
            raise McpIntegrationError(
                f"MCP server returned an unparseable response for method '{method}'.",
                {"method": method, "content_type": content_type, "body_preview": body[:300]},
            )
        error = message.get("error")
        if error:
            raise McpIntegrationError(
                f"MCP server reported an error for method '{method}': {error.get('message')}",
                {"method": method, "code": error.get("code"), "data": error.get("data")},
            )
        return message.get("result", {}) or {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no ``id``; no response expected)."""
        payload: dict[str, Any] = {"jsonrpc": _JSONRPC_VERSION, "method": method}
        if params is not None:
            payload["params"] = params
        status, _content_type, session_id, _body = await asyncio.to_thread(self._post_sync, payload)
        if session_id:
            self._session_id = session_id
        # Notifications typically respond 202 Accepted with an empty body; nothing
        # to parse. Non-2xx here is non-fatal for the handshake, so we only log it.
        if status < 200 or status >= 300:
            logger.warning(
                "McpManager: notification returned non-2xx status",
                extra={"method": method, "status": status},
            )

    def _post_sync(self, payload: dict[str, Any]) -> tuple[int, str, str | None, str]:
        """Blocking HTTP POST of a JSON-RPC payload. Runs inside a worker thread.

        Returns:
            (status_code, content_type, mcp_session_id, response_body)

        Raises:
            McpIntegrationError: on connection/timeout failures (no HTTP status).
        """
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            # MCP servers may reply with either JSON or an SSE stream.
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._auth_header and self._auth_value:
            headers[self._auth_header] = self._auth_value

        request = urllib.request.Request(  # noqa: S310 - URL is operator-provided config
            self._server_url, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
                return (
                    int(getattr(response, "status", 200)),
                    response.headers.get("Content-Type", ""),
                    response.headers.get("Mcp-Session-Id"),
                    body,
                )
        except urllib.error.HTTPError as exc:
            # HTTP error responses still carry a status + body we can surface.
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - defensive
                body = ""
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            session_id = exc.headers.get("Mcp-Session-Id") if exc.headers else None
            return int(exc.code), content_type, session_id, body
        except urllib.error.URLError as exc:
            raise McpIntegrationError(
                "Could not reach the MCP server.",
                {"server_url": self._server_url, "reason": str(getattr(exc, "reason", exc))},
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise McpIntegrationError(
                "Unexpected error while calling the MCP server.",
                {"server_url": self._server_url, "error": str(exc)},
            ) from exc

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_body(self, content_type: str, body: str) -> dict[str, Any] | None:
        """Parse a response body as JSON or SSE into a JSON-RPC message dict."""
        if not body or not body.strip():
            return None
        if "text/event-stream" in (content_type or "").lower():
            return self._parse_sse(body)
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            # Some servers stream SSE without setting the content-type header.
            return self._parse_sse(body)

    @staticmethod
    def _parse_sse(body: str) -> dict[str, Any] | None:
        """Extract the JSON-RPC response object from an SSE (event-stream) body.

        Concatenates ``data:`` lines per event and returns the last event whose
        JSON payload looks like a JSON-RPC response (has ``result`` or ``error``).
        """
        last_message: dict[str, Any] | None = None
        for raw_event in body.replace("\r\n", "\n").split("\n\n"):
            data_lines = [
                line[len("data:"):].lstrip()
                for line in raw_event.split("\n")
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            try:
                obj = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                last_message = obj
        return last_message

    @staticmethod
    def _parse_tool_result(name: str, result: dict[str, Any] | None) -> McpToolResult:
        """Normalize a ``tools/call`` result into an :class:`McpToolResult`."""
        result = result or {}
        is_error = bool(result.get("isError", False))
        texts: list[str] = []
        for item in result.get("content", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                texts.append(str(item.get("text", "")))
            elif item_type == "resource":
                resource = item.get("resource", {}) or {}
                texts.append(str(resource.get("text") or json.dumps(resource)))
            else:
                texts.append(json.dumps(item))
        text = "\n".join(t for t in texts if t)
        if not text and result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"])
        return McpToolResult(tool_name=name, is_error=is_error, text=text, raw=result)

    # ── Argument filtering ────────────────────────────────────────────────────

    async def _filter_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Restrict ``arguments`` to the keys declared in the tool's input schema.

        If the schema is unavailable or declares no ``properties``, the arguments
        are passed through unchanged.
        """
        tools = await self.list_tools()
        schema = next((t.input_schema for t in tools if t.name == name), None)
        if not isinstance(schema, dict):
            return arguments
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return arguments
        allowed = set(properties.keys())
        filtered = {key: value for key, value in arguments.items() if key in allowed}
        if len(filtered) != len(arguments):
            logger.debug(
                "McpManager: filtered arguments to tool input schema",
                extra={
                    "tool_name": name,
                    "dropped_keys": sorted(set(arguments) - allowed),
                    "kept_keys": sorted(filtered.keys()),
                },
            )
        return filtered
