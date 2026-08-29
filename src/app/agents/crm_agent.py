"""CRM data tools — each CRM API exposed as an individual LLM tool.

The LLM chooses which of these to call based on the question; nothing is
hardcoded. All four wrap methods on ``CrmTools`` via the generic
``DataQueryTool``.

Tools
-----
crm_profile       — demographics + account metadata + assigned RM
crm_interactions  — conversation history + open service requests (filterable)
crm_advisory      — suggestions + compliance flags + active alerts
crm_book_summary  — pipeline overview across ALL customers
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
                "Get a customer's CRM identity: demographics, account metadata (segment, NPS, "
                "churn risk, tenure, lifetime value, KYC status), and the assigned relationship "
                "manager. Use for questions about who the customer is, their segment, KYC, NPS, "
                "churn risk, or which RM owns the relationship."
            ),
            prompt_hint=(
                "Use crm_profile for customer demographics, segment, KYC, NPS, churn risk, or the assigned RM."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_customer_full_profile,
        ),
        DataQuerySpec(
            name="crm_interactions",
            domain="crm",
            description=(
                "Get a customer's interaction history (conversations) and open service requests. "
                "Use for questions about past meetings, calls, emails, conversation history, "
                "sentiment, follow-ups, or open service tickets. Optional filters let you scope "
                "to a channel, a sentiment, or the most recent N conversations."
            ),
            prompt_hint=(
                "Use crm_interactions for conversation history, past meetings/calls, sentiment, "
                "follow-ups, or open service requests."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_interactions,
            optional_parameters={
                "channel": {
                    "type": "string",
                    "enum": ["phone", "email", "in_person", "video_call", "app_chat"],
                    "description": "Optional channel filter for conversations.",
                },
                "sentiment": {
                    "type": "string",
                    "enum": ["positive", "neutral", "negative"],
                    "description": "Optional sentiment filter for conversations.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional — return only the most recent N conversations.",
                },
            },
        ),
        DataQuerySpec(
            name="crm_advisory",
            domain="crm",
            description=(
                "Get a customer's advisory view: suggestions provided (with status), compliance "
                "flags, and active alerts. Use for questions about recommendations made, pending "
                "advice, compliance blockers, or outstanding actions."
            ),
            prompt_hint=(
                "Use crm_advisory for suggestions/recommendations, compliance flags, or CRM alerts."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_advisory_view,
        ),
        DataQuerySpec(
            name="crm_book_summary",
            domain="crm",
            description=(
                "Get a pipeline-level summary for EVERY customer: segment, NPS, churn risk, last "
                "interaction, pending follow-ups, open suggestions, and compliance flags per "
                "customer. Use only for cross-customer or book-wide questions, NOT for a single "
                "named customer."
            ),
            prompt_hint=(
                "Use crm_book_summary only for book-wide / all-customer pipeline overviews."
            ),
            summarize_prompt=CRM_AGENT_PROMPT,
            fetch=tools.get_all_customers_summary,
            requires_customer=False,
        ),
    ]
    built = [DataQueryTool(spec=spec, unique_toolkit=unique_toolkit) for spec in specs]
    logger.info(
        "CRM tools built",
        extra={"tool_names": [t.name for t in built]},
    )
    return built
