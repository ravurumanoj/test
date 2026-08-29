"""Portfolio data tools — each portfolio API exposed as an individual LLM tool.

The LLM chooses which of these to call based on the question; nothing is
hardcoded. All four wrap methods on ``PortfolioTools`` via the generic
``DataQueryTool``.

Tools
-----
portfolio_snapshot     — holdings + asset allocation + P&L for one customer
portfolio_performance  — returns, risk metrics, sector/geo exposure, events
portfolio_compliance   — line of credit, tax summary, active alerts
portfolio_book_summary — book-of-business overview across ALL customers
"""

from __future__ import annotations

import logging

from app.agents.data_query_tool import DataQuerySpec, DataQueryTool
from app.agents.prompts import PORTFOLIO_AGENT_PROMPT
from app.services.portfolio_tools import PortfolioTools
from app.services.unique_toolkit import UniqueToolkit

logger = logging.getLogger(__name__)


def build_portfolio_tools(unique_toolkit: UniqueToolkit) -> list[DataQueryTool]:
    """Build the four granular portfolio tools bound to a shared PortfolioTools instance."""
    tools = PortfolioTools()
    specs = [
        DataQuerySpec(
            name="portfolio_snapshot",
            domain="portfolio",
            description=(
                "Get a customer's current portfolio position: holdings, asset allocation, "
                "and profit & loss (realised and unrealised). Use for questions about what a "
                "customer holds, their allocation mix, position values, or P&L."
            ),
            prompt_hint=(
                "Use portfolio_snapshot for holdings, asset allocation, position values, or P&L."
            ),
            summarize_prompt=PORTFOLIO_AGENT_PROMPT,
            fetch=tools.get_portfolio_snapshot,
        ),
        DataQuerySpec(
            name="portfolio_performance",
            domain="portfolio",
            description=(
                "Get a customer's portfolio performance: time-period returns, risk metrics "
                "(YTD return, alpha, Sharpe, benchmark), sector and geographic exposure, and "
                "upcoming events. Use for questions about performance, returns, risk, exposure, "
                "or how the portfolio is doing versus the benchmark."
            ),
            prompt_hint=(
                "Use portfolio_performance for returns, alpha, Sharpe, benchmark comparison, "
                "sector/geographic exposure, or upcoming events."
            ),
            summarize_prompt=PORTFOLIO_AGENT_PROMPT,
            fetch=tools.get_performance_view,
        ),
        DataQuerySpec(
            name="portfolio_compliance",
            domain="portfolio",
            description=(
                "Get a customer's portfolio compliance view: line of credit (LOC) usage, tax "
                "summary, and active alerts. Use for questions about credit facilities, tax "
                "exposure, or outstanding portfolio alerts."
            ),
            prompt_hint=(
                "Use portfolio_compliance for line of credit, tax summary, or portfolio alerts."
            ),
            summarize_prompt=PORTFOLIO_AGENT_PROMPT,
            fetch=tools.get_compliance_view,
        ),
        DataQuerySpec(
            name="portfolio_book_summary",
            domain="portfolio",
            description=(
                "Get a high-level portfolio summary for EVERY customer (book of business): AUM, "
                "YTD return, alpha, risk profile, and alert counts per customer. Use only for "
                "cross-customer or book-wide questions, NOT for a single named customer."
            ),
            prompt_hint=(
                "Use portfolio_book_summary only for book-wide / all-customer overviews."
            ),
            summarize_prompt=PORTFOLIO_AGENT_PROMPT,
            fetch=tools.get_all_portfolios_summary,
            requires_customer=False,
        ),
    ]
    built = [DataQueryTool(spec=spec, unique_toolkit=unique_toolkit) for spec in specs]
    logger.info(
        "Portfolio tools built",
        extra={"tool_names": [t.name for t in built]},
    )
    return built
