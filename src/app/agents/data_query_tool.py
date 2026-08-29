"""Generic data-query Tool — wraps ONE data-retrieval API as an LLM-callable tool.

Instead of a coarse "portfolio_agent" / "crm_agent" that hardcodes a single data
method, every underlying data API (get_portfolio_snapshot, get_performance_view,
get_interactions, ...) is exposed to the LLM as its own first-class tool. The LLM
decides which one(s) to call from the question — no hardcoded routing.

Each tool:
  1. resolves its arguments (customer_id + any optional filters),
  2. calls its single bound data method (logged explicitly),
  3. summarises the result via the domain prompt through UniqueToolkit,
  4. returns a ToolCallResponse with referenceable content_chunks + debug_info.

Mirrors the Unique Toolkit Tool pattern (docs/unique_toolkit_agentic_framework_core.md).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.base_tool import BaseToolConfig, Tool
from app.schemas import ContentChunk, ToolCallResponse, ToolDescription
from app.services.unique_toolkit import UniqueToolkit

logger = logging.getLogger(__name__)

# Content chunk sizing — mirrors the previous per-agent constants.
_CHUNK_MAX_CHARS: int = 2000
_CHUNK_MIN_CHARS: int = 50


@dataclass(frozen=True)
class DataQuerySpec:
    """Declarative description of one data API exposed as a tool.

    Attributes
    ----------
    name                — LLM function name (e.g. ``portfolio_performance``).
    domain              — ``portfolio`` or ``crm`` (drives AgentAnswer routing).
    description         — LLM-facing description used for tool selection.
    prompt_hint         — extra one-line guidance injected into the system prompt.
    summarize_prompt    — domain prompt used to summarise the fetched data.
    fetch               — the single bound data method this tool calls.
    requires_customer   — whether ``customer_id`` is a required argument.
    optional_parameters — extra JSON-schema properties the LLM may supply
                          (forwarded as keyword arguments to ``fetch``).
    """

    name: str
    domain: str
    description: str
    prompt_hint: str
    summarize_prompt: str
    fetch: Callable[..., Any]
    requires_customer: bool = True
    optional_parameters: dict[str, dict[str, Any]] = field(default_factory=dict)


class DataQueryToolConfig(BaseToolConfig):
    """Configuration for a data-query tool."""

    display_name: str = "Data Query Tool"
    icon: str = "🗂️"
    is_enabled: bool = True
    is_exclusive: bool = False


class DataQueryTool(Tool):
    """Expose a single data-retrieval API as an LLM-callable tool."""

    def __init__(
        self,
        spec: DataQuerySpec,
        unique_toolkit: UniqueToolkit,
        config: DataQueryToolConfig | None = None,
    ) -> None:
        """Bind one data API spec to the toolkit used for summarisation."""
        super().__init__(config or DataQueryToolConfig())
        self._spec = spec
        self.unique_toolkit = unique_toolkit
        logger.debug("DataQueryTool initialized", extra={"tool_name": spec.name, "domain": spec.domain})

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Return the LLM function name for this tool."""
        return self._spec.name

    @property
    def domain(self) -> str:
        """Return the business domain (portfolio/crm) for AgentAnswer routing."""
        return self._spec.domain

    # ── LLM schema ────────────────────────────────────────────────────────────

    def tool_description(self) -> ToolDescription:
        """Build the LLM-readable schema from the spec."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        if self._spec.requires_customer:
            properties["customer_id"] = {
                "type": "string",
                "description": (
                    "The unique customer identifier (e.g. CUST-1001). "
                    "Use the customer ID provided in the system context."
                ),
            }
            required.append("customer_id")
        properties.update(self._spec.optional_parameters)
        return ToolDescription(
            name=self._spec.name,
            description=self._spec.description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        )

    def tool_description_for_system_prompt(self) -> str:
        """Return the extra system-prompt hint for this tool."""
        return self._spec.prompt_hint

    # ── Execution ─────────────────────────────────────────────────────────────

    async def run(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallResponse:
        """Fetch data via the bound API, summarise it, and return a ToolCallResponse."""
        customer_id = str(arguments.get("customer_id") or context.get("customer_id", "")).strip()
        question = context.get("question", "")
        extra_kwargs = {
            key: arguments[key]
            for key in self._spec.optional_parameters
            if key in arguments and arguments[key] not in (None, "")
        }

        logger.info(
            "DataQueryTool: TOOL TRIGGERED — %s",
            self._spec.name,
            extra={
                "tool_name": self._spec.name,
                "domain": self._spec.domain,
                "data_method": getattr(self._spec.fetch, "__name__", "unknown"),
                "customer_id": customer_id,
                "optional_args": extra_kwargs,
            },
        )

        # ── Data retrieval ────────────────────────────────────────────────────
        try:
            if self._spec.requires_customer:
                data = self._spec.fetch(customer_id, **extra_kwargs)
            else:
                data = self._spec.fetch(**extra_kwargs)
        except Exception as exc:
            logger.exception(
                "DataQueryTool: data retrieval failed",
                extra={"tool_name": self._spec.name, "customer_id": customer_id},
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self._spec.name,
                error_message=f"{self._spec.name} data retrieval failed: {exc}",
            )

        logger.info(
            "DataQueryTool: data retrieved",
            extra={
                "tool_name": self._spec.name,
                "customer_id": customer_id,
                "record_count": len(data) if isinstance(data, list) else 1,
                "keys": list(data.keys()) if isinstance(data, dict) else "list",
            },
        )

        # ── LLM summarisation ─────────────────────────────────────────────────
        summarize_context: dict[str, Any] = data if isinstance(data, dict) else {"records": data}
        try:
            summary = self.unique_toolkit.execute(
                agent_name=self._spec.name,
                prompt=self._spec.summarize_prompt,
                context=summarize_context,
                question=question,
            )
        except Exception as exc:
            logger.exception(
                "DataQueryTool: LLM summarisation failed",
                extra={"tool_name": self._spec.name, "customer_id": customer_id},
            )
            return ToolCallResponse(
                id=tool_call_id,
                name=self._spec.name,
                error_message=f"{self._spec.name} summarisation failed: {exc}",
            )

        content_chunks = self._build_chunks(data=data, customer_id=customer_id)
        debug_info: dict[str, Any] = {
            "tool_name": self._spec.name,
            "domain": self._spec.domain,
            "customer_id": customer_id,
            "summary_length": len(summary),
            "chunk_count": len(content_chunks),
        }
        logger.info(
            "DataQueryTool: completed — %s",
            self._spec.name,
            extra={"tool_name": self._spec.name, "chunk_count": len(content_chunks)},
        )
        return ToolCallResponse(
            id=tool_call_id,
            name=self._spec.name,
            content=summary,
            content_chunks=content_chunks,
            debug_info=debug_info,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_chunks(self, data: Any, customer_id: str) -> list[ContentChunk]:
        """Turn each top-level data section into a referenceable ContentChunk."""
        scope = customer_id or "book"
        if isinstance(data, list):
            sections: dict[str, Any] = {"records": data}
        elif isinstance(data, dict):
            sections = {k: v for k, v in data.items() if k != "customer_id"}
        else:
            sections = {}

        chunks: list[ContentChunk] = []
        for section_name, section_data in sections.items():
            if not section_data:
                continue
            text = json.dumps(section_data, ensure_ascii=True, indent=2)
            if len(text) < _CHUNK_MIN_CHARS:
                continue
            chunks.append(
                ContentChunk(
                    id=f"{self._spec.domain}_{scope}_{self._spec.name}_{section_name}",
                    chunk_id="0",
                    text=text[:_CHUNK_MAX_CHARS],
                    metadata={
                        "customer_id": customer_id,
                        "section": section_name,
                        "source": self._spec.domain,
                        "tool": self._spec.name,
                    },
                )
            )
        return chunks
