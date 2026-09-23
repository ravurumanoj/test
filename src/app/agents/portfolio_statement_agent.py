"""Portfolio statement tools — each statement section exposed as an individual LLM tool.

Wraps ``PortfolioStatementTools`` (single-account OCR-derived statement, see
``data/portfolio_data.json``) the same way ``portfolio_agent.py`` wraps the
multi-customer ``PortfolioTools``. The LLM chooses which tool to call; there is
no per-customer ``customer_id`` since the source data covers one account.

Tools
-----
statement_overview   — account/ID metadata, valuation date, risk profile, data-quality note,
                       + total assets/liabilities/net-total with currency allocation
statement_holdings   — individual holdings (instrument, ISIN/ticker, price, value, P&L),
                       optionally filtered by asset class
statement_allocation — asset-class totals + geographic (country) allocation + reported subtotals
statement_credit_fx  — off-balance-sheet credit lines + exchange rates used to value the statement
"""

from __future__ import annotations

import logging

from app.agents.data_query_tool import DataQuerySpec, DataQueryTool
from app.agents.prompts import PORTFOLIO_STATEMENT_PROMPT
from app.services.portfolio_statement_tools import PortfolioStatementTools
from app.services.unique_toolkit import UniqueToolkit

logger = logging.getLogger(__name__)


def build_portfolio_statement_tools(unique_toolkit: UniqueToolkit) -> list[DataQueryTool]:
    """Build the four statement tools bound to a shared PortfolioStatementTools instance."""
    tools = PortfolioStatementTools()
    specs = [
        DataQuerySpec(
            name="statement_overview",
            domain="portfolio",
            description=(
                "Get the account statement's identifying details and overall size: account/"
                "portfolio ID, account number and reference, reference currency, risk profile, "
                "valuation date, statement type, and a data-quality note (this statement was "
                "OCR-transcribed from a scanned document, so some values are pending manual "
                "verification). Also returns total assets, total liabilities, and net total in "
                "USD, each broken down by per-currency allocation percentage, plus the line-by-"
                "line asset breakdown. Use for questions about total portfolio value, net worth, "
                "AUM, currency exposure/allocation, valuation date, risk profile, account/"
                "reference numbers, or how reliable/verified the statement data is."
            ),
            prompt_hint=(
                "Use statement_overview for total assets/liabilities/net total, currency "
                "allocation, account metadata (risk profile, valuation date, account numbers), "
                "or data-quality/OCR reliability questions."
            ),
            summarize_prompt=PORTFOLIO_STATEMENT_PROMPT,
            fetch=tools.get_statement_overview,
            requires_customer=False,
            requires_portfolio_id=True,
        ),
        DataQuerySpec(
            name="statement_holdings",
            domain="portfolio",
            description=(
                "Get the individual holdings/positions on the account statement: asset class, "
                "instrument name, ISIN/ticker, currency, quantity, average and last price, "
                "market value (local and USD), % of portfolio, unrealised P&L, accrued interest, "
                "maturity/price dates, and country. Covers cash accounts, fixed term deposits, "
                "bonds, bond funds, structured products, equities, fund/ETFs, commodity ETFs, and "
                "private equity funds. Some fields were OCR-reconstructed and flagged as "
                "unverified in the response. Optionally filter by asset class. Use for questions "
                "about specific instruments, positions held, quantities, prices, ISIN/ticker "
                "lookups, or per-holding P&L."
            ),
            prompt_hint=(
                "Use statement_holdings for specific instruments/positions, quantities, prices, "
                "ISIN/ticker lookups, per-holding P&L, or which fields are OCR-unverified."
            ),
            summarize_prompt=PORTFOLIO_STATEMENT_PROMPT,
            fetch=tools.get_holdings,
            requires_customer=False,
            requires_portfolio_id=True,
            optional_parameters={
                "asset_class": {
                    "type": "string",
                    "description": (
                        "Optional asset class filter, e.g. EQUITY, BOND, BOND_FUND, "
                        "FUND_ETF, COMMODITY_ETF, STRUCTURED_PRODUCT, PRIVATE_EQUITY_FUND, "
                        "FIXED_TERM_DEPOSIT, CASH_AND_CURRENT_ACCOUNTS."
                    ),
                }
            },
        ),
        DataQuerySpec(
            name="statement_allocation",
            domain="portfolio",
            description=(
                "Get the account statement's asset-class totals (e.g. EQUITY, BOND, "
                "CASH_AND_CURRENT_ACCOUNTS market value and % of portfolio), geographic/country "
                "allocation per asset class, and reported subtotals (e.g. EUR/GBP equities, "
                "FUND_ETF grouped by pricing currency). Use for questions about asset-class mix "
                "or geographic/country exposure — NOT currency allocation (use statement_overview) "
                "and NOT individual holdings (use statement_holdings)."
            ),
            prompt_hint=(
                "Use statement_allocation for asset-class breakdown or geographic/country "
                "exposure (not currency allocation or individual holdings)."
            ),
            summarize_prompt=PORTFOLIO_STATEMENT_PROMPT,
            fetch=tools.get_allocation_breakdown,
            requires_customer=False,
            requires_portfolio_id=True,
        ),
        DataQuerySpec(
            name="statement_credit_fx",
            domain="portfolio",
            description=(
                "Get the account statement's off-balance-sheet items (e.g. the global credit "
                "line: reference, currency, amount, and start date) and the exchange rates used "
                "to value the statement (per-currency rate and rate date). Use for questions "
                "about credit facilities/lines of credit, or which FX rate/rate date was used to "
                "convert a currency to USD."
            ),
            prompt_hint=(
                "Use statement_credit_fx for credit facilities/lines of credit or FX rates/rate "
                "dates used in the statement."
            ),
            summarize_prompt=PORTFOLIO_STATEMENT_PROMPT,
            fetch=tools.get_credit_and_fx_info,
            requires_customer=False,
            requires_portfolio_id=True,
        ),
    ]
    built = [DataQueryTool(spec=spec, unique_toolkit=unique_toolkit) for spec in specs]
    logger.info(
        "Portfolio statement tools built",
        extra={"tool_names": [t.name for t in built]},
    )
    return built
