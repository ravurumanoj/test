
"""API routes for the relationship manager POC."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agents.relationship_manager import RelationshipManagerOrchestrator
from app.schemas import (
    HealthResponse,
    RelationshipManagerRequest,
    RelationshipManagerResponse,
    WebhookEvent,
)
from app.services.session_service import UniqueSessionService
from app.settings import Settings

try:
    from unique_toolkit.language_model import LanguageModelName
    _LANGUAGE_MODEL_NAME_AVAILABLE = True
except ImportError:
    LanguageModelName = None  # type: ignore[assignment]
    _LANGUAGE_MODEL_NAME_AVAILABLE = False

logger = logging.getLogger(__name__)

# Events that carry a user message we should answer.
# TODO: replace these placeholder names with the real Unique event names later.
_ANSWERABLE_EVENTS = frozenset({"module.chosen", "user.message.created"})

# Sample payload shown in Swagger "Try it out" for the raw-body webhook endpoint.
# The handler reads request.body() directly (for HMAC verification), so FastAPI
# cannot auto-generate a schema — this example makes the endpoint testable in docs.
_WEBHOOK_EXAMPLE: dict[str, Any] = {
    "id": "evt_test_1",
    "version": "1.0.0",
    "event": "module.chosen",
    "createdAt": 1700000000,
    "userId": "user_abc",
    "companyId": "company_xyz",
    "payload": {
        "chatId": "chat_test_123",
        "assistantId": "assistant_test_123",
        "text": "How is my portfolio performing this quarter?",
        "userMessage": {"id": "msg_user_1", "text": "How is my portfolio performing this quarter?"},
        "assistantMessage": {"id": "msg_assistant_1"},
        "configuration": {"customerId": "CUST001"},
    },
}


def create_router(
    orchestrator: RelationshipManagerOrchestrator,
    settings: Settings,
    session_service: UniqueSessionService | None = None,
) -> APIRouter:
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

    @router.post(
        "/relationship-manager/webhook",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"type": "object"},
                        "example": _WEBHOOK_EXAMPLE,
                    }
                },
            }
        },
    )
    async def relationship_manager_webhook(request: Request) -> JSONResponse:
        """Answer a chat message delivered as a webhook event and write the reply back.

        **Request body (JSON).** Send the following fields:

        **Top level**
        - `event` (string, required): the event type. Only `module.chosen` and
          `user.message.created` are processed; anything else is acknowledged and
          ignored. This tells the endpoint the message is ready to be answered.
        - `userId` (string): the Unique user the request acts on behalf of. Used as
          the identity for SDK calls; falls back to `UNIQUE_AUTH_USER_ID` if empty.
        - `companyId` (string): the Unique company/tenant id for SDK calls; falls
          back to `UNIQUE_AUTH_COMPANY_ID` if empty.
        - `id` (string, optional): a unique identifier for this webhook delivery.
          We log it for traceability but do not require it — send any placeholder
          value when testing.
        - `version`, `createdAt` (optional): delivery envelope metadata (schema
          version and epoch timestamp). Logged but not required.

        **`payload` object**
        - `chatId` (string, required): the conversation this message belongs to. Used
          as the session id and as the target chat when writing the answer. Without it
          the request is acknowledged but not answered.
        - `text` (string): the user's question/message. Either this or
          `userMessage.text` must be non-empty — that text is what the agent answers.
        - `assistantId` (string): the Unique assistant id for SDK calls; falls back to
          `UNIQUE_ASSISTANT_ID` if empty.
        - `userMessage.text` (string): alternative place for the user's message; takes
          precedence over `payload.text` when present.
        - `assistantMessage.id` (string): id of the empty assistant message the Unique
          UI pre-creates. The generated answer is written into this placeholder. If
          omitted, a new assistant message is created instead.
        - `configuration.customerId` (string): which customer's data to query. When
          absent we use `UNIQUE_DEFAULT_CUSTOMER_ID`.

        The endpoint always returns HTTP 200 so the sender does not retry; the JSON
        body reports whether the message was actually handled.
        """
        raw_body = await request.body()
        sig_header = request.headers.get("X-Unique-Signature", "")
        timestamp = request.headers.get("X-Unique-Created-At", "")

        # 1. Verify authenticity (skipped only when no secret is configured).
        if session_service is not None:
            try:
                session_service.verify_webhook(raw_body, sig_header, timestamp)
            except Exception as exc:
                logger.warning("Webhook signature verification failed", extra={"error": str(exc)})
                return JSONResponse(status_code=400, content={"success": False, "reason": "invalid_signature"})

        # 2. Parse the envelope.
        try:
            event = WebhookEvent.model_validate_json(raw_body)
        except Exception as exc:
            logger.warning("Webhook body could not be parsed", extra={"error": str(exc)})
            return JSONResponse(status_code=400, content={"success": False, "reason": "invalid_body"})

        logger.info(
            "Webhook event received",
            extra={
                "event": event.event,
                "chat_id": event.payload.chatId,
                "assistant_id": event.payload.assistantId,
                "user_id": event.userId,
                "company_id": event.companyId,
            },
        )

        if event.event not in _ANSWERABLE_EVENTS:
            logger.info("Webhook event ignored (not answerable)", extra={"event": event.event})
            return JSONResponse(status_code=200, content={"success": True, "handled": False})

        payload = event.payload
        user_text = (payload.userMessage.text or payload.text).strip()
        chat_id = payload.chatId
        assistant_message_id = payload.assistantMessage.id

        if not user_text or not chat_id:
            logger.info(
                "Webhook missing user text or chatId — acknowledging without action",
                extra={"has_text": bool(user_text), "has_chat_id": bool(chat_id)},
            )
            return JSONResponse(status_code=200, content={"success": True, "handled": False})

        # 3. Resolve the customer_id (payload configuration wins; else default).
        customer_id = str(payload.configuration.get("customerId") or settings.unique_default_customer_id)

        # 4. Run the orchestrator. persist_turn=False: Unique already stored the user
        #    message; the assistant reply is written by updating the placeholder below.
        #    Identity comes from the event (client) and falls back to env vars downstream.
        rm_request = RelationshipManagerRequest(
            customer_id=customer_id,
            question=user_text,
            session_id=chat_id,
            persist_turn=False,
            auth_user_id=event.userId,
            auth_company_id=event.companyId,
            assistant_id=payload.assistantId,
        )
        t0 = time.perf_counter()
        try:
            response = await orchestrator.handle_request(rm_request)
        except Exception:
            logger.exception("Webhook orchestrator run failed", extra={"chat_id": chat_id})
            return JSONResponse(status_code=200, content={"success": False, "reason": "processing_error"})
        elapsed_ms = round((time.perf_counter() - t0) * 1000)

        # 5. Write the answer back into the assistant message placeholder.
        if session_service is not None:
            try:
                session_service.write_assistant_message(
                    chat_id=chat_id,
                    message_id=assistant_message_id,
                    text=response.final_answer,
                    user_id=event.userId,
                    company_id=event.companyId,
                    assistant_id=payload.assistantId,
                )
            except Exception:
                logger.exception("Webhook failed to write assistant message", extra={"chat_id": chat_id})
                return JSONResponse(status_code=200, content={"success": False, "reason": "write_error"})

        logger.info(
            "Webhook handled",
            extra={
                "chat_id": chat_id,
                "customer_id": customer_id,
                "elapsed_ms": elapsed_ms,
                "routing_decision": response.routing_decision,
                "final_answer_length": len(response.final_answer),
            },
        )
        return JSONResponse(status_code=200, content={"success": True, "handled": True})

    return router