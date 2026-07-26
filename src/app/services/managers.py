
"""All five Unique Toolkit-aligned agentic framework managers.

Mirrors the patterns described in:
  docs/unique_toolkit_agentic_framework_managers.md
  docs/unique_toolkit_agentic_framework_core.md

Managers
--------
DebugInfoManager      — key/value debug trace store (exposed to Debug-role users)
ReferenceManager      — content-chunk tracking and sequential citation numbering
HistoryManager        — conversation history with Loop Token Reducer for context budget
EvaluationManager     — pluggable quality/compliance evaluations run concurrently
PostprocessorManager  — pluggable response transformations applied after LLM output

Concrete implementations
------------------------
FinancialSafetyEvaluation       — flags unsafe financial-guarantee language (compliance check)
FinancialDisclaimerPostprocessor — appends regulatory disclaimer; strips it from history
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
from typing import Any

from app.schemas import ContentChunk, EvaluationMetricResult, ToolCallResponse

logger = logging.getLogger(__name__)


# ─── DebugInfoManager ────────────────────────────────────────────────────────


class DebugInfoManager:
    """Key-value debug store that captures tool traces and runtime diagnostics.

    Mirrors DebugInfoManager in unique_toolkit:
      - Values stored via add() are surfaced to "Debug"-role users in the Unique UI
      - extract_from_tool_responses() harvests ToolCallResponse.debug_info automatically
    """

    def __init__(self) -> None:
        """Initialize an empty debug store."""
        self._store: dict[str, Any] = {}
        logger.debug("DebugInfoManager initialized")

    def add(self, key: str, value: Any) -> None:
        """Store a debug key-value pair.

        Mirrors DebugInfoManager.add(key, value) from docs.
        """
        self._store[key] = value
        logger.debug("DebugInfoManager.add called", extra={"key": key})

    def get(self) -> dict[str, Any]:
        """Return a snapshot of all stored debug entries.

        Mirrors DebugInfoManager.get() from docs.
        """
        return dict(self._store)

    def extract_from_tool_responses(self, tool_responses: list[ToolCallResponse]) -> None:
        """Harvest debug_info from every ToolCallResponse and merge into the store.

        Mirrors the orchestrator calling DebugInfoManager after tool execution
        to capture per-tool traces and diagnostics.
        """
        for resp in tool_responses:
            if resp.debug_info:
                key = f"{resp.name}__{resp.id}"
                self._store[key] = resp.debug_info
                logger.debug(
                    "DebugInfoManager extracted tool debug_info",
                    extra={"tool_name": resp.name, "tool_call_id": resp.id},
                )
        logger.info(
            "DebugInfoManager updated from tool responses",
            extra={"total_debug_entries": len(self._store), "tools_processed": len(tool_responses)},
        )


# ─── ReferenceManager ────────────────────────────────────────────────────────


class ReferenceManager:
    """Manages content chunks from tool responses for source citations.

    Mirrors ReferenceManager in unique_toolkit:
      - extract_referenceable_chunks() harvests ContentChunks from ToolCallResponses
      - Sequential reference numbers enable UI citations (e.g. [1], [2])
      - Supports back-references to chunks from previous iterations
    """

    def __init__(self) -> None:
        """Initialize an empty reference store."""
        self._chunks: list[ContentChunk] = []
        self._tool_chunks: dict[str, list[ContentChunk]] = {}  # tool_call_id → chunks
        self._reference_counter: int = 0
        logger.debug("ReferenceManager initialized")

    def extract_referenceable_chunks(self, tool_responses: list[ToolCallResponse]) -> None:
        """Extract ContentChunks from all tool responses and register them.

        Mirrors ReferenceManager.extract_referenceable_chunks() in unique_toolkit.
        Multiple tools per iteration are numbered sequentially (incremental offset).
        """
        for resp in tool_responses:
            if not resp.content_chunks:
                continue
            self._chunks.extend(resp.content_chunks)
            self._tool_chunks[resp.id] = list(resp.content_chunks)
            logger.debug(
                "ReferenceManager extracted chunks from tool",
                extra={"tool_name": resp.name, "tool_call_id": resp.id, "chunk_count": len(resp.content_chunks)},
            )
        logger.info(
            "ReferenceManager updated",
            extra={
                "total_chunks": len(self._chunks),
                "tool_responses_processed": len(tool_responses),
            },
        )

    def get_chunks(self) -> list[ContentChunk]:
        """Return all registered content chunks across all tools."""
        return list(self._chunks)

    def get_chunks_for_tool(self, tool_call_id: str) -> list[ContentChunk]:
        """Return content chunks for a specific tool call.

        Mirrors ReferenceManager.get_chunks_of_tool() from docs.
        """
        return list(self._tool_chunks.get(tool_call_id, []))

    def get_all_tool_chunks(self) -> dict[str, list[ContentChunk]]:
        """Return all chunks grouped by tool_call_id."""
        return dict(self._tool_chunks)

    def get_next_reference_number(self) -> int:
        """Increment and return the next sequential reference number.

        Mirrors the HistoryManager source-offset management in unique_toolkit —
        enables consistent citation numbers across multiple tool calls.
        """
        self._reference_counter += 1
        logger.debug("ReferenceManager allocated reference number", extra={"ref_num": self._reference_counter})
        return self._reference_counter

    def build_reference_map(self) -> dict[int, ContentChunk]:
        """Build a {reference_number: chunk} mapping for citation rendering.

        Returns a snapshot; calling this multiple times does not re-number chunks.
        """
        result: dict[int, ContentChunk] = {}
        for i, chunk in enumerate(self._chunks, start=1):
            result[i] = chunk
        logger.debug("ReferenceManager built reference map", extra={"entry_count": len(result)})
        return result

    def replace_chunks_of_tool(self, tool_call_id: str, chunks: list[ContentChunk]) -> None:
        """Replace chunks for a specific tool call.

        Mirrors ReferenceManager.replace_chunks_of_tool() from docs.
        """
        if tool_call_id in self._tool_chunks:
            old_count = len(self._tool_chunks[tool_call_id])
            self._tool_chunks[tool_call_id] = list(chunks)
            logger.debug(
                "ReferenceManager replaced tool chunks",
                extra={"tool_call_id": tool_call_id, "old_count": old_count, "new_count": len(chunks)},
            )


# ─── HistoryManager ──────────────────────────────────────────────────────────


class HistoryManager:
    """Manages conversation history with token-window awareness.

    Mirrors HistoryManager in unique_toolkit:
      - Tracks user messages, assistant responses, tool call queries, and results
      - Loop Token Reducer trims oldest non-system messages when over budget
      - extract_message_tools() persists tool call records across turns
      - has_no_loop_messages() used by orchestrator startup check
    """

    _APPROX_CHARS_PER_TOKEN: int = 4  # Conservative estimate for English text

    def __init__(self, max_token_budget: int = 6000) -> None:
        """Initialize with an optional token-budget cap."""
        self._loop_history: list[dict[str, Any]] = []
        self._max_token_budget = max_token_budget
        logger.debug("HistoryManager initialized", extra={"max_token_budget": max_token_budget})

    # ── Startup check ─────────────────────────────────────────────────────────

    def has_no_loop_messages(self) -> bool:
        """Return True when the loop has no messages yet.

        Mirrors HistoryManager.has_no_loop_messages() — used by the orchestrator
        to detect a fresh session and display a startup indicator.
        """
        return not bool(self._loop_history)

    # ── Message appending ─────────────────────────────────────────────────────

    def add_system_message(self, content: str) -> None:
        """Add a system prompt to the conversation history."""
        self._loop_history.append({"role": "system", "content": content})
        logger.debug("HistoryManager: system message added")

    def add_user_message(self, content: str) -> None:
        """Add a user message to the conversation history."""
        self._loop_history.append({"role": "user", "content": content})
        logger.debug(
            "HistoryManager: user message added",
            extra={"content_length": len(content), "content_preview": content[:200]},
        )

    def add_assistant_message(
        self, content: str, tool_calls: list[dict[str, Any]] | None = None
    ) -> None:
        """Append an assistant message with optional tool call requests.

        Mirrors HistoryManager._append_tool_calls_to_history() in unique_toolkit:
        tool call structs are embedded in the assistant message per OpenAI format.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._loop_history.append(msg)
        logger.debug(
            "HistoryManager: assistant message added",
            extra={
                "has_tool_calls": bool(tool_calls),
                "tool_call_names": [tc.get("function", {}).get("name") for tc in (tool_calls or [])],
                "content_length": len(content or ""),
            },
        )

    def add_tool_call_results(self, tool_responses: list[ToolCallResponse]) -> None:
        """Append tool execution results to the conversation history.

        Mirrors HistoryManager.add_tool_call_results() in unique_toolkit.
        Failed tools produce an error-description message so the LLM understands
        what happened and can proceed without that context.
        """
        for resp in tool_responses:
            if not resp.successful:
                self._loop_history.append(
                    {
                        "role": "tool",
                        "tool_call_id": resp.id,
                        "content": f"Tool {resp.name} failed: {resp.error_message}",
                    }
                )
                logger.warning(
                    "HistoryManager: tool failure recorded",
                    extra={"tool_name": resp.name, "error": resp.error_message},
                )
            else:
                content = resp.content
                if resp.content_chunks:
                    # Inline chunk texts so the LLM can reference them when generating
                    chunks_text = "\n".join(
                        f"[Source {i + 1}] {chunk.text}"
                        for i, chunk in enumerate(resp.content_chunks)
                    )
                    content = f"{resp.content}\n\nSources:\n{chunks_text}" if resp.content else chunks_text
                self._loop_history.append(
                    {
                        "role": "tool",
                        "tool_call_id": resp.id,
                        "content": content,
                    }
                )
                logger.debug(
                    "HistoryManager: tool result recorded",
                    extra={
                        "tool_name": resp.name,
                        "content_length": len(content),
                        "chunk_count": len(resp.content_chunks or []),
                    },
                )
        logger.info(
            "HistoryManager: tool results batch added",
            extra={
                "tool_response_count": len(tool_responses),
                "total_messages": len(self._loop_history),
            },
        )

    # ── History retrieval with token reducer ──────────────────────────────────

    def get_history_for_model_call(self) -> list[dict[str, Any]]:
        """Return conversation history trimmed to fit within the token budget.

        Mirrors HistoryManager.get_history_for_model_call() in unique_toolkit.
        Implements the Loop Token Reducer:
          1. Estimate total tokens for the full history
          2. If over budget, preserve all system messages
          3. Drop oldest non-system messages until within budget
        """
        full_history = list(self._loop_history)
        estimated_tokens = self._estimate_tokens(full_history)

        if estimated_tokens <= self._max_token_budget:
            logger.debug(
                "HistoryManager: history within token budget",
                extra={
                    "estimated_tokens": estimated_tokens,
                    "budget": self._max_token_budget,
                    "message_count": len(full_history),
                    "roles_breakdown": {r: sum(1 for m in full_history if m.get("role") == r) for r in {"system", "user", "assistant", "tool"}},
                },
            )
            return full_history

        # Loop Token Reducer — keep all system messages, trim oldest non-system
        system_messages = [m for m in full_history if m.get("role") == "system"]
        non_system = [m for m in full_history if m.get("role") != "system"]

        reduced = list(non_system)
        while reduced and self._estimate_tokens(system_messages + reduced) > self._max_token_budget:
            removed = reduced.pop(0)
            logger.debug(
                "HistoryManager: Loop Token Reducer removed message",
                extra={"removed_role": removed.get("role")},
            )

        result = system_messages + reduced
        logger.warning(
            "HistoryManager: history trimmed by Loop Token Reducer",
            extra={
                "original_message_count": len(full_history),
                "trimmed_message_count": len(result),
                "original_estimated_tokens": estimated_tokens,
            },
        )
        return result

    # ── Tool call persistence ─────────────────────────────────────────────────

    def extract_message_tools(self) -> list[dict[str, Any]]:
        """Extract all tool call records from the loop history for persistence.

        Mirrors HistoryManager.extract_message_tools() in unique_toolkit.
        Returns a flat list of {tool_call_id, name, arguments} dicts that can be
        stored and replayed on the next conversation turn.
        """
        records: list[dict[str, Any]] = []
        for msg in self._loop_history:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    records.append(
                        {
                            "tool_call_id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}"),
                        }
                    )
        logger.debug(
            "HistoryManager.extract_message_tools completed",
            extra={"record_count": len(records)},
        )
        return records

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate token count using a character-based heuristic."""
        total_chars = sum(len(json.dumps(m, ensure_ascii=True)) for m in messages)
        return total_chars // self._APPROX_CHARS_PER_TOKEN


# ─── EvaluationManager ───────────────────────────────────────────────────────


class Evaluation(abc.ABC):
    """Abstract base for response quality evaluations.

    Mirrors unique_toolkit.agentic.evaluation.Evaluation.
    Implement run() with your detection logic.
    """

    def __init__(self, name: str) -> None:
        """Initialize with a unique evaluation identifier."""
        self.name = name

    def get_name(self) -> str:
        """Return the evaluation name."""
        return self.name

    @abc.abstractmethod
    async def run(self, response_text: str) -> EvaluationMetricResult:
        """Evaluate the generated response and return a metric result."""


class EvaluationManager:
    """Runs pluggable quality/compliance evaluations on the final response.

    Mirrors EvaluationManager in unique_toolkit:
      - Evaluations registered via add_evaluation()
      - All evaluations run concurrently via asyncio.gather()
      - In a full Unique platform deployment, results surface to the chat UI
        (PENDING placeholder → final result) via ChatService.modify_message_assessment
    """

    def __init__(self) -> None:
        """Initialize with an empty evaluation registry."""
        self._evaluations: dict[str, Evaluation] = {}
        logger.debug("EvaluationManager initialized")

    def add_evaluation(self, evaluation: Evaluation) -> None:
        """Register an evaluation.

        Mirrors EvaluationManager.add_evaluation() from docs.
        """
        self._evaluations[evaluation.get_name()] = evaluation
        logger.info("EvaluationManager: evaluation registered", extra={"evaluation_name": evaluation.get_name()})

    def get_evaluation_by_name(self, name: str) -> Evaluation | None:
        """Return a registered evaluation by name, or None."""
        return self._evaluations.get(name)

    async def run_evaluations(self, response_text: str) -> list[EvaluationMetricResult]:
        """Run all registered evaluations concurrently and return their results.

        Mirrors EvaluationManager.run_evaluations() in unique_toolkit.
        Exceptions from individual evaluations are caught and recorded as failures
        so a single broken evaluation never halts the response pipeline.
        """
        if not self._evaluations:
            logger.debug("EvaluationManager: no evaluations registered; skipping")
            return []

        logger.info(
            "EvaluationManager: starting evaluation batch",
            extra={"evaluation_count": len(self._evaluations), "response_length": len(response_text)},
        )

        tasks = [ev.run(response_text) for ev in self._evaluations.values()]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        evaluation_results: list[EvaluationMetricResult] = []
        for ev_name, raw in zip(self._evaluations.keys(), raw_results):
            if isinstance(raw, Exception):
                logger.warning(
                    "EvaluationManager: evaluation raised an exception",
                    extra={"evaluation_name": ev_name, "error": str(raw)},
                )
                evaluation_results.append(
                    EvaluationMetricResult(
                        name=ev_name,
                        is_positive=False,
                        value="ERROR",
                        reason=f"Evaluation raised an exception: {raw}",
                    )
                )
            else:
                evaluation_results.append(raw)
                logger.info(
                    "EvaluationManager: evaluation completed",
                    extra={
                        "evaluation_name": ev_name,
                        "is_positive": raw.is_positive,
                        "value": raw.value,
                    },
                )

        return evaluation_results


# ─── PostprocessorManager ────────────────────────────────────────────────────


class Postprocessor(abc.ABC):
    """Abstract base for response postprocessors.

    Mirrors unique_toolkit.agentic.postprocessor.Postprocessor.
    Implement run() to transform the response; implement remove_from_text()
    to clean up artifacts before the next LLM call.
    """

    def __init__(self, name: str) -> None:
        """Initialize with a unique postprocessor identifier."""
        self.name = name

    def get_name(self) -> str:
        """Return the postprocessor name."""
        return self.name

    @abc.abstractmethod
    async def run(self, response_text: str) -> str:
        """Transform the response text and return the modified version."""

    async def remove_from_text(self, text: str) -> str:
        """Remove artifacts added by this postprocessor from the text.

        Mirrors Postprocessor.remove_from_text() — called by HistoryManager
        before each LLM round so the model is not confused by postprocessing
        additions (e.g. disclaimer markers, stock ticker blocks).
        """
        return text


class PostprocessorManager:
    """Runs and manages pluggable response postprocessors.

    Mirrors PostprocessorManager in unique_toolkit:
      - run() called in parallel, results applied sequentially
      - remove_from_text() exposed to HistoryManager for history cleanup
    """

    def __init__(self) -> None:
        """Initialize with an empty postprocessor list."""
        self._postprocessors: list[Postprocessor] = []
        logger.debug("PostprocessorManager initialized")

    def add_postprocessor(self, pp: Postprocessor) -> None:
        """Register a postprocessor.

        Mirrors PostprocessorManager.add_postprocessor() from docs.
        """
        self._postprocessors.append(pp)
        logger.info(
            "PostprocessorManager: postprocessor registered",
            extra={"postprocessor_name": pp.get_name()},
        )

    def get_postprocessors(self) -> list[Postprocessor]:
        """Return all registered postprocessors."""
        return list(self._postprocessors)

    async def run_postprocessors(self, response_text: str) -> str:
        """Run all postprocessors concurrently, then apply modifications sequentially.

        Mirrors PostprocessorManager.run_postprocessors() in unique_toolkit.
        The last successfully transformed version is the final output.
        """
        if not self._postprocessors:
            logger.debug("PostprocessorManager: no postprocessors; returning text unchanged")
            return response_text

        logger.info(
            "PostprocessorManager: starting postprocessor batch",
            extra={"postprocessor_count": len(self._postprocessors)},
        )

        tasks = [pp.run(response_text) for pp in self._postprocessors]
        transformed_versions = await asyncio.gather(*tasks, return_exceptions=True)

        final_text = response_text
        for pp, transformed in zip(self._postprocessors, transformed_versions):
            if isinstance(transformed, Exception):
                logger.warning(
                    "PostprocessorManager: postprocessor raised an exception",
                    extra={"postprocessor_name": pp.get_name(), "error": str(transformed)},
                )
                continue
            if isinstance(transformed, str) and transformed != response_text:
                final_text = transformed
                logger.info(
                    "PostprocessorManager: postprocessor applied",
                    extra={
                        "postprocessor_name": pp.get_name(),
                        "original_length": len(response_text),
                        "new_length": len(final_text),
                    },
                )

        return final_text

    async def remove_from_text(self, text: str) -> str:
        """Remove postprocessing artifacts from text for history cleanup.

        Called by HistoryManager before each LLM call.
        Mirrors PostprocessorManager.remove_from_text() in unique_toolkit.
        """
        for pp in self._postprocessors:
            try:
                text = await pp.remove_from_text(text)
            except Exception:
                logger.debug(
                    "PostprocessorManager: remove_from_text failed",
                    extra={"postprocessor_name": pp.get_name()},
                )
        return text


# ─── Concrete evaluations ─────────────────────────────────────────────────────


class FinancialSafetyEvaluation(Evaluation):
    """Flags responses that contain unsafe financial-guarantee language.

    Implements a compliance check aligned with the EvaluationManager pattern
    from unique_toolkit_agentic_framework_managers.md (Hallucination/Compliance).
    Phrases like "guaranteed to grow" or "you must buy" trigger a FAIL result.
    """

    _UNSAFE_PHRASES: frozenset[str] = frozenset(
        {
            "will definitely",
            "guaranteed to",
            "certain to grow",
            "no risk",
            "100% safe",
            "you must buy",
            "you should buy",
            "i recommend buying",
            "will certainly increase",
        }
    )

    def __init__(self) -> None:
        """Initialize with the financial safety evaluation identifier."""
        super().__init__(name="financial_safety")

    async def run(self, response_text: str) -> EvaluationMetricResult:
        """Check for unsafe financial language in the response text."""
        logger.info(
            "FinancialSafetyEvaluation: starting run",
            extra={"response_length": len(response_text)},
        )
        lower_text = response_text.lower()
        flagged = [phrase for phrase in self._UNSAFE_PHRASES if phrase in lower_text]

        is_safe = not bool(flagged)
        result = EvaluationMetricResult(
            name=self.name,
            is_positive=is_safe,
            value="PASS" if is_safe else "FAIL",
            reason=(
                "No unsafe financial guarantee language detected."
                if is_safe
                else f"Potentially unsafe phrases detected: {', '.join(sorted(flagged))}"
            ),
        )
        logger.info(
            "FinancialSafetyEvaluation: completed",
            extra={
                "is_positive": result.is_positive,
                "value": result.value,
                "flagged_phrase_count": len(flagged),
            },
        )
        return result


# ─── Concrete postprocessors ──────────────────────────────────────────────────


class FinancialDisclaimerPostprocessor(Postprocessor):
    """Appends a standard financial services disclaimer to every response.

    Implements the Postprocessor pattern from unique_toolkit_agentic_framework_managers.md:
      - run() enriches the response text
      - remove_from_text() strips the artifact so LLM history stays clean
    """

    _DISCLAIMER: str = (
        "\n\n---\n*This information is for relationship management purposes only and does not "
        "constitute financial advice. Past performance is not indicative of future results.*"
    )
    _MARKER: str = "---\n*This information is for relationship management purposes only"

    def __init__(self) -> None:
        """Initialize with the disclaimer postprocessor identifier."""
        super().__init__(name="financial_disclaimer")

    async def run(self, response_text: str) -> str:
        """Append the disclaimer if it is not already present in the response."""
        if self._MARKER in response_text:
            logger.debug("FinancialDisclaimerPostprocessor: disclaimer already present; skipping")
            return response_text
        enriched = response_text + self._DISCLAIMER
        logger.info(
            "FinancialDisclaimerPostprocessor: disclaimer appended",
            extra={"original_length": len(response_text), "enriched_length": len(enriched)},
        )
        return enriched

    async def remove_from_text(self, text: str) -> str:
        """Remove the disclaimer from LLM history to prevent confusing the model.

        Mirrors Postprocessor.remove_from_text() from docs — called by HistoryManager
        at the start of each loop iteration.
        """
        marker_pos = text.rfind("\n\n---\n*This information is for relationship management")
        if marker_pos != -1:
            cleaned = text[:marker_pos]
            logger.debug("FinancialDisclaimerPostprocessor: disclaimer removed from history text")
            return cleaned
        return text
