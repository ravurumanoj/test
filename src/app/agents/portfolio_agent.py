"""Portfolio data tools — each portfolio API exposed as an individual LLM tool.

The LLM chooses which of these to call based on the question; nothing is
hardcoded. Each wraps a method on ``PortfolioTools`` via the generic
``DataQueryTool``.

DISCONNECTED: ``PortfolioTools`` reads the old sample dataset
(``data/portfolio.json`` — 5 hardcoded customers incl. CUST-1001 "Rajesh
Kumar"). All tools in this module are now commented out so the LLM can never
call them and old data can never leak into an answer. Live portfolio queries
must go through ``portfolio_statement_agent.py`` / ``PortfolioStatementTools``,
which reads the current ``data/portfolio_data.json`` statement instead.

Tools
-----
All tools below (portfolio_recent_activity, portfolio_snapshot,
portfolio_performance, portfolio_compliance, portfolio_book_summary) are
disabled — see the commented block below. ``build_portfolio_tools`` returns
an empty list.
"""

from __future__ import annotations

import logging

# from app.agents.data_query_tool import DataQuerySpec, DataQueryTool  # noqa: disabled with old tools below
from app.agents.data_query_tool import DataQueryTool

# from app.agents.prompts import PORTFOLIO_AGENT_PROMPT, PORTFOLIO_RECENT_ACTIVITY_PROMPT  # unused while disabled
# from app.services.portfolio_tools import PortfolioTools  # OLD data source (portfolio.json) — disconnected
from app.services.unique_toolkit import UniqueToolkit

logger = logging.getLogger(__name__)


def build_portfolio_tools(unique_toolkit: UniqueToolkit) -> list[DataQueryTool]:
    """Return an empty list — old ``portfolio.json``-backed tools are disconnected.

    Kept as a no-op factory (rather than removed) so ``main.py`` wiring doesn't
    need to change. Use ``build_portfolio_statement_tools`` (portfolio_data.json)
    for all live portfolio queries instead.
    """
    del unique_toolkit  # unused while disabled
    return []
    # tools = PortfolioTools()

    # NOTE: Tool connectivity temporarily disabled — kept below for later restoration.
    # tools = PortfolioTools()
    # specs = [
    #     DataQuerySpec(
    #         name="portfolio_snapshot",
    #         domain="portfolio",
    #         description=(
    #             "Get a customer's current portfolio position: holdings, asset allocation, "
    #             "and profit & loss (realised and unrealised). Use for questions about what a "
    #             "customer holds, their allocation mix, position values, or P&L."
    #         ),
    #         prompt_hint=(
    #             "Use portfolio_snapshot for holdings, asset allocation, position values, or P&L."
    #         ),
    #         summarize_prompt=PORTFOLIO_AGENT_PROMPT,
    #         fetch=tools.get_portfolio_snapshot,
    #     ),
    #     DataQuerySpec(
    #         name="portfolio_performance",
    #         domain="portfolio",
    #         description=(
    #             "Get a customer's portfolio performance: time-period returns, risk metrics "
    #             "(YTD return, alpha, Sharpe, benchmark), sector and geographic exposure, and "
    #             "upcoming events. Use for questions about performance, returns, risk, exposure, "
    #             "or how the portfolio is doing versus the benchmark."
    #         ),
    #         prompt_hint=(
    #             "Use portfolio_performance for returns, alpha, Sharpe, benchmark comparison, "
    #             "sector/geographic exposure, or upcoming events."
    #         ),
    #         summarize_prompt=PORTFOLIO_AGENT_PROMPT,
    #         fetch=tools.get_performance_view,
    #     ),
    #     DataQuerySpec(
    #         name="portfolio_compliance",
    #         domain="portfolio",
    #         description=(
    #             "Get a customer's portfolio compliance view: line of credit (LOC) usage, tax "
    #             "summary, and active alerts. Use for questions about credit facilities, tax "
    #             "exposure, or outstanding portfolio alerts."
    #         ),
    #         prompt_hint=(
    #             "Use portfolio_compliance for line of credit, tax summary, or portfolio alerts."
    #         ),
    #         summarize_prompt=PORTFOLIO_AGENT_PROMPT,
    #         fetch=tools.get_compliance_view,
    #     ),
    #     DataQuerySpec(
    #         name="portfolio_book_summary",
    #         domain="portfolio",
    #         description=(
    #             "Get a high-level portfolio summary for EVERY customer (book of business): AUM, "
    #             "YTD return, alpha, risk profile, and alert counts per customer. Use only for "
    #             "cross-customer or book-wide questions, NOT for a single named customer."
    #         ),
    #         prompt_hint=(
    #             "Use portfolio_book_summary only for book-wide / all-customer overviews."
    #         ),
    #         summarize_prompt=PORTFOLIO_AGENT_PROMPT,
    #         fetch=tools.get_all_portfolios_summary,
    #         requires_customer=False,
    #     ),
    # ]
    # built = [DataQueryTool(spec=spec, unique_toolkit=unique_toolkit) for spec in specs]
    # logger.info(
    #     "Portfolio tools built",
    #     extra={"tool_names": [t.name for t in built]},
    # )
    # return built

    # DISCONNECTED (old data): this was the last remaining tool reading portfolio.json
    # (via PortfolioTools.get_recent_activity_digest) — it caused CUST-1001/"Rajesh Kumar"
    # sample data to appear in webhook answers. Commented out; use the statement_* tools
    # (portfolio_statement_agent.py, portfolio_data.json) for all portfolio queries now.
    # specs = [
    #     DataQuerySpec(
    #         name="portfolio_recent_activity",
    #         domain="portfolio",
    #         description=(
    #             "Get a 'what's new since your last visit' digest for a customer: portfolio "
    #             "value and cash changes, dividends received, top performing and declining "
    #             "holdings, and current asset/geographic/sector allocation. Use for questions "
    #             "like 'what's changed', 'what's new in my portfolio', 'recent activity', or "
    #             "'give me an update since I last checked'."
    #         ),
    #         prompt_hint=(
    #             "Use portfolio_recent_activity for a 'what's new since last visit' recap: "
    #             "value/cash changes, dividends, top movers, and current allocation."
    #         ),
    #         summarize_prompt=PORTFOLIO_RECENT_ACTIVITY_PROMPT,
    #         fetch=tools.get_recent_activity_digest,
    #     ),
    # ]
    # built = [DataQueryTool(spec=spec, unique_toolkit=unique_toolkit) for spec in specs]
    # logger.info(
    #     "Portfolio tools built",
    #     extra={"tool_names": [t.name for t in built]},
    # )
    # return built
