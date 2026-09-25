"""Retrieval tools for the single-account portfolio statement.

``portfolio_data.json`` is a single detailed account statement (not a list of
customer records like ``portfolio.json``), so it is loaded independently here.
The methods below expose the statement in focused slices so the LLM can fetch
the exact section needed without losing newer fields added to the source file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.services.data_loader import JsonDataLoader

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"

# Internal/OCR-transcription fields — never client-facing (would read like an internal
# processing note rather than customer-relevant portfolio information).
_PORTFOLIO_INTERNAL_FIELDS = ("data_quality_note", "statement_type")


def _strip_internal_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *record* without internal OCR/unverified-field notes."""
    clean = {k: v for k, v in record.items() if k not in _PORTFOLIO_INTERNAL_FIELDS}
    clean.pop("unverified_fields", None)
    return clean


class PortfolioStatementTools:
    """Expose retrieval operations over the single-account statement JSON."""

    def __init__(self) -> None:
        """Initialize with the statement data file."""
        self._loader = JsonDataLoader(_DATA_DIR)
        self._filename = "portfolio_data.json"

    def _data(self) -> dict[str, Any]:
        """Return the parsed statement document."""
        return self._loader.load(self._filename)

    def _check_portfolio_id(self, portfolio_id: str, doc: dict[str, Any]) -> None:
        """Log a warning when the requested portfolio_id doesn't match this statement.

        This POC only has one account statement on file, so a mismatch never blocks
        the read — it's a traceability signal for when multiple statements are added.
        """
        actual = doc.get("portfolio", {}).get("portfolio_id", "")
        if portfolio_id and actual and portfolio_id.strip().upper() != str(actual).upper():
            logger.warning(
                "Requested portfolio_id does not match the available statement",
                extra={"requested_portfolio_id": portfolio_id, "actual_portfolio_id": actual},
            )

    def get_statement_overview(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return account metadata and the total assets/liabilities/net-total summary.

        Args:
            portfolio_id: Account/portfolio identifier (e.g. ``GO00001``); logged and
                checked against the statement on file.

        Returns:
            Dict with ``portfolio`` (account metadata, risk profile, valuation
            date) and ``summary`` (total assets, total liabilities, net total,
            and currency allocation, each with a per-currency percentage).
            Internal OCR/data-quality notes are stripped before returning.
        """
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        logger.info(
            "Statement overview fetched",
            extra={"portfolio_id": portfolio_id or doc.get("portfolio", {}).get("portfolio_id")},
        )
        return {
            "portfolio": _strip_internal_fields(doc.get("portfolio", {})),
            "summary": doc.get("summary", {}),
        }

    def get_holdings(self, portfolio_id: str = "", asset_class: str | None = None) -> list[dict[str, Any]]:
        """Return individual holdings, optionally filtered by asset class.

        Args:
            portfolio_id: Account/portfolio identifier (e.g. ``GO00001``); logged and
                checked against the statement on file.
            asset_class: Optional asset class filter (e.g. ``EQUITY``,
                ``BOND``, ``CASH_AND_CURRENT_ACCOUNTS``). Case-insensitive.

        Returns:
            List of holding dicts (instrument, quantity, prices, market value, and
            P&L). Internal OCR ``unverified_fields`` notes are stripped before returning.
        """
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        holdings = doc.get("holdings", [])
        if asset_class:
            needle = asset_class.strip().upper()
            holdings = [h for h in holdings if h.get("asset_class", "").upper() == needle]
        logger.info("Holdings fetched", extra={"asset_class": asset_class, "count": len(holdings)})
        return [_strip_internal_fields(h) for h in holdings]

    def get_allocation_breakdown(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return asset-class totals, geographic allocation, and reported subtotals.

        Args:
            portfolio_id: Account/portfolio identifier (e.g. ``GO00001``); logged and
                checked against the statement on file.

        Returns:
            Dict with ``asset_class_totals``, ``country_allocation``, and
            ``reported_subtotals``.
        """
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        return {
            "asset_class_totals": doc.get("asset_class_totals", {}),
            "country_allocation": doc.get("country_allocation", []),
            "reported_subtotals": doc.get("reported_subtotals", []),
        }

    def get_credit_and_fx_info(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return off-balance-sheet credit facilities and the FX rates used.

        Args:
            portfolio_id: Account/portfolio identifier (e.g. ``GO00001``); logged and
                checked against the statement on file.

        Returns:
            Dict with ``off_balance_sheet`` (e.g. credit lines) and
            ``exchange_rates`` (per-currency rate and rate date).
        """
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        return {
            "off_balance_sheet": doc.get("off_balance_sheet", []),
            "exchange_rates": doc.get("exchange_rates", []),
            "off_balance_sheet_summary": doc.get("off_balance_sheet_summary", {}),
        }

    def get_transactions(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return transaction-period metadata and all activity sections.

        Includes trade history, option activity, income events, corporate actions,
        and cash movements for the statement period.
        """
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        return {"transactions": doc.get("transactions", {})}

    def get_performance(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return performance, attribution, benchmark, and PE reporting sections."""
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        return {"performance": doc.get("performance", {})}

    def get_valuation_history(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return historical valuation snapshots and current composition context."""
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        return {"valuation_history": doc.get("valuation_history", [])}

    def get_risk_and_metadata(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return concentration, geographic metadata, and statement-level flags.

        This groups the remaining non-transactional analytical fields so no source
        data is stranded outside the tool surface.
        """
        doc = self._data()
        self._check_portfolio_id(portfolio_id, doc)
        return {
            "country_allocation_metadata": doc.get("country_allocation_metadata", {}),
            "concentration_indicators": doc.get("concentration_indicators", {}),
            "data_quality_flags": doc.get("data_quality_flags", {}),
        }
