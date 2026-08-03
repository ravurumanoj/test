"""MCP Tool Wrapper — exposes each MCP server tool as a standard orchestrator Tool.

Standard MCP Architecture
--------------------------
The Model Context Protocol is designed so the LLM generates tool arguments,
not the application code.  The correct flow is:

  1. Discover MCP tools via ``McpManager.list_tools()``
        → each tool has a name, description, and JSON inputSchema

  2. Expose each as an OpenAI function definition (the schema verbatim)
        → the LLM sees ``customerId``, ``query``, etc. in the schema

  3. Let the LLM decide which tool(s) to call and generate the arguments
        → LLM produces ``{"customerId": "CUST-1001"}`` from the schema

  4. Pass LLM-generated arguments directly to ``McpManager.call_tool()``
        → no hardcoding, no naming-convention guessing, no filtering needed

This eliminates all the problems from the old "manual enrichment" approach:
  ✗ hardcoded argument key pools (``customer_id`` + ``customerId`` + …)
  ✗ calling every MCP tool on every request regardless of relevance
  ✗ passing error objects as LLM context when tools fail validation
  ✗ argument mismatch when the MCP server uses an unexpected property name

This module contains only ``McpToolWrapper`` and the ``MCP_TOOL_PREFIX``
constant.  No other module should import ``McpManager`` for the purpose of
calling tools — that responsibility lives here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.agents.base_tool import BaseToolConfig, Tool
from app.schemas import ContentChunk, ToolCallResponse, ToolDescription

if TYPE_CHECKING:
    from app.services.mcp_manager import McpManager, McpToolInfo

logger = logging.getLogger(__name__)

# Prefix added to every MCP tool name so the orchestrator can identify and
# route them without hard-coding any tool names.
MCP_TOOL_PREFIX = "mcp__"

# Cap individual MCP result chunks so they fit comfortably in the LLM context.
_CHUNK_MAX_CHARS: int = 4000


class McpToolConfig(BaseToolConfig):
    """Configuration for a wrapped MCP tool."""

    display_name: str = "MCP Tool"
    icon: str = "🔌"
    is_enabled: bool = True
    is_exclusive: bool = False


class McpToolWrapper(Tool):
    """Wraps one MCP server tool as a standard Unique-Toolkit-aligned Tool.

    The LLM receives the MCP tool's ``inputSchema`` verbatim as the function
    definition, so it generates schema-compliant arguments itself — no
    hardcoding or guessing of property names needed.

    ``run()`` forwards those LLM-generated arguments directly to
    ``McpManager.call_tool()`` with ``filter_to_schema=False`` because the LLM
    already produced correct arguments; additional filtering would only risk
    dropping valid keys.

    References
    ----------
    unique_toolkit_agentic_framework_core.md — Tool Class Documentation
    unique_sdk_ai_completion_advanced_api.md  — MCP API section
    """

    def __init__(self, tool_info: "McpToolInfo", mcp_manager: "McpManager") -> None:
        """Wrap one MCP tool.

        Args:
            tool_info:   Descriptor returned by ``McpManager.list_tools()``
                         (name, description, inputSchema).
            mcp_manager: Shared manager used to execute the actual tool call.
        """
        super().__init__(McpToolConfig())
        self._tool_info = tool_info
        self._mcp_manager = mcp_manager
        logger.debug(
            "McpToolWrapper created",
            extra={"mcp_tool_name": tool_info.name, "orchestrator_name": self.name},
        )

    # ── Tool identity ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Prefixed name exposed to the LLM (e.g. ``mcp__getCustomerBrief``).

        The ``mcp__`` prefix lets the orchestrator route calls to this wrapper
        without hard-coding any tool names — any function the LLM calls whose
        name starts with ``mcp__`` is dispatched here.
        """
        return f"{MCP_TOOL_PREFIX}{self._tool_info.name}"

    @property
    def mcp_name(self) -> str:
        """Raw tool name as advertised by the MCP server (without prefix)."""
        return self._tool_info.name

    # ── LLM schema ────────────────────────────────────────────────────────────

    def tool_description(self) -> ToolDescription:
        """Return the MCP tool's own inputSchema verbatim as a ToolDescription.

        Passing the schema verbatim is the key design decision: the LLM sees
        exactly what the MCP server declared (``customerId``, ``query``, etc.)
        so the arguments it produces are always schema-compliant.
        """
        return ToolDescription(
            name=self.name,
            description=(
                self._tool_info.description
                or f"External MCP tool: {self._tool_info.name}"
            ),
            # Pass the MCP tool's own inputSchema without modification.
            parameters=(
                self._tool_info.input_schema
                or {"type": "object", "properties": {}}
            ),
        )

    def tool_description_for_system_prompt(self) -> str:
        """Return a one-line hint injected into the orchestrator system prompt."""
        desc = self._tool_info.description or self._tool_info.name
        return (
            f"Use {self.name} to fetch live external data: {desc}. "
            "Pass arguments exactly as declared in the tool schema."
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    async def run(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallResponse:
        """Execute the MCP tool with LLM-generated arguments.

        Arguments are produced by the LLM from the schema, so they are already
        correctly named and typed.  They are forwarded to the MCP server as-is
        (``filter_to_schema=False``) to avoid any risk of silently dropping
        valid keys.

        Args:
            tool_call_id: LLM-assigned call identifier.
            arguments:    LLM-generated arguments (schema-compliant).
            context:      Request-level context (customer_id, question, …).
                          Not used directly — the LLM already embedded the
                          correct values into ``arguments`` from the schema.
        """
        logger.info(
            "McpToolWrapper: executing MCP tool",
            extra={
                "orchestrator_tool_name": self.name,
                "mcp_tool_name": self.mcp_name,
                "tool_call_id": tool_call_id,
                "argument_keys": sorted(arguments.keys()),
                "argument_values_preview": {
                    k: str(v)[:60] for k, v in arguments.items()
                },
            },
        )

        result = await self._mcp_manager.call_tool(
            name=self.mcp_name,
            arguments=arguments,
            filter_to_schema=False,  # LLM already generated schema-compliant args
        )

        logger.info(
            "McpToolWrapper: MCP tool returned",
            extra={
                "mcp_tool_name": self.mcp_name,
                "tool_call_id": tool_call_id,
                "is_error": result.is_error,
                "result_length": len(result.text),
                "result_preview": result.text[:200],
            },
        )

        if result.is_error:
            logger.warning(
                "McpToolWrapper: MCP tool returned an error",
                extra={
                    "mcp_tool_name": self.mcp_name,
                    "error_text": result.text[:300],
                    "arguments_sent": arguments,
                },
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self.name,
                error_message=(
                    f"MCP tool '{self.mcp_name}' returned an error: {result.text}"
                ),
                debug_info={
                    "mcp_tool_name": self.mcp_name,
                    "is_error": True,
                    "error_text": result.text,
                    "arguments_sent": arguments,
                },
            )

        chunk = ContentChunk(
            id=f"mcp_{self.mcp_name}_{tool_call_id}",
            chunk_id="0",
            text=result.text[:_CHUNK_MAX_CHARS],
            metadata={
                "source": "mcp",
                "tool": self.mcp_name,
                "tool_call_id": tool_call_id,
            },
        )

        return ToolCallResponse(
            id=tool_call_id,
            name=self.name,
            content=result.text,
            content_chunks=[chunk],
            debug_info={
                "mcp_tool_name": self.mcp_name,
                "is_error": False,
                "result_length": len(result.text),
                "arguments_sent": arguments,
            },
        )
