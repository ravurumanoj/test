
"""CRM retrieval tools for the CRM sub-agent.

Four focused query methods — each merges the data fields that naturally
belong together so API routes and the sub-agent never over-fetch.

Methods
-------
get_all_customers_summary  — RM pipeline overview (all customers)
get_customer_full_profile  — Demographics + account metadata + RM info
get_interactions           — Conversation history + open service requests
get_advisory_view          — Suggestions + compliance flags + alerts
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.data_loader import BaseDataTools

logger = logging.getLogger(__name__)


class CrmTools(BaseDataTools):
    """Expose CRM-specific retrieval operations over local JSON data."""

    def __init__(self) -> None:
        """Initialize with the CRM data file."""
        super().__init__("crm.json")

    def get_all_customers_summary(self) -> list[dict[str, Any]]:
        """Return a pipeline-level summary for every customer.

        Merges segment, NPS, churn risk, last interaction, pending follow-ups,
        open suggestions, and compliance flags into one lightweight record per
        customer — suitable for RM daily dashboards and retention reviews.

        Returns:
            List of summary dicts (one per customer).
        """
        result = []
        for rec in self._all_records():
            meta = rec.get("account_metadata", {})
            profile = rec.get("customer_profile", {})
            rm = rec.get("relationship_manager", {})
            pending_followups = sum(
                1
                for c in rec.get("conversation_history", [])
                if c.get("follow_up_required") and c.get("follow_up_date")
            )
            pending_suggestions = sum(
                1
                for s in rec.get("suggestions_provided", [])
                if s.get("status") in ("pending", "in_progress")
            )
            result.append(
                {
                    "customer_id": rec.get("customer_id"),
                    "name": profile.get("name"),
                    "segment": profile.get("segment"),
                    "relationship_manager": rm.get("name"),
                    "nps_score": meta.get("nps_score"),
                    "churn_risk": meta.get("churn_risk"),
                    "last_interaction_date": meta.get("last_interaction_date"),
                    "total_interactions_ytd": meta.get("total_interactions_ytd"),
                    "pending_followups": pending_followups,
                    "pending_suggestions": pending_suggestions,
                    "open_compliance_flags": len(rec.get("compliance_flags", [])),
                    "alerts": rec.get("alerts", []),
                }
            )
        logger.info("CRM customer summary list built", extra={"count": len(result)})
        return result

    def get_customer_full_profile(self, customer_id: str) -> dict[str, Any]:
        """Return customer demographics, account metadata, and RM info.

        Merges the three identity/relationship sections so callers get complete
        pre-meeting context — contact details, KYC status, segment, lifetime
        value, NPS, churn risk, and the assigned RM — in a single call.

        Args:
            customer_id: Unique customer identifier (e.g. ``CUST-1001``).

        Returns:
            Dict with ``customer_profile``, ``account_metadata``, and
            ``relationship_manager``.
        """
        rec = self._find_customer(customer_id)
        logger.info("Full CRM profile fetched", extra={"customer_id": customer_id})
        return {
            "customer_id": rec.get("customer_id"),
            "customer_profile": rec.get("customer_profile", {}),
            "account_metadata": rec.get("account_metadata", {}),
            "relationship_manager": rec.get("relationship_manager", {}),
        }

    def get_interactions(
        self,
        customer_id: str,
        channel: str | None = None,
        sentiment: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return conversation history and open service requests.

        Merges both interaction types — conversations and service tickets —
        with optional filtering so callers can scope to a specific channel,
        sentiment, or recent-N conversations without a second round-trip.

        Args:
            customer_id: Unique customer identifier.
            channel: Optional filter — ``phone``, ``email``, ``in_person``,
                ``video_call``, or ``app_chat`` (case-insensitive).
            sentiment: Optional filter — ``positive``, ``neutral``, or
                ``negative`` (case-insensitive).
            limit: If set, return only the most recent *N* conversations.

        Returns:
            Dict with ``conversations`` (filtered/limited list) and
            ``open_service_requests``.
        """
        rec = self._find_customer(customer_id)
        convs: list[dict[str, Any]] = rec.get("conversation_history", [])

        if channel:
            convs = [c for c in convs if c.get("channel", "").lower() == channel.lower()]
        if sentiment:
            convs = [c for c in convs if c.get("sentiment", "").lower() == sentiment.lower()]
        if limit is not None and limit > 0:
            convs = convs[:limit]

        open_sr = [r for r in rec.get("service_requests", []) if r.get("status") != "resolved"]

        logger.info(
            "Interactions fetched",
            extra={
                "customer_id": customer_id,
                "conversations": len(convs),
                "open_service_requests": len(open_sr),
            },
        )
        return {
            "customer_id": rec.get("customer_id"),
            "conversations": convs,
            "open_service_requests": open_sr,
        }

    def get_advisory_view(self, customer_id: str) -> dict[str, Any]:
        """Return suggestions, compliance flags, and active alerts.

        Groups the three advisory-related sections that an RM reviews before
        each client interaction: what was recommended, compliance blockers,
        and outstanding actions requiring attention.

        Args:
            customer_id: Unique customer identifier.

        Returns:
            Dict with ``suggestions_provided`` (all), ``pending_suggestions``
            (filtered subset), ``compliance_flags``, and ``alerts``.
        """
        rec = self._find_customer(customer_id)
        all_suggestions: list[dict[str, Any]] = rec.get("suggestions_provided", [])
        pending = [s for s in all_suggestions if s.get("status") in ("pending", "in_progress")]

        logger.info("Advisory view fetched", extra={"customer_id": customer_id})
        return {
            "customer_id": rec.get("customer_id"),
            "suggestions_provided": all_suggestions,
            "pending_suggestions": pending,
            "compliance_flags": rec.get("compliance_flags", []),
            "alerts": rec.get("alerts", []),
        }

    def get_customer_crm(self, customer_id: str) -> dict[str, Any]:
        """Return a comprehensive CRM view combining profile, interactions, and advisory.

        Aggregates get_customer_full_profile, get_interactions, and get_advisory_view
        into a single payload so the CRM sub-agent has all relationship-management
        context in one call without multiple round-trips.

        Args:
            customer_id: Unique customer identifier (e.g. ``CUST-1001``).

        Returns:
            Dict with ``customer_profile``, ``account_metadata``,
            ``relationship_manager``, ``conversation_history``,
            ``open_service_requests``, ``suggestions_provided``,
            ``pending_suggestions``, ``compliance_flags``, and ``alerts``.
        """
        rec = self._find_customer(customer_id)
        all_suggestions: list[dict[str, Any]] = rec.get("suggestions_provided", [])
        pending_suggestions = [s for s in all_suggestions if s.get("status") in ("pending", "in_progress")]
        open_sr = [r for r in rec.get("service_requests", []) if r.get("status") != "resolved"]

        logger.info("Full CRM context fetched", extra={"customer_id": customer_id})
        return {
            "customer_id": rec.get("customer_id"),
            "customer_profile": rec.get("customer_profile", {}),
            "account_metadata": rec.get("account_metadata", {}),
            "relationship_manager": rec.get("relationship_manager", {}),
            "conversation_history": rec.get("conversation_history", []),
            "open_service_requests": open_sr,
            "suggestions_provided": all_suggestions,
            "pending_suggestions": pending_suggestions,
            "compliance_flags": rec.get("compliance_flags", []),
            "alerts": rec.get("alerts", []),
        }
