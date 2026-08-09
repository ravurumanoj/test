"""Session-based conversation history persistence via Unique AI Message API.

Each session_id is used as chatId when calling the Unique SDK.
For the POC a single hardcoded session is configured via UNIQUE_DEFAULT_SESSION_ID.

Load path (in priority order):
  1. unique_sdk.utils.chat_history.load_history  — returns OpenAI-format messages directly
  2. unique_sdk.Message.list                      — raw fallback with manual role mapping

Save path:
  unique_sdk.Message.create × 2 (USER then ASSISTANT) per conversation turn.

All public methods degrade gracefully to no-ops when the SDK is not installed or
the required credentials (api_key, user_id, company_id) are not configured.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.unique_client import UniqueAIClient

logger = logging.getLogger(__name__)

try:
    import unique_sdk as _sdk
    _SDK_AVAILABLE = True
except ImportError:
    _sdk = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False

try:
    from unique_sdk.utils.chat_history import load_history as _sdk_load_history
    _UTILS_AVAILABLE = True
except ImportError:
    _sdk_load_history = None  # type: ignore[assignment]
    _UTILS_AVAILABLE = False


class UniqueSessionService:
    """Persist and retrieve per-session conversation history via Unique AI.

    session_id maps directly to chatId in the Unique platform.
    """

    def __init__(self, client: "UniqueAIClient") -> None:
        """Initialize with the shared Unique AI client (used for SDK configuration)."""
        self._client = client

    @property
    def _is_configured(self) -> bool:
        """Return True when the SDK and all required credentials are present."""
        s = self._client.settings
        return (
            _SDK_AVAILABLE
            and bool(s.unique_app_key)
            and bool(s.unique_auth_user_id)
            and bool(s.unique_auth_company_id)
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def load_history(self, session_id: str) -> list[dict[str, str]]:
        """Fetch prior conversation turns from Unique AI for this session.

        Returns a list of OpenAI-format dicts: {"role": "user"|"assistant", "content": "..."}.
        Returns [] when the SDK is not configured, the session has no history, or any
        call to Unique fails.
        """
        if not self._is_configured or not session_id:
            logger.debug(
                "UniqueSessionService.load_history: not configured — returning empty",
                extra={"session_id": session_id, "sdk_available": _SDK_AVAILABLE},
            )
            return []

        try:
            self._setup_sdk()
            if _UTILS_AVAILABLE and _sdk_load_history is not None:
                return self._load_via_utils(session_id)
            return self._load_via_message_list(session_id)
        except Exception as exc:
            logger.warning(
                "UniqueSessionService.load_history: SDK call failed — returning empty history",
                extra={"session_id": session_id, "error": str(exc)},
            )
            return []

    async def save_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        """Persist a user→assistant turn to Unique AI.

        Creates a USER message followed by an ASSISTANT message in the Unique chat
        identified by session_id.  Skips silently when the SDK is not configured,
        UNIQUE_ASSISTANT_ID is missing, or any Unique API call fails.
        """
        s = self._client.settings
        if not self._is_configured or not session_id or not s.unique_assistant_id:
            logger.debug(
                "UniqueSessionService.save_turn: not configured — skipping persistence",
                extra={
                    "session_id": session_id,
                    "has_assistant_id": bool(s.unique_assistant_id),
                },
            )
            return

        try:
            self._setup_sdk()
            for role, text in (("USER", user_message), ("ASSISTANT", assistant_message)):
                self._create_message(session_id=session_id, role=role, text=text)
            logger.info(
                "UniqueSessionService.save_turn: turn persisted to Unique AI",
                extra={
                    "session_id": session_id,
                    "user_msg_len": len(user_message),
                    "assistant_msg_len": len(assistant_message),
                },
            )
        except Exception as exc:
            logger.warning(
                "UniqueSessionService.save_turn: SDK call failed — turn not persisted",
                extra={"session_id": session_id, "error": str(exc)},
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_via_utils(self, session_id: str) -> list[dict[str, str]]:
        """Load history using unique_sdk.utils.chat_history.load_history (preferred path)."""
        s = self._client.settings
        raw: list[dict[str, Any]] = _sdk_load_history(  # type: ignore[misc]
            user_id=s.unique_auth_user_id,
            company_id=s.unique_auth_company_id,
            chatId=session_id,
            maxTokens=s.unique_max_history_tokens,
            assistantId=s.unique_assistant_id,
        ) or []
        result = [
            {"role": m["role"], "content": m.get("content", "")}
            for m in raw
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        logger.info(
            "UniqueSessionService: history loaded via utils.load_history",
            extra={"session_id": session_id, "message_count": len(result)},
        )
        return result

    def _load_via_message_list(self, session_id: str) -> list[dict[str, str]]:
        """Fallback: load history via raw unique_sdk.Message.list."""
        s = self._client.settings
        resp = _sdk.Message.list(  # type: ignore[union-attr]
            user_id=s.unique_auth_user_id,
            company_id=s.unique_auth_company_id,
            chatId=session_id,
        )
        result: list[dict[str, str]] = []
        for msg in getattr(resp, "data", None) or []:
            role_raw = str(getattr(msg, "role", "")).upper()
            if role_raw == "USER":
                role = "user"
            elif role_raw in ("ASSISTANT", "AGENT"):
                role = "assistant"
            else:
                continue
            content_obj = getattr(msg, "content", None)
            text = (
                content_obj.get("text", "") if isinstance(content_obj, dict) else str(content_obj or "")
            )
            if text:
                result.append({"role": role, "content": text})
        logger.info(
            "UniqueSessionService: history loaded via Message.list",
            extra={"session_id": session_id, "message_count": len(result)},
        )
        return result

    def _create_message(self, session_id: str, role: str, text: str) -> None:
        """Create one message in the Unique chat via unique_sdk.Message.create."""
        s = self._client.settings
        _sdk.Message.create(  # type: ignore[union-attr]
            user_id=s.unique_auth_user_id,
            company_id=s.unique_auth_company_id,
            chatId=session_id,
            assistantId=s.unique_assistant_id,
            role=role,
            content={"type": "TEXT", "text": text},
        )

    def _setup_sdk(self) -> None:
        """Configure the unique_sdk module with current credentials."""
        self._client._configure_sdk(_sdk)
