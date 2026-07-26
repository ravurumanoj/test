
"""Thin adapter around the Unique AI SDK."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from app.errors import UniqueIntegrationError
from app.settings import Settings

logger = logging.getLogger(__name__)

try:
    import unique_sdk
except ImportError:
    unique_sdk = None


class UniqueAIClient:
    """Wrap the only allowed agentic SDK boundary for the application."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the client with runtime settings."""
        self.settings = settings

    def run_completion(self, agent_name: str, messages: list[dict[str, Any]]) -> str:
        """Run a completion through Unique SDK and return assistant text.

        The messages list already encodes the prompt, context, and question so
        no additional parameters are needed here.  Callers (UniqueToolkit.execute)
        build the message list with _build_messages before invoking this method.
        """
        logger.info(
            "UniqueAIClient.run_completion called",
            extra={
                "agent_name": agent_name,
                "message_count": len(messages),
                "model": self.settings.unique_model_name,
            },
        )
        response = self.create_completion(messages=messages)
        content = self._extract_completion_text(response)
        if not content:
            raise UniqueIntegrationError(
                "Unique SDK returned an empty or unsupported completion response.",
                {"agent_name": agent_name},
            )
        logger.info(
            "UniqueAIClient.run_completion completed",
            extra={
                "agent_name": agent_name,
                "content_length": len(content),
                "content_preview": content[:300],
            },
        )
        return content

    def create_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Run a raw completion request and return a normalized OpenAI-style payload.
        
        The unique-sdk.ChatCompletion.create signature is:
          create(company_id, user_id, **params)
        
        Where params includes: model, messages, options (which contains tools, etc.)
        """
        if not self.settings.unique_app_id or not self.settings.unique_app_key:
            raise UniqueIntegrationError(
                "UNIQUE_APP_ID and UNIQUE_APP_KEY must be configured.",
                {"operation": "create_completion"},
            )
        if unique_sdk is None:
            logger.error("unique_sdk package is not installed or could not be imported")
            raise UniqueIntegrationError(
                "The unique-sdk package is not installed or not available in the environment.",
                {"package": "unique-sdk"},
            )

        self._configure_sdk(unique_sdk)
        chat_completion = getattr(unique_sdk, "ChatCompletion", None)
        create = getattr(chat_completion, "create", None) if chat_completion else None
        if create is None or not callable(create):
            raise UniqueIntegrationError(
                "The unique-sdk package does not expose unique_sdk.ChatCompletion.create as documented.",
                {"operation": "create_completion"},
            )

        # Build options dict with tools and temperature
        options: dict[str, Any] = {}
        if tools:
            options["tools"] = tools
        if tool_choice:
            options["tool_choice"] = tool_choice
        if temperature is not None:
            options["temperature"] = temperature

        # Build the params dict for the **params unpacking in create()
        params: dict[str, Any] = {
            "company_id": self.settings.unique_auth_company_id,
            "user_id": self.settings.unique_auth_user_id,
            "model": self.settings.unique_model_name,
            "messages": messages,
        }
        if options:
            params["options"] = options

        logger.debug(
            "create_completion calling unique_sdk.ChatCompletion.create",
            extra={
                "model": self.settings.unique_model_name,
                "messages_count": len(messages),
                "has_tools": bool(tools),
                "tool_names": [t.get("function", {}).get("name") for t in (tools or [])],
                "has_options": bool(options),
                "temperature": temperature,
            },
        )

        try:
            result = create(**params)
        except Exception as exc:
            logger.exception("Unique SDK ChatCompletion.create call failed", extra={"params_keys": list(params.keys())})
            raise UniqueIntegrationError(
                "Unique SDK ChatCompletion.create call failed.",
                {"model_name": self.settings.unique_model_name},
            ) from exc
        normalized = self._normalize_completion_response(result)
        content_peek = self._extract_completion_text(normalized)
        tool_call_count = len(self.extract_tool_calls(normalized))
        logger.debug(
            "create_completion: SDK response received",
            extra={
                "model": self.settings.unique_model_name,
                "response_has_content": bool(content_peek),
                "content_preview": content_peek[:200],
                "tool_call_count": tool_call_count,
            },
        )
        return normalized

    def _configure_sdk(self, unique_sdk_module: Any) -> None:
        """Configure the imported unique_sdk module with documented credentials and proxy settings."""
        import os
        
        unique_sdk_module.api_key = self.settings.unique_app_key
        unique_sdk_module.app_id = self.settings.unique_app_id

        optional_settings = {
            "api_base": self.settings.unique_api_base_url,
            "api_version": self.settings.unique_api_version,
            "company_id": self.settings.unique_auth_company_id,
            "user_id": self.settings.unique_auth_user_id,
        }
        for attribute_name, value in optional_settings.items():
            if value and hasattr(unique_sdk_module, attribute_name):
                setattr(unique_sdk_module, attribute_name, value)
        
        # Configure HTTP proxies if provided in environment
        http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
        https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if http_proxy or https_proxy:
            logger.info(
                "Configuring HTTP proxies for unique_sdk",
                extra={"has_http_proxy": bool(http_proxy), "has_https_proxy": bool(https_proxy)},
            )
            # The requests library uses environment variables automatically
            if http_proxy:
                os.environ["HTTP_PROXY"] = http_proxy
            if https_proxy:
                os.environ["HTTPS_PROXY"] = https_proxy

    def _filter_supported_arguments(self, callable_obj: Any, candidate_arguments: dict[str, Any]) -> dict[str, Any]:
        """Pass only parameters supported by the installed Unique SDK version."""
        try:
            parameters = inspect.signature(callable_obj).parameters
        except (TypeError, ValueError):
            return {key: value for key, value in candidate_arguments.items() if value not in (None, "")}

        return {
            key: value
            for key, value in candidate_arguments.items()
            if key in parameters and value not in (None, "")
        }

    def _extract_completion_text(self, result: Any) -> str:
        """Normalize completion responses returned by different SDK versions."""
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            return self._extract_from_dict(result)

        if hasattr(result, "to_dict") and callable(result.to_dict):
            try:
                return self._extract_from_dict(result.to_dict())
            except Exception:
                logger.debug("Failed to normalize Unique SDK response via to_dict", exc_info=True)

        choices = getattr(result, "choices", None)
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else getattr(choices[0], "message", None)
            if isinstance(message, dict):
                # Use `or ""` to treat None content (tool-call responses) as empty string.
                return str(message.get("content") or "").strip()
            if message is not None:
                # Use `or ""` to treat None content (tool-call responses) as empty string.
                return str(getattr(message, "content", None) or "").strip()
        return ""

    def extract_tool_calls(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract OpenAI-compatible tool calls from a completion payload."""
        choices = result.get("choices", [])
        if not isinstance(choices, list) or not choices:
            return []
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            return []
        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            return []
        normalized: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function_data = tool_call.get("function", {})
            if not isinstance(function_data, dict):
                continue
            name = function_data.get("name")
            arguments = function_data.get("arguments", "{}")
            if not isinstance(name, str) or not name:
                continue
            normalized.append(
                {
                    "id": str(tool_call.get("id", "")) or f"call_{name}",
                    "name": name,
                    "arguments": arguments,
                }
            )
        return normalized

    def _extract_from_dict(self, result: dict[str, Any]) -> str:
        """Extract assistant text from an OpenAI-style completion payload."""
        choices = result.get("choices", [])
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            return ""
        # Use `or ""` to treat None content (tool-call responses) as empty string.
        return str(message.get("content") or "").strip()

    def _normalize_completion_response(self, result: Any) -> dict[str, Any]:
        """Normalize various SDK response shapes into a dict payload."""
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": result,
                        }
                    }
                ]
            }
        if hasattr(result, "to_dict") and callable(result.to_dict):
            converted = result.to_dict()
            if isinstance(converted, dict):
                return converted

        choices = getattr(result, "choices", None)
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message")
            else:
                message = getattr(first_choice, "message", None)
            message_content = ""
            message_tool_calls: list[dict[str, Any]] | None = None
            if isinstance(message, dict):
                # Use `or ""` to treat None content (tool-call responses) as empty string.
                message_content = str(message.get("content") or "")
                message_tool_calls = message.get("tool_calls") or None
            elif message is not None:
                message_content = str(getattr(message, "content", None) or "")
                raw_tool_calls = getattr(message, "tool_calls", None)
                if raw_tool_calls:
                    message_tool_calls = []
                    for tc in raw_tool_calls:
                        if isinstance(tc, dict):
                            message_tool_calls.append(tc)
                        else:
                            fn = getattr(tc, "function", None)
                            message_tool_calls.append({
                                "id": str(getattr(tc, "id", "") or ""),
                                "type": str(getattr(tc, "type", "function") or "function"),
                                "function": {
                                    "name": str(getattr(fn, "name", "") or "") if fn else "",
                                    "arguments": str(getattr(fn, "arguments", "{}") or "{}") if fn else "{}",
                                },
                            })
            normalized_message: dict[str, Any] = {"role": "assistant", "content": message_content}
            if message_tool_calls:
                normalized_message["tool_calls"] = message_tool_calls
            return {"choices": [{"message": normalized_message}]}
        return {"choices": [{"message": {"role": "assistant", "content": ""}}]}
