"""CRM data tools — each CRM API exposed as an individual LLM tool.

The LLM chooses which of these to call based on the question; nothing is
hardcoded. All four wrap methods on ``CrmTools`` via the generic
``DataQueryTool``.

Tools
-----
crm_profile       — file metadata + client identity + assigned RM + linked account context
crm_interactions  — meetings, transcripts, email threads, excluded interactions
crm_advisory      — grouped CRM follow-up context and interaction overviews
crm_book_summary  — top-level CRM document summary for the linked account
"""

from __future__ import annotations

import logging

from app.agents.data_query_tool import DataQuerySpec, DataQueryTool
from app.agents.prompts import CRM_AGENT_PROMPT
from app.services.crm_tools import CrmTools
from app.services.unique_toolkit import UniqueToolkit

logger = logging.getLogger(__name__)


def build_crm_tools(unique_toolkit: UniqueToolkit) -> list[DataQueryTool]:
    """Build the four granular CRM tools bound to a shared CrmTools instance."""
    tools = CrmTools()
    specs = [
        DataQuerySpec(
            name="crm_profile",
            domain="crm",
            description=(
                "Get the CRM identity sections for the linked account: file metadata, client "
                "profile, linked account/portfolio context, and the assigned relationship manager. "
                "Use for questions about who the client is, KYC/review dates, preferred channel, "
                "linked accounts, or which RM owns the relationship."
            ),
            prompt_hint=(
                "Use crm_profile for client identity, linked account context, KYC/review dates, preferred channel, or the assigned RM."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_customer_full_profile,
            requires_portfolio_id=True,
        ),
        DataQuerySpec(
            name="crm_interactions",
            domain="crm",
            description=(
                "Get the CRM interaction sections for the linked account: meetings with full "
                "transcripts, email threads with raw messages, and excluded interactions. Use for "
                "questions about past meetings, calls, emails, what the client said, follow-ups, "
                "or interaction history. Optional filters let you scope by channel or recent count."
            ),
            prompt_hint=(
                "Use crm_interactions for meetings, transcripts, email threads, client statements, or interaction history."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_interactions,
            requires_portfolio_id=True,
            optional_parameters={
                "channel": {
                    "type": "string",
                    "enum": ["phone", "email", "telephony_recorded_line"],
                    "description": "Optional channel filter for meetings/email threads.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional — return only the most recent N meetings/email threads.",
                },
            },
        ),
        DataQuerySpec(
            name="crm_advisory",
            domain="crm",
            description=(
                "Get grouped CRM follow-up context for the linked account: top-level metadata, "
                "client/RM context, meeting overviews, email-thread overviews, and excluded "
                "interactions. Use for questions about what needs follow-up, what interactions "
                "exist, or how the CRM record is organised."
            ),
            prompt_hint=(
                "Use crm_advisory for grouped CRM follow-up context, interaction overviews, or excluded-interaction context."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_advisory_view,
            requires_portfolio_id=True,
        ),
        DataQuerySpec(
            name="crm_book_summary",
            domain="crm",
            description=(
                "Get the top-level CRM document summary for the linked account: file metadata, "
                "client identity, and relationship-manager ownership. Use for broad CRM summary "
                "questions when you need the document-level context first."
            ),
            prompt_hint=(
                "Use crm_book_summary for broad CRM document-level context for the linked account."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_book_summary,
            requires_customer=False,
            requires_portfolio_id=True,
        ),
    ]
    built = [DataQueryTool(spec=spec, unique_toolkit=unique_toolkit) for spec in specs]
    logger.info(
        "CRM tools built",
        extra={"tool_names": [t.name for t in built]},
    )
    return built
