
"""API routes for the relationship manager POC."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.agents.relationship_manager import RelationshipManagerOrchestrator
from app.schemas import HealthResponse, RelationshipManagerRequest, RelationshipManagerResponse
from app.settings import Settings

logger = logging.getLogger(__name__)


def create_router(orchestrator: RelationshipManagerOrchestrator, settings: Settings) -> APIRouter:
    """Create the API router with health and relationship-manager endpoints."""
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Return a basic health response for service checks."""
        logger.info("Health endpoint requested")
        return HealthResponse(status="ok", app_name=settings.app_name, environment=settings.app_env)

    @router.post("/relationship-manager/query", response_model=RelationshipManagerResponse)
    async def relationship_manager_query(request: RelationshipManagerRequest) -> RelationshipManagerResponse:
        """Handle a relationship manager question through the orchestrator."""
        logger.info(
            "Relationship manager query received",
            extra={"customer_id": request.customer_id, "question": request.question},
        )
        return await orchestrator.handle_request(request)

    return router