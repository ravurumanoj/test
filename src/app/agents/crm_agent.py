
"""CRM sub-agent implemented as a Unique Toolkit-aligned Tool.

Extends the abstract Tool base class so the orchestrator can:
  - Expose its LLM schema via tool_description()
  - Execute it via run() → ToolCallResponse
  - Route content_chunks to ReferenceManager for source citations
  - Route debug_info to DebugInfoManager for developer inspection

This agent is responsible for local CRM data retrieval and LLM summarisation
only.  MCP tool integration now lives at the orchestrator level via
``McpToolWrapper`` — the LLM decides which MCP tools to call and generates
the arguments itself, so no MCP code belongs here.

Backward-compatible handle() shim preserved for existing test surfaces.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.base_tool import BaseToolConfig, Tool
from app.agents.prompts import CRM_AGENT_PROMPT
from app.schemas import AgentAnswer, ContentChunk, ToolCallResponse, ToolDescription
from app.services.crm_tools import CrmTools
from app.services.unique_toolkit import UniqueToolkit

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
    ) -> None:
        """Initialize the CRM agent with its toolkit dependency.

        Args:
            unique_toolkit: Facade over the Unique AI SDK/Toolkit completion layer.
            config:         Optional tool configuration (defaults applied when None).
        """
        super().__init__(config or CrmAgentConfig())
        self.unique_toolkit = unique_toolkit
        self.tools = CrmTools()
        logger.debug("CrmAgent initialized")

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

        debug_info: dict[str, Any] = {
            "customer_id": customer_id,
            "crm_data_keys": list(crm_data.keys()),
            "summary_length": len(summary),
            "chunk_count": len(content_chunks),
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
