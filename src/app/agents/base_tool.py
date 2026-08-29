
"""Abstract Tool base class following the Unique Toolkit agentic framework pattern.

Mirrors unique_toolkit.agentic.tools.tool.Tool and BaseToolConfig as described in:
  docs/unique_toolkit_agentic_framework_core.md

All sub-agents and custom tools must extend Tool and implement:
  name                — internal identifier matching LLM function call name
  tool_description()  — LLM-readable schema
  run()               — execute the tool and return ToolCallResponse
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.schemas import ToolCallResponse, ToolDescription

logger = logging.getLogger(__name__)


class BaseToolConfig:
    """Tool configuration base class.

    Mirrors unique_toolkit BaseToolConfig.
    In a full Unique Toolkit deployment this is a Pydantic model so the
    frontend can render configuration dynamically.  For this REST API POC
    it is a plain dataclass-style class with class-level defaults.

    Extend with custom fields for your tool's specific settings.
    """

    display_name: str = "Base Tool"
    icon: str = "🔧"
    is_enabled: bool = True
    is_exclusive: bool = False


class Tool(ABC):
    """Abstract base for every tool in the agentic framework.

    Mirrors unique_toolkit.agentic.tools.tool.Tool.

    Key properties
    --------------
    name             — Internal identifier; must match the function name the LLM
                       uses when calling this tool in its tool_calls response.
    is_enabled()     — Whether the tool can be selected by the orchestrator.
    is_exclusive()   — When True, only this tool runs in a given iteration batch.
    takes_control()  — When True, the orchestrator exits the loop after this tool
                       finishes (e.g. Deep Research hand-off pattern).

    Required abstract members
    -------------------------
    name                 — property
    tool_description()   — LLM-readable schema
    run()                — execute and return ToolCallResponse
    """

    def __init__(self, config: BaseToolConfig) -> None:
        """Initialize the tool with its configuration object."""
        self.settings = config

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the internal tool identifier.

        Must match the ``name`` value the LLM emits in its tool_calls payload.
        """

    def display_name(self) -> str:
        """Return the user-facing tool name shown in the UI."""
        return self.settings.display_name

    @property
    def domain(self) -> str:
        """Return the business domain this tool contributes to (e.g. 'portfolio', 'crm').

        Used by the orchestrator to group tool results into AgentAnswer records and
        to derive the routing decision. Empty string means the tool does not map to
        a sub-agent domain (e.g. MCP tools).
        """
        return ""

    def icon(self) -> str:
        """Return the UI icon character for this tool."""
        return self.settings.icon

    # ── Lifecycle flags ───────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """Return True when this tool is active and available for selection.

        Mirrors Tool.is_enabled() from docs — disabled tools are excluded by
        ToolManager during the filtering step.
        """
        return self.settings.is_enabled

    def is_exclusive(self) -> bool:
        """Return True when this tool must run alone in its iteration batch.

        Mirrors Tool.is_exclusive() from docs — if any loaded tool is exclusive,
        the ToolManager runs only that tool.
        """
        return self.settings.is_exclusive

    def takes_control(self) -> bool:
        """Return True when this tool takes over the orchestrator loop.

        Mirrors Tool.takes_control() from docs — when True the orchestrator
        exits after this tool finishes (e.g. Deep Research hand-off pattern).
        Default is False for most tools.
        """
        return False

    # ── LLM schema ────────────────────────────────────────────────────────────

    @abstractmethod
    def tool_description(self) -> ToolDescription:
        """Return the LLM-readable tool schema for the orchestrator planning step.

        Mirrors LanguageModelToolDescription in unique_toolkit.
        """

    def tool_description_for_system_prompt(self) -> str:
        """Return additional guidance text injected into the system prompt.

        Mirrors Tool.tool_description_for_system_prompt() from docs.
        Use this to add nuanced hints beyond the JSON schema description.
        """
        return ""

    # ── Execution ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def run(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallResponse:
        """Execute the tool and return a structured ToolCallResponse.

        Mirrors Tool.run(tool_call: LanguageModelFunction) in unique_toolkit,
        adapted for the REST API context where request-level context (customer_id,
        question) is passed explicitly rather than through a ChatEvent.

        Args:
            tool_call_id: The ID assigned by the LLM for this tool invocation.
            arguments:    Parsed JSON arguments from the LLM function call.
            context:      Request-level context dict (customer_id, question, …).

        Returns:
            ToolCallResponse — fill content for plain text, content_chunks for
            referenceable citations, debug_info for DebugInfoManager, and
            error_message for failures.
        """
