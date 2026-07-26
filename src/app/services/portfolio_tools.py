
"""Portfolio retrieval tools for the portfolio sub-agent.

Four focused query methods — each merges the data fields that naturally
belong together so API routes and the sub-agent never over-fetch.

Methods
-------
get_all_portfolios_summary  — RM book-of-business overview (all customers)
get_portfolio_snapshot      — Holdings + asset allocation + P&L for one customer
get_performance_view        — Returns, risk metrics, sector/geo exposure, events
get_compliance_view         — LOC, tax summary, and active alerts
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
