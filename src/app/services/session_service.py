"""Session-based conversation history persistence via Unique AI Message API.

A session_id is NOT a Unique chatId. Unique owns the chat records: a chat must
exist in their database before any message can be written to or read from it.
Posting to an invented chatId returns HTTP 404 ``Chat not found``, which is why
nothing was ever persisted and history always came back empty.

This service therefore resolves each session_id to a real Unique chat:
  * session ids that already look like a Unique chat id (``chat_...``) are used as-is
  * anything else is mapped, once per process, to a chat created through
    ``unique_sdk.Space.create_chat`` and cached in memory.

Load path:  unique_sdk.Message.list (messages expose a flat ``text`` field).
Save path:  unique_sdk.Message.create × 2 (USER then ASSISTANT) per turn.

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


class UniqueSessionService:
    """Persist and retrieve per-session conversation history via Unique AI.

    session_id maps directly to chatId in the Unique platform.
    """

    def __init__(self, client: "UniqueAIClient") -> None:
        """Initialize with the shared Unique AI client (used for SDK configuration)."""
        self._client = client
        # session_id → real Unique chatId, resolved lazily and reused for the process lifetime.
        self._chat_ids: dict[str, str] = {}

    @property
    def _is_configured(self) -> bool:
        """Return True when the SDK and the app API key are present.

        Identity (user/company/assistant) is resolved per call via _resolve_identity,
        so it may come from the request (webhook event) rather than env vars.
        """
        return _SDK_AVAILABLE and bool(self._client.settings.unique_app_key)

    def _resolve_identity(
        self,
        user_id: str = "",
        company_id: str = "",
        assistant_id: str = "",
    ) -> tuple[str, str, str]:
        """Resolve identity with precedence: caller-provided value first, else env var."""
        s = self._client.settings
        return (
            user_id or s.unique_auth_user_id,
            company_id or s.unique_auth_company_id,
            assistant_id or s.unique_assistant_id,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def load_history(
        self,
        session_id: str,
        *,
        user_id: str = "",
        company_id: str = "",
        assistant_id: str = "",
    ) -> list[dict[str, str]]:
        """Fetch prior conversation turns from Unique AI for this session.

        Identity args override the env vars when non-empty (webhook passes the event
        userId/companyId/assistantId). Returns a list of OpenAI-format dicts:
        {"role": "user"|"assistant", "content": "..."}.
        Returns [] when the SDK is not configured, the session has no history, or any
        call to Unique fails.
        """
        uid, cid, aid = self._resolve_identity(user_id, company_id, assistant_id)
        if not self._is_configured or not session_id or not uid or not cid:
            logger.debug(
                "UniqueSessionService.load_history: not configured — returning empty",
                extra={"session_id": session_id, "sdk_available": _SDK_AVAILABLE, "has_user": bool(uid)},
            )
            return []

        try:
            self._setup_sdk()
            chat_id = self._resolve_chat_id(
                session_id, create_if_missing=False, user_id=uid, company_id=cid, assistant_id=aid
            )
            if not chat_id:
                logger.info(
                    "UniqueSessionService.load_history: no Unique chat bound to session yet",
                    extra={"session_id": session_id},
                )
                return []
            return self._load_via_message_list(session_id, chat_id, user_id=uid, company_id=cid)
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
        *,
        user_id: str = "",
        company_id: str = "",
        assistant_id: str = "",
    ) -> None:
        """Persist a user→assistant turn to Unique AI.

        Identity args override the env vars when non-empty. Creates a USER message
        followed by an ASSISTANT message in the Unique chat identified by session_id.
        Skips silently when the SDK/identity is not configured, the assistant id is
        missing, or any Unique API call fails.
        """
        uid, cid, aid = self._resolve_identity(user_id, company_id, assistant_id)
        if not self._is_configured or not session_id or not uid or not cid or not aid:
            logger.debug(
                "UniqueSessionService.save_turn: not configured — skipping persistence",
                extra={"session_id": session_id, "has_assistant_id": bool(aid)},
            )
            return

        try:
            self._setup_sdk()
            chat_id = self._resolve_chat_id(
                session_id, create_if_missing=True, user_id=uid, company_id=cid, assistant_id=aid
            )
            for role, text in (("USER", user_message), ("ASSISTANT", assistant_message)):
                self._create_message(
                    chat_id=chat_id, role=role, text=text, user_id=uid, company_id=cid, assistant_id=aid
                )
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

    # ── Webhook support (Unique AI space integration) ─────────────────────────

    def verify_webhook(self, payload: bytes, sig_header: str, timestamp: str) -> None:
        """Verify a Unique webhook HMAC signature.

        Raises when verification fails. When no endpoint secret is configured the
        check is skipped (POC only) and a warning is logged.
        """
        secret = self._client.settings.unique_webhook_endpoint_secret
        if not secret:
            logger.warning(
                "UniqueSessionService.verify_webhook: no endpoint secret configured — "
                "skipping signature verification (set UNIQUE_WEBHOOK_ENDPOINT_SECRET in production)"
            )
            return
        if not _SDK_AVAILABLE:
            raise RuntimeError("unique_sdk not installed — cannot verify webhook signature")
        self._setup_sdk()
        _sdk.Webhook.construct_event(payload, sig_header, timestamp, secret)  # type: ignore[union-attr]

    def write_assistant_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        user_id: str = "",
        company_id: str = "",
        assistant_id: str = "",
    ) -> None:
        """Fill the pre-created assistant message placeholder with the final answer.

        Identity args override the env vars when non-empty (webhook passes the event
        userId/companyId). Uses Message.modify when Unique supplied a placeholder id
        (the standard external-module flow); falls back to Message.create otherwise.
        """
        uid, cid, aid = self._resolve_identity(user_id, company_id, assistant_id)
        self._setup_sdk()
        if message_id:
            _sdk.Message.modify(  # type: ignore[union-attr]
                user_id=uid,
                company_id=cid,
                id=message_id,
                chatId=chat_id,
                text=text,
            )
            logger.info(
                "UniqueSessionService.write_assistant_message: assistant message updated",
                extra={"chat_id": chat_id, "message_id": message_id, "text_len": len(text)},
            )
            return
        self._create_message(
            chat_id=chat_id, role="ASSISTANT", text=text, user_id=uid, company_id=cid, assistant_id=aid
        )
        logger.info(
            "UniqueSessionService.write_assistant_message: assistant message created (no placeholder id)",
            extra={"chat_id": chat_id, "text_len": len(text)},
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _resolve_chat_id(
        self,
        session_id: str,
        *,
        create_if_missing: bool,
        user_id: str = "",
        company_id: str = "",
        assistant_id: str = "",
    ) -> str:
        """Map a session_id to a real Unique chatId, creating the chat on demand.

        Returns an empty string when no chat exists yet and creation was not requested.
        """
        if session_id.startswith("chat_"):
            return session_id
        cached = self._chat_ids.get(session_id)
        if cached or not create_if_missing:
            return cached or ""

        uid, cid, aid = self._resolve_identity(user_id, company_id, assistant_id)
        chat: Any = _sdk.Space.create_chat(  # type: ignore[union-attr]
            user_id=uid,
            company_id=cid,
            title=session_id,
            assistantId=aid,
        )
        chat_id = str(chat["id"] if isinstance(chat, dict) else getattr(chat, "id", ""))
        if not chat_id:
            raise RuntimeError("Unique Space.create_chat returned no chat id")
        self._chat_ids[session_id] = chat_id
        logger.info(
            "UniqueSessionService: Unique chat created for session — set "
            "UNIQUE_DEFAULT_SESSION_ID to this chat_id to keep history across restarts",
            extra={"session_id": session_id, "chat_id": chat_id},
        )
        return chat_id

    def _load_via_message_list(
        self,
        session_id: str,
        chat_id: str,
        *,
        user_id: str = "",
        company_id: str = "",
    ) -> list[dict[str, str]]:
        """Load history via unique_sdk.Message.list (messages expose a flat ``text`` field)."""
        uid, cid, _aid = self._resolve_identity(user_id, company_id)
        resp = _sdk.Message.list(  # type: ignore[union-attr]
            user_id=uid,
            company_id=cid,
            chatId=chat_id,
        )
        result: list[dict[str, str]] = []
        for msg in getattr(resp, "data", None) or []:
            role_raw = str(self._field(msg, "role") or "").upper()
            if role_raw == "USER":
                role = "user"
            elif role_raw in ("ASSISTANT", "AGENT"):
                role = "assistant"
            else:
                continue
            text = str(self._field(msg, "text") or "").strip()
            if text:
                result.append({"role": role, "content": text})
        logger.info(
            "UniqueSessionService: history loaded via Message.list",
            extra={
                "session_id": session_id,
                "chat_id": chat_id,
                "message_count": len(result),
            },
        )
        return result

    @staticmethod
    def _field(msg: Any, key: str) -> Any:
        """Read a field from an SDK object that behaves as both dict and attribute holder."""
        if isinstance(msg, dict):
            return msg.get(key)
        return getattr(msg, key, None)

    def _create_message(
        self,
        chat_id: str,
        role: str,
        text: str,
        *,
        user_id: str = "",
        company_id: str = "",
        assistant_id: str = "",
    ) -> None:
        """Create one message in the Unique chat via unique_sdk.Message.create."""
        uid, cid, aid = self._resolve_identity(user_id, company_id, assistant_id)
        _sdk.Message.create(  # type: ignore[union-attr]
            user_id=uid,
            company_id=cid,
            chatId=chat_id,
            assistantId=aid,
            role=role,
            text=text,
        )

    def _setup_sdk(self) -> None:
        """Configure the unique_sdk module with current credentials."""
        self._client._configure_sdk(_sdk)
