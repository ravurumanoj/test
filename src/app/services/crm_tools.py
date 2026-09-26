
"""CRM retrieval tools for the CRM sub-agent.

The CRM source is a single account-linked document. These methods return grouped
top-level or second-level sections directly, rather than hardcoding every nested
field, so newly added CRM content remains available to the LLM automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.services.data_loader import JsonDataLoader

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"


class CrmTools:
    """Expose CRM-specific retrieval operations over the single CRM document."""

    def __init__(self) -> None:
        """Initialize with the CRM data file."""
        self._loader = JsonDataLoader(_DATA_DIR)
        self._filename = "crm.json"

    def _data(self) -> dict[str, Any]:
        """Return the parsed CRM document."""
        return self._loader.load(self._filename)

    def _matches_context(self, doc: dict[str, Any], customer_id: str = "", portfolio_id: str = "") -> None:
        """Log mismatches against the single CRM document without blocking reads."""
        linked_accounts = doc.get("client", {}).get("linked_accounts", [])
        linked_portfolio_ids = {
            str(account.get("portfolio_id", "")).upper()
            for account in linked_accounts
            if account.get("portfolio_id")
        }
        linked_account_numbers = {
            str(account.get("account_number", ""))
            for account in linked_accounts
            if account.get("account_number")
        }
        if portfolio_id and portfolio_id.strip().upper() not in linked_portfolio_ids:
            logger.warning(
                "Requested CRM portfolio_id does not match the available CRM document",
                extra={"requested_portfolio_id": portfolio_id, "available_portfolio_ids": sorted(linked_portfolio_ids)},
            )
        if customer_id and customer_id.strip() not in linked_account_numbers:
            logger.info(
                "CRM customer_id treated as contextual only for single-document CRM source",
                extra={"requested_customer_id": customer_id, "available_account_numbers": sorted(linked_account_numbers)},
            )

    def get_book_summary(self, portfolio_id: str = "") -> dict[str, Any]:
        """Return the CRM document's top-level identity and coverage summary."""
        doc = self._data()
        self._matches_context(doc, portfolio_id=portfolio_id)
        return {
            "client": doc.get("client", {}),
            "relationship_manager": doc.get("relationship_manager", {}),
        }

    def get_customer_full_profile(self, customer_id: str = "", portfolio_id: str = "") -> dict[str, Any]:
        """Return the CRM identity/profile sections for the linked account."""
        doc = self._data()
        self._matches_context(doc, customer_id=customer_id, portfolio_id=portfolio_id)
        logger.info("Full CRM profile fetched", extra={"customer_id": customer_id, "portfolio_id": portfolio_id})
        return {
            "client": doc.get("client", {}),
            "relationship_manager": doc.get("relationship_manager", {}),
        }

    def get_interactions(
        self,
        customer_id: str = "",
        portfolio_id: str = "",
        channel: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return grouped interaction sections from meetings and email threads."""
        doc = self._data()
        self._matches_context(doc, customer_id=customer_id, portfolio_id=portfolio_id)
        meetings: list[dict[str, Any]] = doc.get("meetings", [])
        email_threads: list[dict[str, Any]] = doc.get("email_threads", [])

        if channel:
            needle = channel.lower()
            meetings = [meeting for meeting in meetings if str(meeting.get("interaction_type", "")).lower() == needle or str(meeting.get("channel", "")).lower() == needle]
            email_threads = [thread for thread in email_threads if str(thread.get("channel", "email")).lower() == needle]
        if limit is not None and limit > 0:
            meetings = meetings[:limit]
            email_threads = email_threads[:limit]

        logger.info(
            "Interactions fetched",
            extra={
                "customer_id": customer_id,
                "portfolio_id": portfolio_id,
                "meetings": len(meetings),
                "email_threads": len(email_threads),
            },
        )
        return {
            "client": doc.get("client", {}),
            "meetings": meetings,
            "email_threads": email_threads,
            "excluded_interactions": doc.get("excluded_interactions", []),
        }

    def get_advisory_view(self, customer_id: str = "", portfolio_id: str = "") -> dict[str, Any]:
        """Return the non-interaction CRM sections relevant for RM follow-up.

        This intentionally returns grouped top-level sections instead of flattening
        nested keys, so newly added metadata remains available automatically.
        """
        doc = self._data()
        self._matches_context(doc, customer_id=customer_id, portfolio_id=portfolio_id)
        meetings = doc.get("meetings", [])
        email_threads = doc.get("email_threads", [])
        return {
            "client": doc.get("client", {}),
            "relationship_manager": doc.get("relationship_manager", {}),
            "meeting_overview": [
                {
                    "meeting_id": meeting.get("meeting_id"),
                    "title": meeting.get("title"),
                    "meeting_date": meeting.get("meeting_date"),
                    "interaction_type": meeting.get("interaction_type"),
                    "linked_portfolio_ids": meeting.get("linked_portfolio_ids", []),
                }
                for meeting in meetings
            ],
            "email_thread_overview": [
                {
                    "thread_id": thread.get("thread_id"),
                    "subject": thread.get("subject"),
                    "start_date": thread.get("start_date"),
                    "end_date": thread.get("end_date"),
                    "linked_portfolio_ids": thread.get("linked_portfolio_ids", []),
                }
                for thread in email_threads
            ],
            "excluded_interactions": doc.get("excluded_interactions", []),
        }
