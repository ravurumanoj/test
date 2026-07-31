
"""CRM sub-agent implemented as a Unique Toolkit-aligned Tool.

Extends the abstract Tool base class so the orchestrator can:
  - Expose its LLM schema via tool_description()
  - Execute it via run() → ToolCallResponse
  - Route content_chunks to ReferenceManager for source citations
  - Route debug_info to DebugInfoManager for developer inspection

Backward-compatible handle() shim preserved for existing test surfaces.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from app.agents.base_tool import BaseToolConfig, Tool
from app.agents.prompts import CRM_AGENT_PROMPT
from app.schemas import AgentAnswer, ContentChunk, ToolCallResponse, ToolDescription
from app.services.crm_tools import CrmTools
from app.services.unique_toolkit import UniqueToolkit

if TYPE_CHECKING:  # imported for type hints only — keeps runtime coupling minimal
    from app.services.mcp_manager import McpManager

logger = logging.getLogger(__name__)

# Content chunk constants — used by _build_content_chunks
_CHUNK_MAX_CHARS: int = 2000   # per-chunk character cap (≈500 tokens at 4 chars/token)
_CHUNK_MIN_CHARS: int = 50     # skip trivial empty sections below this threshold


class CrmAgentConfig(BaseToolConfig):
    """Configuration for the CRM sub-agent tool."""

    display_name: str = "CRM Agent"
    icon: str = "👤"
    is_enabled: bool = True
    is_exclusive: bool = False
    # Optional: restrict MCP enrichment to a single named MCP tool. When empty
    # (default) EVERY tool the MCP server advertises is called (best-effort),
    # which is what makes this work with different MCP tools out of the box.
    mcp_tool_name: str = ""


class CrmAgent(Tool):
    """CRM sub-agent tool — retrieves and summarises CRM context for a customer.

    Implements the Unique Toolkit Tool abstract class so the orchestrator
    can register it, expose its schema to the LLM, and execute it through
    the standard ToolCallResponse interface.

    References
    ----------
    unique_toolkit_agentic_framework_core.md — Tool Class Documentation
    """

    def __init__(
        self,
        unique_toolkit: UniqueToolkit,
        config: CrmAgentConfig | None = None,
        mcp_manager: "McpManager | None" = None,
    ) -> None:
        """Initialize the CRM agent with its toolkit dependency.

        Args:
            unique_toolkit: Facade over the Unique AI SDK/Toolkit completion layer.
            config:         Optional tool configuration (defaults applied when None).
            mcp_manager:    Optional MCP Manager. When provided AND configured with
                            a server URL, the agent enriches its CRM context with
                            data returned by the MCP server's tools. When None
                            (the default), the agent behaves exactly as before.
        """
        super().__init__(config or CrmAgentConfig())
        self.unique_toolkit = unique_toolkit
        self.tools = CrmTools()
        self.mcp_manager = mcp_manager
        logger.debug(
            "CrmAgent initialized",
            extra={"mcp_enabled": bool(mcp_manager and mcp_manager.is_configured)},
        )

    # ── Tool identity ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Return the tool identifier used in LLM function call names."""
        return "crm_agent"

    # ── LLM schema ────────────────────────────────────────────────────────────

    def tool_description(self) -> ToolDescription:
        """Return the LLM-readable tool schema for the orchestrator planning step.

        Mirrors LanguageModelToolDescription in unique_toolkit_agentic_framework_core.md.
        """
        return ToolDescription(
            name=self.name,
            description=(
                "Retrieve and summarise CRM data for a customer: interaction history, "
                "service requests, advisory suggestions, compliance flags, and alerts. "
                "Use this tool when the question is about customer interactions, service history, "
                "compliance status, advisory notes, or relationship management context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "string",
                        "description": (
                            "The unique customer identifier to retrieve CRM data for "
                            "(e.g. CUST-1001). Use the customer ID provided in the system context."
                        ),
                    }
                },
                "required": ["customer_id"],
                "additionalProperties": False,
            },
        )

    def tool_description_for_system_prompt(self) -> str:
        """Return additional guidance text injected into the orchestrator system prompt."""
        return (
            "Use crm_agent when the question involves customer interactions, service requests, "
            "compliance flags, advisory suggestions, NPS, churn risk, or alerts."
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    async def run(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallResponse:
        """Execute the CRM sub-agent and return a structured ToolCallResponse.

        Mirrors Tool.run() from unique_toolkit_agentic_framework_core.md.
        Returns content_chunks for ReferenceManager and debug_info for DebugInfoManager.

        Args:
            tool_call_id: LLM-assigned call identifier.
            arguments:    Parsed JSON arguments from the LLM function call.
            context:      Request-level context — must contain customer_id and question.
        """
        # Resolve customer_id: prefer the value the LLM passed in arguments
        # (now that customer_id is a required tool parameter), fall back to
        # the request-level context supplied by the orchestrator.
        customer_id: str = str(arguments.get("customer_id") or context.get("customer_id", "")).strip()
        question: str = context.get("question", "")

        logger.info(
            "CrmAgent.run started",
            extra={
                "tool_call_id": tool_call_id,
                "customer_id": customer_id,
                "question": question,
                "arguments_received": arguments,
            },
        )

        # ── Data retrieval ────────────────────────────────────────────────────
        try:
            crm_data = self.tools.get_customer_crm(customer_id)
            logger.info(
                "CrmAgent: CRM data retrieved",
                extra={
                    "customer_id": customer_id,
                    "data_keys": list(crm_data.keys()),
                    "record_counts": {
                        k: len(v) if isinstance(v, list) else "scalar"
                        for k, v in crm_data.items()
                    },
                },
            )
        except Exception as exc:
            logger.exception(
                "CrmAgent: CRM data retrieval failed",
                extra={"customer_id": customer_id},
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self.name,
                error_message=f"CRM data retrieval failed: {exc}",
            )

        # ── MCP enrichment (only active when an MCP server is configured) ──────
        # Augments the local CRM record with data returned by the standalone MCP
        # server's tools. Completely inert when no MCP manager is injected, and
        # fails soft (never raises) so a bad MCP server can't break the CRM flow.
        mcp_augmentation = await self._augment_with_mcp(customer_id=customer_id, question=question)
        if mcp_augmentation.get("enrichment"):
            # Merge MCP output into the context handed to the LLM summarizer so the
            # generated summary can reason over the external MCP tool results too.
            crm_data = {**crm_data, "mcp_tools": mcp_augmentation["enrichment"]}

        # ── LLM summarization ─────────────────────────────────────────────────
        logger.debug(
            "CrmAgent: sending data to LLM for summarization",
            extra={
                "customer_id": customer_id,
                "question": question,
                "crm_data_keys": list(crm_data.keys()),
                "prompt_name": "CRM_AGENT_PROMPT",
            },
        )
        try:
            summary = self.unique_toolkit.execute(
                agent_name=self.name,
                prompt=CRM_AGENT_PROMPT,
                context=crm_data,
                question=question,
            )
            logger.info(
                "CrmAgent: LLM summary received",
                extra={
                    "customer_id": customer_id,
                    "summary_length": len(summary),
                    "summary_preview": summary[:300],
                },
            )
        except Exception as exc:
            logger.exception(
                "CrmAgent: LLM completion failed",
                extra={"customer_id": customer_id},
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self.name,
                error_message=f"CRM LLM completion failed: {exc}",
            )

        # ── Build ContentChunks for ReferenceManager ──────────────────────────
        content_chunks = self._build_content_chunks(customer_id=customer_id, crm_data=crm_data)
        # Append MCP-derived chunks so external tool output is citable too.
        content_chunks.extend(mcp_augmentation.get("chunks", []))

        debug_info: dict[str, Any] = {
            "customer_id": customer_id,
            "crm_data_keys": list(crm_data.keys()),
            "summary_length": len(summary),
            "chunk_count": len(content_chunks),
            # MCP diagnostics (present regardless of whether MCP is enabled).
            "mcp_enabled": mcp_augmentation.get("enabled", False),
            "mcp_discovered_tools": mcp_augmentation.get("discovered_tools", []),
            "mcp_called_tools": mcp_augmentation.get("called", []),
            "mcp_error": mcp_augmentation.get("error"),
        }

        logger.info(
            "CrmAgent.run completed",
            extra={
                "tool_call_id": tool_call_id,
                "customer_id": customer_id,
                "chunk_count": len(content_chunks),
                "debug_info_keys": list(debug_info.keys()),
            },
        )

        return ToolCallResponse(
            id=tool_call_id,
            name=self.name,
            content=summary,
            content_chunks=content_chunks,
            debug_info=debug_info,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_content_chunks(
        self, customer_id: str, crm_data: dict[str, Any]
    ) -> list[ContentChunk]:
        """Build referenceable ContentChunks from CRM data sections.

        Each major section becomes a separate chunk so the ReferenceManager
        can assign granular citation numbers (e.g. [1] profile, [2] interactions).
        """
        sections = {
            "profile": crm_data.get("customer_profile", {}),
            "account_metadata": crm_data.get("account_metadata", {}),
            "interactions": crm_data.get("conversation_history", []),
            "suggestions": crm_data.get("suggestions_provided", []),
            "compliance": crm_data.get("compliance_flags", []),
        }
        chunks: list[ContentChunk] = []
        for section_name, section_data in sections.items():
            if not section_data:
                continue
            text = json.dumps(section_data, ensure_ascii=True, indent=2)
            if len(text) > _CHUNK_MIN_CHARS:
                chunks.append(
                    ContentChunk(
                        id=f"crm_{customer_id}_{section_name}",
                        chunk_id="0",
                        text=text[:_CHUNK_MAX_CHARS],  # per-chunk token budget cap
                        metadata={
                            "customer_id": customer_id,
                            "section": section_name,
                            "source": "crm",
                        },
                    )
                )
        logger.debug(
            "CrmAgent: content chunks built",
            extra={"customer_id": customer_id, "chunk_count": len(chunks)},
        )
        return chunks

    # ── MCP enrichment ────────────────────────────────────────────────────────

    async def _augment_with_mcp(self, customer_id: str, question: str) -> dict[str, Any]:
        """Enrich CRM context with results from the configured MCP server's tools.

        Mirrors the Unique Toolkit *Tool Manager \u2192 MCP source* pattern: the MCP
        server's tools are discovered dynamically and invoked here. Nothing about
        the tool names is hard-coded \u2014 whatever tools the server advertises are
        used, so this works with *different* MCP servers/tools without code change.

        This method NEVER raises: any MCP failure is logged and reported via the
        returned ``error`` field so the core CRM flow always completes.

        Returns a dict with:
            enabled           \u2014 whether an MCP server was configured
            discovered_tools  \u2014 names of every tool the server advertised
            called            \u2014 names of tools actually invoked this turn
            enrichment        \u2014 list of {tool, is_error, text} for the LLM context
            chunks            \u2014 list[ContentChunk] built from successful results
            error             \u2014 error string when discovery/all calls failed (else None)
        """
        # No manager injected, or no server URL configured \u2192 behave as before.
        if self.mcp_manager is None or not self.mcp_manager.is_configured:
            return {"enabled": False, "discovered_tools": [], "called": [], "enrichment": [], "chunks": []}

        # 1. Discover the tools the MCP server exposes.
        try:
            tools = await self.mcp_manager.list_tools()
        except Exception as exc:  # noqa: BLE001 - fail soft, keep CRM flow alive
            logger.warning(
                "CrmAgent: MCP tool discovery failed \u2014 continuing without MCP enrichment",
                extra={"customer_id": customer_id, "error": str(exc)},
            )
            return {"enabled": True, "discovered_tools": [], "called": [], "enrichment": [], "chunks": [], "error": str(exc)}

        # 2. Select which tools to call. Empty config \u2192 call all discovered tools.
        configured_tool = (self.settings.mcp_tool_name or "").strip()
        if configured_tool:
            target_tools = [t for t in tools if t.name == configured_tool]
            if not target_tools:
                logger.warning(
                    "CrmAgent: configured MCP tool not found on server",
                    extra={"configured_tool": configured_tool, "available": [t.name for t in tools]},
                )
        else:
            target_tools = tools

        # 3. Invoke each selected tool. Arguments are filtered to each tool's own
        #    input schema by the manager, so passing both keys is always safe.
        base_arguments: dict[str, Any] = {"customer_id": customer_id, "question": question}
        enrichment: list[dict[str, Any]] = []
        chunks: list[ContentChunk] = []
        called: list[str] = []
        for tool in target_tools:
            called.append(tool.name)
            try:
                result = await self.mcp_manager.call_tool(tool.name, base_arguments)
            except Exception as exc:  # noqa: BLE001 - one bad tool must not abort the rest
                logger.warning(
                    "CrmAgent: MCP tool call failed \u2014 skipping this tool",
                    extra={"tool_name": tool.name, "error": str(exc)},
                )
                enrichment.append({"tool": tool.name, "is_error": True, "text": f"MCP call failed: {exc}"})
                continue

            enrichment.append({"tool": tool.name, "is_error": result.is_error, "text": result.text})
            if result.text and not result.is_error:
                chunks.append(
                    ContentChunk(
                        id=f"mcp_{customer_id}_{tool.name}",
                        chunk_id="0",
                        text=result.text[:_CHUNK_MAX_CHARS],
                        metadata={"customer_id": customer_id, "source": "mcp", "tool": tool.name},
                    )
                )

        logger.info(
            "CrmAgent: MCP enrichment completed",
            extra={
                "customer_id": customer_id,
                "discovered_tools": [t.name for t in tools],
                "called_tools": called,
                "chunk_count": len(chunks),
            },
        )
        return {
            "enabled": True,
            "discovered_tools": [t.name for t in tools],
            "called": called,
            "enrichment": enrichment,
            "chunks": chunks,
        }

    # ── Backward-compatible shim ──────────────────────────────────────────────

    async def handle(self, customer_id: str, question: str) -> AgentAnswer:
        """Legacy interface kept for backward compatibility with existing tests.

        Wraps run() and adapts ToolCallResponse → AgentAnswer.
        New code should call run() directly via the orchestrator.
        """
        logger.debug(
            "CrmAgent.handle (legacy shim) called",
            extra={"customer_id": customer_id},
        )
        response = await self.run(
            tool_call_id=f"legacy_{self.name}",
            arguments={},
            context={"customer_id": customer_id, "question": question},
        )
        return AgentAnswer(
            agent_name="crm",
            summary=response.content if response.successful else response.error_message,
            # Data is already encoded in response.content and response.content_chunks;
            # retrieved_context is an empty dict to avoid a redundant second fetch.
            retrieved_context={},
        )
