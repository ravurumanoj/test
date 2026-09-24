
"""Portfolio retrieval tools for the portfolio sub-agent.

Four focused query methods — each merges the data fields that naturally
belong together so API routes and the sub-agent never over-fetch.

Methods
-------
get_all_portfolios_summary  — RM book-of-business overview (all customers)
get_portfolio_snapshot      — Holdings + asset allocation + P&L for one customer
get_performance_view        — Returns, risk metrics, sector/geo exposure, events
get_compliance_view         — LOC, tax summary, and active alerts
get_recent_activity_digest  — "What's new since your last visit" recap: value/cash
                               changes, dividends, top movers, and current allocation
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.data_loader import BaseDataTools

logger = logging.getLogger(__name__)


class PortfolioTools(BaseDataTools):
    """Expose portfolio-specific retrieval operations over local JSON data."""

    def __init__(self) -> None:
        """Initialize with the portfolio data file."""
        super().__init__("portfolio.json")

    def get_all_portfolios_summary(self) -> list[dict[str, Any]]:
        """Return a high-level summary for every customer.

        Merges account details, portfolio summary KPIs, and alerts into one
        lightweight record per customer — suitable for RM dashboards and
        book-of-business overviews.

        Returns:
            List of summary dicts containing customer name, risk profile, AUM,
            YTD return, alpha, RM name, and alerts.
        """
        result = []
        for rec in self._all_records():
            profile = rec.get("customer_profile", {})
            acc = rec.get("account_details", {})
            summary = rec.get("portfolio_summary", {})
            metrics = rec.get("performance_metrics", {})
            result.append(
                {
                    "customer_id": rec.get("customer_id"),
                    "name": profile.get("name"),
                    "risk_profile": profile.get("risk_profile"),
                    "investment_horizon": profile.get("investment_horizon"),
                    "relationship_manager": acc.get("relationship_manager"),
                    "account_type": acc.get("account_type"),
                    "total_aum": summary.get("total_aum"),
                    "currency": summary.get("currency"),
                    "unrealized_pnl": summary.get("unrealized_pnl"),
                    "unrealized_pnl_pct": summary.get("unrealized_pnl_pct"),
                    "total_return_ytd_pct": summary.get("total_return_ytd_pct"),
                    "benchmark_ytd_pct": metrics.get("benchmark_ytd_pct"),
                    "alpha_pct": metrics.get("alpha_pct"),
                    "as_of_date": summary.get("as_of_date"),
                    "alert_count": len(rec.get("alerts", [])),
                    "alerts": rec.get("alerts", []),
                }
            )
        logger.info("Portfolio summary list built", extra={"count": len(result)})
        return result

    def get_portfolio_snapshot(self, customer_id: str) -> dict[str, Any]:
        """Return holdings, asset allocation, and P&L for one customer.

        Merges three naturally related sections — holdings detail, asset-class
        breakdown, and profit/loss statement — into a single response for
        position reviews and client meetings.

        Args:
            customer_id: Unique customer identifier (e.g. ``CUST-1001``).

        Returns:
            Dict with ``account_details``, ``customer_profile``,
            ``portfolio_summary``, ``asset_allocation``, ``holdings``,
            and ``pnl_summary``.
        """
        rec = self._find_customer(customer_id)
        logger.info(
            "Portfolio snapshot fetched",
            extra={
                "customer_id": customer_id,
                "holdings_count": len(rec.get("holdings", [])),
                "asset_allocation_classes": list(rec.get("asset_allocation", {}).keys()),
            },
        )
        return {
            "customer_id": rec.get("customer_id"),
            "account_details": rec.get("account_details", {}),
            "customer_profile": rec.get("customer_profile", {}),
            "portfolio_summary": rec.get("portfolio_summary", {}),
            "asset_allocation": rec.get("asset_allocation", {}),
            "holdings": rec.get("holdings", []),
            "pnl_summary": rec.get("pnl_summary", {}),
        }

    def get_performance_view(self, customer_id: str) -> dict[str, Any]:
        """Return performance metrics, exposure breakdowns, and upcoming events.

        Combines time-period returns and risk ratios with sector/geographic
        exposure and the event calendar — everything needed for an investment
        review or IPS compliance check.

        Args:
            customer_id: Unique customer identifier.

        Returns:
            Dict with ``performance_metrics``, ``sector_exposure``,
            ``geographic_exposure``, and ``upcoming_events``.
        """
        rec = self._find_customer(customer_id)
        logger.info(
            "Performance view fetched",
            extra={
                "customer_id": customer_id,
                "sector_count": len(rec.get("sector_exposure", {})),
                "upcoming_events": len(rec.get("upcoming_events", [])),
            },
        )
        return {
            "customer_id": rec.get("customer_id"),
            "performance_metrics": rec.get("performance_metrics", {}),
            "sector_exposure": rec.get("sector_exposure", {}),
            "geographic_exposure": rec.get("geographic_exposure", {}),
            "upcoming_events": rec.get("upcoming_events", []),
        }

    def get_compliance_view(self, customer_id: str) -> dict[str, Any]:
        """Return the LOC details, tax summary, and active alerts.

        Groups the three compliance-related sections that relationship managers
        check together: credit facility usage, tax exposure, and outstanding
        action alerts.

        Args:
            customer_id: Unique customer identifier.

        Returns:
            Dict with ``line_of_credit`` (or ``null``), ``tax_summary``,
            and ``alerts``.
        """
        rec = self._find_customer(customer_id)
        logger.info(
            "Compliance view fetched",
            extra={
                "customer_id": customer_id,
                "has_loc": bool(rec.get("line_of_credit")),
                "alert_count": len(rec.get("alerts", [])),
            },
        )
        return {
            "customer_id": rec.get("customer_id"),
            "line_of_credit": rec.get("line_of_credit"),
            "tax_summary": rec.get("tax_summary", {}),
            "alerts": rec.get("alerts", []),
        }

    def get_recent_activity_digest(self, customer_id: str) -> dict[str, Any]:
        """Return a "what's new since your last visit" digest for one customer.

        Some template concepts are not yet tracked by this data source — a stored
        last-visit date, a transaction-level buy/sell/income-event log, and
        currency/FX attribution — and are returned as ``None``. The summarisation
        prompt is instructed to silently omit any section it cannot back with real
        data rather than inventing or padding it.

        Top gainers/decliners are ranked here (not by the LLM) from each holding's
        ``unrealized_pnl_pct`` so the ranking is deterministic and never invented.

        Args:
            customer_id: Unique customer identifier.

        Returns:
            Dict with ``portfolio_value_summary``, ``cash_position``,
            ``dividends_received_ytd``, ``top_movers`` (gainers/decliners),
            ``asset_allocation``, ``sector_exposure``, ``geographic_exposure``,
            and the untracked fields set to ``None``.
        """
        rec = self._find_customer(customer_id)
        summary = rec.get("portfolio_summary", {})
        allocation = rec.get("asset_allocation", {})
        cash = allocation.get("cash_and_equivalents", {})
        holdings = [h for h in rec.get("holdings", []) if isinstance(h.get("unrealized_pnl_pct"), (int, float))]

        def _mover(h: dict[str, Any]) -> dict[str, Any]:
            return {
                "instrument_name": h.get("instrument_name"),
                "unrealized_pnl_pct": h.get("unrealized_pnl_pct"),
                "weight_in_portfolio_pct": h.get("weight_in_portfolio_pct"),
            }

        top_gainers = sorted((h for h in holdings if h["unrealized_pnl_pct"] > 0), key=lambda h: h["unrealized_pnl_pct"], reverse=True)[:3]
        top_decliners = sorted((h for h in holdings if h["unrealized_pnl_pct"] < 0), key=lambda h: h["unrealized_pnl_pct"])[:3]

        logger.info(
            "Recent activity digest fetched",
            extra={
                "customer_id": customer_id,
                "top_gainer_count": len(top_gainers),
                "top_decliner_count": len(top_decliners),
            },
        )
        return {
            "customer_id": rec.get("customer_id"),
            "as_of_date": summary.get("as_of_date"),
            "last_visit_date": None,  # not tracked yet by this data source
            "portfolio_value_summary": {
                "current_value": summary.get("current_value"),
                "invested_value": summary.get("invested_value"),
                "unrealized_pnl": summary.get("unrealized_pnl"),
                "unrealized_pnl_pct": summary.get("unrealized_pnl_pct"),
                "total_return_ytd_pct": summary.get("total_return_ytd_pct"),
                "realized_pnl_ytd": summary.get("realized_pnl_ytd"),
                "currency": summary.get("currency"),
            },
            "cash_position": {
                "value": cash.get("value"),
                "pct_of_total": cash.get("pct"),
            },
            "dividends_received_ytd": summary.get("dividends_received_ytd"),
            "transaction_history": None,  # buy/sell/income-event log not tracked yet
            "currency_movement_impact": None,  # FX attribution not tracked yet
            "top_movers": {
                "top_gainers": [_mover(h) for h in top_gainers],
                "top_decliners": [_mover(h) for h in top_decliners],
            },
            "asset_allocation": allocation,
            "sector_exposure": rec.get("sector_exposure", {}),
            "geographic_exposure": rec.get("geographic_exposure", {}),
        }
