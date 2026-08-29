
"""Request and response schemas for the POC API.

Includes Unique Toolkit-aligned types:
  ContentChunk       — referenceable document chunk (mirrors unique_toolkit ContentChunk)
  ToolCallResponse   — structured tool output (mirrors unique_toolkit ToolCallResponse)
  ToolDescription    — LLM-readable tool schema (mirrors LanguageModelToolDescription)
  EvaluationMetricResult — quality-check result (mirrors unique_toolkit EvaluationMetricResult)

Ordering: Unique Toolkit types are defined first so forward-reference resolution
is unambiguous in all Python and Pydantic v2 contexts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ─── Unique Toolkit-aligned types (defined first — used by response schemas) ─


class ContentChunk(BaseModel):
    """Represent a referenceable document chunk from a Tool response.

    Mirrors unique_toolkit ContentChunk — used by ReferenceManager to
    build UI source citations (e.g. [1]↗).
    """

    id: str
    chunk_id: str = "0"
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def source_id(self) -> str:
        """Return the ReferenceManager-compatible identifier (id-chunk_id)."""
        return f"{self.id}-{self.chunk_id}"


class ToolCallResponse(BaseModel):
    """Structured output returned by a Tool after execution.

    Mirrors unique_toolkit.agentic.tool_call_response.ToolCallResponse.

    Fields
    ------
    content        — plain text output (used when no citations are needed)
    content_chunks — referenceable chunks processed by ReferenceManager
    debug_info     — captured by DebugInfoManager for developer inspection
    error_message  — non-empty string signals a failed execution to the LLM
    """

    id: str
    name: str
    content: str = ""
    content_chunks: list[ContentChunk] | None = None
    debug_info: dict[str, Any] | None = None
    error_message: str = ""

    @property
    def successful(self) -> bool:
        """Return True when the tool executed without errors."""
        return not bool(self.error_message)


class ToolDescription(BaseModel):
    """LLM-readable tool definition used in the orchestrator planning step.

    Mirrors unique_toolkit LanguageModelToolDescription.
    """

    name: str
    description: str
    parameters: dict[str, Any]


class EvaluationMetricResult(BaseModel):
    """Result of a quality evaluation run.

    Mirrors unique_toolkit EvaluationMetricResult — surfaced to the chat
    UI (PENDING → result) in a full Unique platform deployment.
    """

    name: str
    is_positive: bool
    value: str
    reason: str


# ─── API request / response schemas ──────────────────────────────────────────


class ConversationTurn(BaseModel):
    """One prior turn in a multi-turn conversation.

    Used by ``RelationshipManagerRequest.chat_history`` to seed the
    HistoryManager with previous user/assistant exchanges so the orchestrator
    maintains context across API calls.
    """

    role: Literal["user", "assistant"]
    content: str


class RelationshipManagerRequest(BaseModel):
    """Represent an incoming relationship manager query."""

    customer_id: str = Field(min_length=1, description="Customer identifier used by the sub-agents.")
    question: str = Field(min_length=3, description="Natural language question from the relationship manager.")
    session_id: str = Field(
        default="",
        description=(
            "Session identifier used as chatId in Unique AI for history persistence. "
            "When empty, the server falls back to UNIQUE_DEFAULT_SESSION_ID. "
            "Set this to the Unique chatId that represents this user's conversation thread."
        ),
    )
    chat_history: list[ConversationTurn] = Field(
        default_factory=list,
        description=(
            "Fallback conversation history (oldest first). "
            "Used only when Unique AI session history is unavailable or unconfigured. "
            "Prefer session_id-based persistence for multi-turn conversations."
        ),
    )
    persist_turn: bool = Field(
        default=True,
        description=(
            "When True the orchestrator writes the user+assistant turn back to Unique AI. "
            "Set False for webhook-driven requests, where Unique already stores the user "
            "message and the assistant reply is written by updating the pre-created "
            "assistant message placeholder."
        ),
    )
    auth_user_id: str = Field(
        default="",
        description=(
            "Unique user id to act on behalf of for SDK calls. When non-empty this "
            "overrides UNIQUE_AUTH_USER_ID (used by the webhook to pass event.userId). "
            "Empty falls back to the env var."
        ),
    )
    auth_company_id: str = Field(
        default="",
        description=(
            "Unique company id for SDK calls. Overrides UNIQUE_AUTH_COMPANY_ID when "
            "non-empty (webhook passes event.companyId). Empty falls back to the env var."
        ),
    )
    assistant_id: str = Field(
        default="",
        description=(
            "Unique assistant id for SDK calls. Overrides UNIQUE_ASSISTANT_ID when "
            "non-empty (webhook passes payload.assistantId). Empty falls back to the env var."
        ),
    )

    @field_validator("customer_id", "question")
    @classmethod
    def strip_values(cls, value: str) -> str:
        """Normalize string fields before business processing."""
        return value.strip()


class AgentAnswer(BaseModel):
    """Represent an answer generated by one sub-agent."""

    agent_name: Literal["portfolio", "crm"]
    summary: str
    retrieved_context: dict
    tool_name: str = Field(
        default="",
        description="The specific data tool that produced this answer (e.g. portfolio_performance).",
    )


class RelationshipManagerResponse(BaseModel):
    """Represent the orchestrated result returned to the client."""

    customer_id: str
    question: str
    routing_decision: list[str]
    final_answer: str
    agent_answers: list[AgentAnswer]
    debug_info: dict[str, Any] = Field(default_factory=dict)
    evaluation_results: list[EvaluationMetricResult] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Represent the service health payload."""

    status: str
    app_name: str
    environment: str


# ─── Unique AI webhook schemas (space integration) ───────────────────────────


class WebhookUserMessage(BaseModel):
    """User message carried by a Unique webhook event."""

    id: str = ""
    text: str = ""
    createdAt: str | None = None
    originalText: str | None = None
    language: str | None = None


class WebhookAssistantMessage(BaseModel):
    """Placeholder assistant message Unique pre-creates for us to fill in."""

    id: str = ""
    createdAt: str | None = None


class WebhookPayload(BaseModel):
    """Payload of a Unique ``external-module.chosen`` / ``user-message.created`` event.

    Extra keys sent by Unique (name, description, toolChoices, userMetadata, ...) are
    accepted and ignored so schema drift on their side never breaks the endpoint.
    """

    model_config = {"extra": "allow"}

    chatId: str = ""
    assistantId: str = ""
    text: str = ""  # present on user-message.created
    userMessage: WebhookUserMessage = Field(default_factory=WebhookUserMessage)
    assistantMessage: WebhookAssistantMessage = Field(default_factory=WebhookAssistantMessage)
    configuration: dict[str, Any] = Field(default_factory=dict)


class WebhookEvent(BaseModel):
    """Envelope of a Unique webhook delivery."""

    model_config = {"extra": "allow"}

    id: str = ""
    version: str = ""
    event: str = ""
    createdAt: int | str | None = None
    userId: str = ""
    companyId: str = ""
    payload: WebhookPayload = Field(default_factory=WebhookPayload)
