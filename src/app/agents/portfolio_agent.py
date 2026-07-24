
"""Portfolio sub-agent implemented as a Unique Toolkit-aligned Tool.

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
from typing import Any

from app.agents.base_tool import BaseToolConfig, Tool
from app.agents.prompts import PORTFOLIO_AGENT_PROMPT
from app.schemas import AgentAnswer, ContentChunk, ToolCallResponse, ToolDescription
from app.services.portfolio_tools import PortfolioTools
from app.services.unique_toolkit import UniqueToolkit

logger = logging.getLogger(__name__)

# Content chunk constants — used by _build_content_chunks
_CHUNK_MAX_CHARS: int = 2000   # per-chunk character cap (≈500 tokens at 4 chars/token)
_CHUNK_MIN_CHARS: int = 50     # skip trivial empty sections below this threshold


class PortfolioAgentConfig(BaseToolConfig):
    """Configuration for the portfolio sub-agent tool."""

    display_name: str = "Portfolio Agent"
    icon: str = "📊"
    is_enabled: bool = True
    is_exclusive: bool = False


class PortfolioAgent(Tool):
    """Portfolio sub-agent tool — retrieves and summarises portfolio context.

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
        config: PortfolioAgentConfig | None = None,
    ) -> None:
        """Initialize the portfolio agent with its toolkit dependency."""
        super().__init__(config or PortfolioAgentConfig())
        self.unique_toolkit = unique_toolkit
        self.tools = PortfolioTools()
        logger.debug("PortfolioAgent initialized")

    # ── Tool identity ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Return the tool identifier used in LLM function call names."""
        return "portfolio_agent"

    # ── LLM schema ────────────────────────────────────────────────────────────

    def tool_description(self) -> ToolDescription:
        """Return the LLM-readable tool schema for the orchestrator planning step.

        Mirrors LanguageModelToolDescription in unique_toolkit_agentic_framework_core.md.
        """
        return ToolDescription(
            name=self.name,
            description=(
                "Retrieve and summarise portfolio data for a customer: holdings, "
                "asset allocation, P&L, performance metrics, compliance view, and alerts. "
                "Use this tool when the question is about investments, portfolio performance, "
                "asset allocation, returns, risk metrics, or financial position."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    def tool_description_for_system_prompt(self) -> str:
        """Return additional guidance text injected into the orchestrator system prompt."""
        return (
            "Use portfolio_agent when the question involves investments, holdings, "
            "asset allocation, returns, risk metrics, P&L, or the customer's financial position."
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    async def run(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallResponse:
        """Execute the portfolio sub-agent and return a structured ToolCallResponse.

        Mirrors Tool.run() from unique_toolkit_agentic_framework_core.md.
        Returns content_chunks for ReferenceManager and debug_info for DebugInfoManager.

        Args:
            tool_call_id: LLM-assigned call identifier.
            arguments:    Parsed JSON arguments from the LLM function call.
            context:      Request-level context — must contain customer_id and question.
        """
        customer_id: str = context.get("customer_id", "")
        question: str = context.get("question", "")

        logger.info(
            "PortfolioAgent.run started",
            extra={"tool_call_id": tool_call_id, "customer_id": customer_id},
        )

        # ── Data retrieval ────────────────────────────────────────────────────
        try:
            portfolio_data = self.tools.get_portfolio_snapshot(customer_id)
            logger.info(
                "PortfolioAgent: portfolio data retrieved",
                extra={"customer_id": customer_id, "data_keys": list(portfolio_data.keys())},
            )
        except Exception as exc:
            logger.exception(
                "PortfolioAgent: portfolio data retrieval failed",
                extra={"customer_id": customer_id},
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self.name,
                error_message=f"Portfolio data retrieval failed: {exc}",
            )

        # ── LLM summarization ─────────────────────────────────────────────────
        try:
            summary = self.unique_toolkit.execute(
                agent_name=self.name,
                prompt=PORTFOLIO_AGENT_PROMPT,
                context=portfolio_data,
                question=question,
            )
            logger.info(
                "PortfolioAgent: summary generated",
                extra={"customer_id": customer_id, "summary_length": len(summary)},
            )
        except Exception as exc:
            logger.exception(
                "PortfolioAgent: LLM completion failed",
                extra={"customer_id": customer_id},
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self.name,
                error_message=f"Portfolio LLM completion failed: {exc}",
            )

        # ── Build ContentChunks for ReferenceManager ──────────────────────────
        content_chunks = self._build_content_chunks(customer_id=customer_id, portfolio_data=portfolio_data)

        debug_info: dict[str, Any] = {
            "customer_id": customer_id,
            "portfolio_data_keys": list(portfolio_data.keys()),
            "summary_length": len(summary),
            "chunk_count": len(content_chunks),
        }

        logger.info(
            "PortfolioAgent.run completed",
            extra={
                "tool_call_id": tool_call_id,
                "customer_id": customer_id,
                "chunk_count": len(content_chunks),
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
        self, customer_id: str, portfolio_data: dict[str, Any]
    ) -> list[ContentChunk]:
        """Build referenceable ContentChunks from portfolio data sections.

        Each major section becomes a separate chunk so the ReferenceManager
        can assign granular citation numbers (e.g. [1] holdings, [2] P&L).
        """
        sections = {
            "holdings": portfolio_data.get("holdings", []),
            "asset_allocation": portfolio_data.get("asset_allocation", {}),
            "pnl_summary": portfolio_data.get("pnl_summary", {}),
            "portfolio_summary": portfolio_data.get("portfolio_summary", {}),
        }
        chunks: list[ContentChunk] = []
        for section_name, section_data in sections.items():
            if not section_data:
                continue
            text = json.dumps(section_data, ensure_ascii=True, indent=2)
            if len(text) > _CHUNK_MIN_CHARS:
                chunks.append(
                    ContentChunk(
                        id=f"portfolio_{customer_id}_{section_name}",
                        chunk_id="0",
                        text=text[:_CHUNK_MAX_CHARS],  # per-chunk token budget cap
                        metadata={
                            "customer_id": customer_id,
                            "section": section_name,
                            "source": "portfolio",
                        },
                    )
                )
        logger.debug(
            "PortfolioAgent: content chunks built",
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
            "PortfolioAgent.handle (legacy shim) called",
            extra={"customer_id": customer_id},
        )
        response = await self.run(
            tool_call_id=f"legacy_{self.name}",
            arguments={},
            context={"customer_id": customer_id, "question": question},
        )
        return AgentAnswer(
            agent_name="portfolio",
            summary=response.content if response.successful else response.error_message,
            # Data is already encoded in response.content and response.content_chunks;
            # retrieved_context is an empty dict to avoid a redundant second fetch.
            retrieved_context={},
        )
