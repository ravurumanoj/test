
"""API routes for the relationship manager POC."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter

from app.agents.relationship_manager import RelationshipManagerOrchestrator
from app.schemas import HealthResponse, RelationshipManagerRequest, RelationshipManagerResponse
from app.settings import Settings

try:
    from unique_toolkit.language_model import LanguageModelName
    _LANGUAGE_MODEL_NAME_AVAILABLE = True
except ImportError:
    LanguageModelName = None  # type: ignore[assignment]
    _LANGUAGE_MODEL_NAME_AVAILABLE = False

logger = logging.getLogger(__name__)


def create_router(orchestrator: RelationshipManagerOrchestrator, settings: Settings) -> APIRouter:
    """Create the API router with health and relationship-manager endpoints."""
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Return a basic health response for service checks."""
        logger.info("Health endpoint requested")
        return HealthResponse(status="ok", app_name=settings.app_name, environment=settings.app_env)

    @router.get("/models")
    async def list_models() -> dict[str, Any]:
        """Return all model identifiers supported by unique_toolkit, grouped by provider.

        These are the valid values for the UNIQUE_MODEL_NAME environment variable.
        Note: your Unique workspace may only have a subset enabled.
        Call GET /models/live to query the workspace-specific list via the Unique API.
        """
        logger.info("Models list requested")
        if not _LANGUAGE_MODEL_NAME_AVAILABLE:
            return {
                "available": False,
                "reason": "unique_toolkit not installed",
                "models": [],
            }

        providers: dict[str, list[dict[str, str]]] = {}
        for model in LanguageModelName:
            value: str = model.value
            if value.startswith("litellm:anthropic"):
                provider = "Anthropic (Claude)"
            elif value.startswith("litellm:vertex-claude"):
                provider = "Vertex AI (Claude)"
            elif value.startswith("litellm:gemini"):
                provider = "Google (Gemini)"
            elif value.startswith("litellm:openai"):
                provider = "OpenAI (via LiteLLM)"
            elif value.startswith("litellm:deepseek"):
                provider = "DeepSeek"
            elif value.startswith("litellm:grok"):
                provider = "Grok (xAI)"
            elif value.startswith("litellm:"):
                provider = "Other (LiteLLM)"
            elif value.startswith("AZURE_o"):
                provider = "Azure OpenAI (Reasoning)"
            elif value.startswith("AZURE_"):
                provider = "Azure OpenAI"
            else:
                provider = "Other"

            providers.setdefault(provider, []).append(
                {"name": model.name, "value": value}
            )

        total = sum(len(v) for v in providers.values())
        current_model = settings.unique_model_name
        logger.info("Models list returned", extra={"total_models": total, "current_model": current_model})
        return {
            "current_model": current_model,
            "total_models": total,
            "models_by_provider": providers,
        }

    @router.post("/relationship-manager/query", response_model=RelationshipManagerResponse)
    async def relationship_manager_query(request: RelationshipManagerRequest) -> RelationshipManagerResponse:
        """Handle a relationship manager question through the orchestrator."""
        logger.info(
            "Relationship manager query received",
            extra={
                "customer_id": request.customer_id,
                "question": request.question,
                "question_length": len(request.question),
            },
        )
        t0 = time.perf_counter()
        response = await orchestrator.handle_request(request)
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        logger.info(
            "Relationship manager query completed",
            extra={
                "customer_id": request.customer_id,
                "elapsed_ms": elapsed_ms,
                "routing_decision": response.routing_decision,
                "agent_answer_count": len(response.agent_answers),
                "evaluation_count": len(response.evaluation_results),
                "final_answer_length": len(response.final_answer),
                "final_answer_preview": response.final_answer[:300],
            },
        )
        return response

    return router