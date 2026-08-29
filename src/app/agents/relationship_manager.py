
"""Relationship manager orchestrator — uses all five Unique Toolkit managers.

Architecture
------------
The orchestrator mirrors the Unique Toolkit Orchestrator pattern described in:
  docs/unique_toolkit_agentic_framework_core.md
  docs/unique_toolkit_agentic_framework_managers.md

On each request a fresh set of managers is instantiated (stateless-per-request),
matching the Unique platform's "ChatService is stateful but per-event" convention.

Manager wiring
--------------
HistoryManager       — builds token-window-aware LLM message lists
ReferenceManager     — collects ContentChunks from tool responses for citations
DebugInfoManager     — captures per-tool debug_info and runtime metadata
EvaluationManager    — runs FinancialSafetyEvaluation after final answer
PostprocessorManager — appends FinancialDisclaimerPostprocessor to final answer

Tool wiring
-----------
Every underlying data API (portfolio + CRM) is registered as its own DataQueryTool.
The orchestrator exposes their tool_description() as LLM function definitions,
executes them via run(), and feeds ToolCallResponse into all managers. The LLM
decides which tool(s) to call — no hardcoded per-domain routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.agents.base_tool import Tool
from app.agents.mcp_tool_wrapper import MCP_TOOL_PREFIX, McpToolWrapper
from app.errors import RoutingError
from app.schemas import AgentAnswer, ConversationTurn, EvaluationMetricResult, RelationshipManagerRequest, RelationshipManagerResponse, ToolCallResponse

if TYPE_CHECKING:
    from app.services.mcp_manager import McpManager
    from app.services.session_service import UniqueSessionService
from app.services.managers import (
    DebugInfoManager,
    EvaluationManager,
    FinancialDisclaimerPostprocessor,
    FinancialSafetyEvaluation,
    HistoryManager,
    PostprocessorManager,
    ReferenceManager,
)
from app.services.unique_toolkit import UniqueToolkit
from app.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    """Represent one LLM-requested tool invocation."""

    id: str
    name: str
    arguments: str


class RelationshipManagerOrchestrator:
    """Orchestrate a question to the portfolio and CRM sub-agents.

    Mirrors the Unique Toolkit Orchestrator (unique_toolkit_agentic_framework_core.md):
      1. Checks for a fresh session (HistoryManager.has_no_loop_messages)
      2. Iterates: plan → execute tools → update all managers → repeat
      3. Final iteration: forces answer with tools disabled
      4. Runs EvaluationManager and PostprocessorManager before returning
      5. Persists tool call records via HistoryManager.extract_message_tools
    """

    def __init__(
        self,
        tools: Sequence[Tool],
        unique_toolkit: UniqueToolkit,
        settings: Settings,
        mcp_manager: "McpManager | None" = None,
        session_service: "UniqueSessionService | None" = None,
    ) -> None:
        """Initialize orchestrator with the data tools and runtime settings.

        Args:
            tools:           The data-query tools (portfolio + CRM) exposed to the LLM.
                             Each is a Tool with a ``domain`` used for AgentAnswer routing.
            unique_toolkit:  Facade over the Unique AI SDK.
            settings:        Runtime settings.
            mcp_manager:     Optional MCP Manager.  When provided and configured,
                             MCP tools are discovered at runtime and exposed to the
                             LLM as additional function definitions alongside the
                             built-in tools.  The LLM generates arguments from
                             each tool's own schema — no hardcoding required.
            session_service: Optional UniqueSessionService.  When provided,
                             conversation history is loaded from and persisted to
                             Unique AI using session_id as chatId.
        """
        self.unique_toolkit = unique_toolkit
        self.settings = settings
        self._mcp_manager = mcp_manager
        self._session_service = session_service
        # Tool registry — mirrors ToolManager.get_tools()
        # MCP tools are added lazily on the first request via _ensure_mcp_tools_loaded.
        self._tools: dict[str, Tool] = {tool.name: tool for tool in tools}
        logger.info(
            "Orchestrator: data tools registered",
            extra={
                "tool_names": list(self._tools.keys()),
                "domains": sorted({t.domain for t in self._tools.values() if t.domain}),
            },
        )
        self._mcp_tools_discovered: bool = False  # set True after first discovery attempt

    async def _ensure_mcp_tools_loaded(self) -> None:
        """Discover MCP tools on the first request and add them to the tool registry.

        This is the standard MCP integration pattern: tools are discovered from
        the live server, their schemas are passed verbatim to the LLM as function
        definitions, and the LLM generates correct arguments itself.

        Called once per orchestrator instance (idempotent after first success).
        Failures are caught and logged — the app continues without MCP tools rather
        than crashing.
        """
        if self._mcp_tools_discovered:
            return
        self._mcp_tools_discovered = True  # mark before await to avoid races

        if self._mcp_manager is None or not self._mcp_manager.is_configured:
            logger.info("Orchestrator: MCP manager not configured — skipping tool discovery")
            return

        try:
            mcp_tools = await self._mcp_manager.list_tools()
            for tool_info in mcp_tools:
                wrapper = McpToolWrapper(tool_info, self._mcp_manager)
                self._tools[wrapper.name] = wrapper
                logger.debug(
                    "Orchestrator: MCP tool registered",
                    extra={"orchestrator_name": wrapper.name, "mcp_name": tool_info.name},
                )
            logger.info(
                "Orchestrator: MCP tools discovered and registered",
                extra={
                    "mcp_tool_count": len(mcp_tools),
                    "mcp_tool_names": [t.name for t in mcp_tools],
                    "orchestrator_names": [f"{MCP_TOOL_PREFIX}{t.name}" for t in mcp_tools],
                    "total_tools_now": len(self._tools),
                    "all_tool_names": list(self._tools.keys()),
                },
            )
        except Exception as exc:
            logger.warning(
                "Orchestrator: MCP tool discovery failed — proceeding without MCP tools",
                extra={"error": str(exc), "mcp_server_url": getattr(self._mcp_manager, "_server_url", "unknown")},
            )

    async def handle_request(self, request: RelationshipManagerRequest) -> RelationshipManagerResponse:
        """Run the iterative plan-and-execute loop aligned with Unique orchestrator semantics.

        Instantiates all five managers fresh per request (stateless-per-request pattern).
        Logs progress at each stage so every action is visible in application logs.
        """
        logger.info(
            "Orchestrator: request received",
            extra={"customer_id": request.customer_id, "question": request.question},
        )

        # Discover MCP tools on first request (idempotent, fails soft).
        await self._ensure_mcp_tools_loaded()

        # ── Manager initialization (fresh per request) ────────────────────────
        history_manager = HistoryManager(max_token_budget=self.settings.unique_max_history_tokens)
        reference_manager = ReferenceManager()
        debug_info_manager = DebugInfoManager()
        evaluation_manager = EvaluationManager()
        postprocessor_manager = PostprocessorManager()

        # Register evaluations — mirrors EvaluationManager.add_evaluation()
        evaluation_manager.add_evaluation(FinancialSafetyEvaluation())

        # Register postprocessors — mirrors PostprocessorManager.add_postprocessor()
        postprocessor_manager.add_postprocessor(FinancialDisclaimerPostprocessor())

        logger.info("Orchestrator: all managers initialized and configured")

        # ── Load session history from Unique AI ───────────────────────────────
        # session_id acts as chatId in the Unique platform.
        # Falls back to request.chat_history when Unique AI returns nothing.
        session_id = request.session_id or self.settings.unique_default_session_id
        fetched_history: list[dict[str, str]] = []
        if self._session_service is not None:
            fetched_history = await self._session_service.load_history(
                session_id,
                user_id=request.auth_user_id,
                company_id=request.auth_company_id,
                assistant_id=request.assistant_id,
            )
            logger.info(
                "Orchestrator: history fetched from Unique AI",
                extra={"session_id": session_id, "fetched_turns": len(fetched_history)},
            )

        if not fetched_history and request.chat_history:
            fetched_history = [{"role": t.role, "content": t.content} for t in request.chat_history]
            logger.info(
                "Orchestrator: using request.chat_history fallback",
                extra={"fallback_turns": len(fetched_history)},
            )

        # Seed HistoryManager with prior turns BEFORE adding the system message so
        # has_no_loop_messages() returns True only when no prior turns were loaded.
        # get_history_for_model_call() always reorders system messages to the front,
        # so LLM message ordering is correct regardless of insertion order here.
        loaded_user = 0
        loaded_assistant = 0
        for msg in fetched_history:
            if msg["role"] == "user":
                history_manager.add_user_message(msg["content"], source="history")
                loaded_user += 1
            elif msg["role"] == "assistant":
                history_manager.add_assistant_message(msg["content"], source="history")
                loaded_assistant += 1

        # has_no_loop_messages() is now True iff no prior turns were loaded — mirrors
        # the Unique Toolkit orchestrator pattern for fresh-session detection.
        is_fresh_session = history_manager.has_no_loop_messages()
        if is_fresh_session:
            logger.info(
                "Orchestrator: fresh session detected — no prior history",
                extra={"customer_id": request.customer_id, "session_id": session_id},
            )
        else:
            logger.info(
                "Orchestrator: resuming conversation — prior history loaded",
                extra={
                    "customer_id": request.customer_id,
                    "session_id": session_id,
                    "user_turns_loaded": loaded_user,
                    "assistant_turns_loaded": loaded_assistant,
                    "total_turns_loaded": loaded_user + loaded_assistant,
                },
            )

        # ── Add system prompt then current question ───────────────────────────
        system_prompt = self._build_system_prompt(request.customer_id)
        history_manager.add_system_message(system_prompt)
        logger.debug(
            "Orchestrator: system message added to history",
            extra={"system_prompt_length": len(system_prompt)},
        )

        history_manager.add_user_message(request.question, source="current")
        logger.debug(
            "Orchestrator: current user question added to history",
            extra={"question_length": len(request.question), "question_preview": request.question[:120]},
        )

        # ── Build tool definitions from registered Tool instances ──────────────
        tool_definitions = self._get_tool_definitions()
        logger.info(
            "Orchestrator: tool definitions built",
            extra={"tool_count": len(tool_definitions), "tool_names": [t["function"]["name"] for t in tool_definitions]},
        )

        max_iterations = max(1, self.settings.unique_agent_max_iterations)
        all_agent_answers: list[AgentAnswer] = []
        final_answer: str = ""
        evaluation_results: list[EvaluationMetricResult] = []
        iteration_index: int = 0  # initialized so post-loop reference is always defined
        total_tool_calls_executed: int = 0  # counts ALL tools (portfolio, crm, mcp__*)
        triggered_tool_names: list[str] = []  # every tool name triggered, in call order
        context: dict[str, Any] = {
            "customer_id": request.customer_id,
            "question": request.question,
        }

        # ── Main agentic loop ─────────────────────────────────────────────────
        for iteration_index in range(max_iterations):
            logger.info(
                "Orchestrator: loop iteration started",
                extra={
                    "iteration_index": iteration_index,
                    "max_iterations": max_iterations,
                    # Total tool calls executed so far (portfolio + crm + mcp__* combined)
                    "tools_called_so_far": total_tool_calls_executed,
                    "agent_answers_so_far": len(all_agent_answers),
                },
            )

            is_last_iteration = iteration_index == (max_iterations - 1)

            # Get token-window-safe history for this LLM call
            messages = history_manager.get_history_for_model_call()
            logger.debug(
                "Orchestrator: history retrieved for model call",
                extra={"message_count": len(messages)},
            )

            if is_last_iteration:
                # Last iteration: tools disabled — force final answer
                # Mirrors Unique orchestrator "last iteration no-tools" mode
                logger.info("Orchestrator: last iteration — disabling tools for final answer")
                planning_result = self.unique_toolkit.plan_with_tools(
                    messages=messages,
                    tool_definitions=[],
                    allow_tools=False,
                )
                final_answer = planning_result.get("content", "") or self._combine_answers(all_agent_answers)
                debug_info_manager.add("loop_exit_reason", "max_iterations_reached")
                break

            # ── Planning step ──────────────────────────────────────────────────
            logger.info("Orchestrator: planning step — calling LLM with tool definitions")
            planning_result = await self._plan_iteration(
                history_messages=messages,
                tool_definitions=tool_definitions,
            )

            raw_tool_calls = planning_result.get("tool_calls", [])
            tool_calls = self._parse_tool_calls(raw_tool_calls)
            tool_calls = self._filter_duplicate_tool_calls(tool_calls)
            tool_calls = self._limit_tool_calls(tool_calls)

            logger.info(
                "Orchestrator: planning step completed",
                extra={
                    "raw_tool_calls": len(raw_tool_calls),
                    "parsed_tool_calls": len(tool_calls),
                    "tool_names": [tc.name for tc in tool_calls],
                },
            )

            if not tool_calls:
                # The LLM declined to call tools. On the FIRST pass this is almost
                # always wrong (the configured api_version 2023-12-06 cannot force
                # tool_choice="required", so we cannot rely on the model). Fall back
                # to deterministic keyword routing so customer data is always fetched.
                if iteration_index == 0 and not all_agent_answers:
                    tool_calls = self._deterministic_tool_calls(
                        customer_id=request.customer_id,
                        question=request.question,
                    )
                    logger.info(
                        "Orchestrator: LLM returned no tool calls on first pass — "
                        "applying deterministic fallback routing (last resort)",
                        extra={
                            "fallback_reason": "llm_returned_zero_tool_calls",
                            "routed_tools": [tc.name for tc in tool_calls],
                            "note": (
                                "If this triggers frequently, check unique_client.extract_tool_calls "
                                "— the SDK may be returning toolCalls (camelCase) which must be normalised."
                            ),
                        },
                    )

            if not tool_calls:
                # No tools requested — LLM produced a direct final answer
                final_answer = planning_result.get("content", "") or self._combine_answers(all_agent_answers)
                debug_info_manager.add("loop_exit_reason", "no_tool_calls_requested")
                logger.info(
                    "Orchestrator: no tool calls requested — using direct LLM answer",
                    extra={"final_answer_length": len(final_answer)},
                )
                break

            # ── Tool execution ─────────────────────────────────────────────────
            logger.info(
                "Orchestrator: executing tool calls concurrently",
                extra={"tool_call_count": len(tool_calls)},
            )
            tool_responses: list[ToolCallResponse] = await self._execute_selected_tools(
                tool_calls=tool_calls,
                context=context,
            )

            # ── Update all managers with tool results ──────────────────────────
            # 1. History: record tool call requests (assistant msg) + results (tool msgs)
            history_manager.add_assistant_message(
                content="",
                tool_calls=[
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in tool_calls
                ],
            )
            history_manager.add_tool_call_results(tool_responses)
            logger.debug(
                "Orchestrator: HistoryManager updated with tool results",
                extra={"tool_response_count": len(tool_responses)},
            )
            total_tool_calls_executed += len(tool_responses)
            triggered_tool_names.extend(tc.name for tc in tool_calls)
            logger.info(
                "Orchestrator: tools triggered this iteration",
                extra={
                    "iteration_index": iteration_index,
                    "iteration_tools": [tc.name for tc in tool_calls],
                    "all_tools_triggered_so_far": triggered_tool_names,
                },
            )

            # 2. ReferenceManager: extract content chunks for citations
            reference_manager.extract_referenceable_chunks(tool_responses)
            logger.debug(
                "Orchestrator: ReferenceManager updated",
                extra={"total_chunks": len(reference_manager.get_chunks())},
            )

            # 3. DebugInfoManager: harvest per-tool debug traces
            debug_info_manager.extract_from_tool_responses(tool_responses)
            logger.debug("Orchestrator: DebugInfoManager updated with tool traces")

            # 4. Collect AgentAnswer records for routing and response payload
            for resp in tool_responses:
                if resp.successful:
                    # A tool contributes an AgentAnswer only when it maps to a sub-agent
                    # domain (portfolio/crm). MCP tools (domain "") contribute
                    # content_chunks to ReferenceManager instead — their results flow
                    # into the LLM context via history.
                    tool = self._tools.get(resp.name)
                    domain = tool.domain if tool is not None else ""
                    if domain not in ("portfolio", "crm"):
                        logger.debug(
                            "Orchestrator: non-domain tool result recorded in history (not in agent_answers)",
                            extra={"tool_name": resp.name, "domain": domain},
                        )
                        continue
                    all_agent_answers.append(
                        AgentAnswer(
                            agent_name=domain,  # type: ignore[arg-type]
                            tool_name=resp.name,
                            summary=resp.content,
                            # retrieved_context is intentionally empty here — actual customer
                            # data lives in resp.content (summary) and resp.content_chunks
                            # (processed by ReferenceManager for citations).
                            retrieved_context={},
                        )
                    )
                    logger.info(
                        "Orchestrator: agent answer collected",
                        extra={
                            "agent_name": domain,
                            "tool_name": resp.name,
                            "summary_length": len(resp.content),
                        },
                    )
                else:
                    logger.warning(
                        "Orchestrator: tool call failed — excluded from agent answers",
                        extra={"tool_name": resp.name, "error": resp.error_message},
                    )

            # ── Check if any tool takes control ───────────────────────────────
            # Mirrors Unique orchestrator control hand-off detection
            control_tool = next(
                (self._tools[tc.name] for tc in tool_calls if tc.name in self._tools and self._tools[tc.name].takes_control()),
                None,
            )
            if control_tool is not None:
                logger.info(
                    "Orchestrator: control hand-off detected — exiting loop",
                    extra={"control_tool": control_tool.name},
                )
                final_answer = self._combine_answers(all_agent_answers)
                debug_info_manager.add("loop_exit_reason", f"control_taken_by_{control_tool.name}")
                break

        # ── Post-loop: evaluation + postprocessing ─────────────────────────────
        if not final_answer:
            final_answer = self._combine_answers(all_agent_answers)

        # ── Inject reference citations from ReferenceManager ──────────────────
        references_section = self._build_references_section(reference_manager)
        if references_section:
            final_answer += references_section
            logger.info(
                "Orchestrator: reference citations appended to final answer",
                extra={"reference_count": len(reference_manager.get_chunks())},
            )

        logger.info(
            "Orchestrator: running EvaluationManager",
            extra={"final_answer_length": len(final_answer)},
        )
        evaluation_results = await evaluation_manager.run_evaluations(final_answer)

        # ── Persist turn to Unique AI before postprocessors add the disclaimer ─
        if self._session_service is not None and request.persist_turn:
            await self._session_service.save_turn(
                session_id=session_id,
                user_message=request.question,
                assistant_message=final_answer,
                user_id=request.auth_user_id,
                company_id=request.auth_company_id,
                assistant_id=request.assistant_id,
            )
            logger.info(
                "Orchestrator: conversation turn persisted to Unique AI",
                extra={"session_id": session_id},
            )
        elif not request.persist_turn:
            logger.info(
                "Orchestrator: skipping turn persistence (persist_turn=False; "
                "webhook flow writes the assistant message directly)",
                extra={"session_id": session_id},
            )

        logger.info(
            "Orchestrator: running PostprocessorManager",
            extra={"postprocessor_count": len(postprocessor_manager.get_postprocessors())},
        )
        final_answer = await postprocessor_manager.run_postprocessors(final_answer)

        # ── Tool call persistence (mirrors Unique Toolkit pattern) ─────────────
        persisted_tools = history_manager.extract_message_tools()
        debug_info_manager.add("persisted_tool_calls", persisted_tools)
        debug_info_manager.add("triggered_tool_names", triggered_tool_names)
        debug_info_manager.add("reference_chunk_count", len(reference_manager.get_chunks()))
        debug_info_manager.add("total_iterations", iteration_index + 1)
        debug_info_manager.add(
            "evaluation_summary",
            [{"name": r.name, "value": r.value, "is_positive": r.is_positive} for r in evaluation_results],
        )

        routing_decision = self._routing_from_answers(all_agent_answers)
        debug_info = debug_info_manager.get()

        logger.info(
            "Orchestrator: completed",
            extra={
                "customer_id": request.customer_id,
                "routing_decision": routing_decision,
                "agent_answer_count": len(all_agent_answers),
                "evaluation_count": len(evaluation_results),
                "reference_chunk_count": len(reference_manager.get_chunks()),
                "debug_entry_count": len(debug_info),
            },
        )

        # ── Signal loop completion (mirrors set_completed_at in Unique Toolkit) ─
        logger.info("Orchestrator: agentic loop finalized — set_completed_at equivalent")

        return RelationshipManagerResponse(
            customer_id=request.customer_id,
            question=request.question,
            routing_decision=routing_decision,
            final_answer=final_answer,
            agent_answers=self._deduplicate_agent_answers(all_agent_answers),
            debug_info=debug_info,
            evaluation_results=evaluation_results,
        )

    # ── Planning step ─────────────────────────────────────────────────────────

    async def _plan_iteration(
        self,
        *,
        history_messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Call the LLM to plan which tools to invoke.

        Mirrors the Unique orchestrator's _plan_or_execute step.
        """
        logger.debug(
            "Orchestrator._plan_iteration: calling LLM",
            extra={
                "message_count": len(history_messages),
                "tool_definition_names": [td["function"]["name"] for td in tool_definitions],
            },
        )
        result = self.unique_toolkit.plan_with_tools(
            messages=history_messages,
            tool_definitions=tool_definitions,
            allow_tools=True,
        )
        tool_calls_returned = result.get("tool_calls", [])
        content_returned = result.get("content") or ""
        logger.debug(
            "Orchestrator._plan_iteration: LLM responded",
            extra={
                "tool_calls_count": len(tool_calls_returned),
                "tool_calls_names": [tc.get("name") for tc in tool_calls_returned],
                "content_preview": content_returned[:200] if content_returned else "",
            },
        )
        return result

    # ── Tool execution ────────────────────────────────────────────────────────

    async def _execute_selected_tools(
        self,
        *,
        tool_calls: list[ToolCall],
        context: dict[str, Any],
    ) -> list[ToolCallResponse]:
        """Execute all tool calls concurrently.

        Mirrors ToolManager.execute_selected_tools() in unique_toolkit.
        """
        tasks = [
            self._execute_tool_call(tool_call=tc, context=context)
            for tc in tool_calls
        ]
        return await asyncio.gather(*tasks)

    async def _execute_tool_call(
        self,
        *,
        tool_call: ToolCall,
        context: dict[str, Any],
    ) -> ToolCallResponse:
        """Execute one tool call and return its ToolCallResponse.

        Mirrors the tool dispatch logic in ToolManager.
        """
        logger.info(
            "Orchestrator: dispatching tool call",
            extra={"tool_name": tool_call.name, "tool_call_id": tool_call.id},
        )
        tool = self._tools.get(tool_call.name)
        if tool is None or not tool.is_enabled():
            logger.error(
                "Orchestrator: unknown or disabled tool requested",
                extra={"tool_name": tool_call.name},
            )
            raise RoutingError(
                "Tool requested by LLM is not registered or is disabled.",
                {"tool_name": tool_call.name},
            )

        try:
            arguments = json.loads(tool_call.arguments or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "Orchestrator: failed to parse tool call arguments as JSON — using empty dict",
                extra={"tool_name": tool_call.name, "raw_arguments": tool_call.arguments},
            )
            arguments = {}

        logger.info(
            f">>> TOOL CALL INPUT [{tool_call.name}]",
            extra={
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.id,
                "arguments": arguments,
                "customer_id": context.get("customer_id"),
                "question": context.get("question"),
            },
        )

        response = await tool.run(
            tool_call_id=tool_call.id,
            arguments=arguments,
            context=context,
        )

        logger.info(
            f"<<< TOOL CALL OUTPUT [{tool_call.name}]  status={'OK' if response.successful else 'FAILED'}",
            extra={
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.id,
                "successful": response.successful,
                "content_length": len(response.content or ""),
                "content_preview": (response.content or "")[:500],
                "error_message": response.error_message or None,
            },
        )
        return response

    # ── Tool definition building ──────────────────────────────────────────────

    def _get_tool_definitions(self) -> list[dict[str, Any]]:
        """Build OpenAI-compatible function definitions from registered Tool instances.

        Mirrors ToolManager.get_tool_definitions() in unique_toolkit — each Tool's
        tool_description() provides the schema exposed to the LLM.
        """
        definitions: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if not tool.is_enabled():
                logger.debug("Orchestrator: tool excluded (disabled)", extra={"tool_name": tool.name})
                continue
            td = tool.tool_description()
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": td.name,
                        "description": td.description,
                        "parameters": td.parameters,
                    },
                }
            )
            logger.debug("Orchestrator: tool definition built", extra={"tool_name": td.name})
        return definitions

    # ── System prompt ─────────────────────────────────────────────────────────

    def _build_system_prompt(self, customer_id: str) -> str:
        """Build the orchestrator system prompt with tool guidance.

        Includes tool_description_for_system_prompt() from each Tool and
        embeds the current customer_id so the LLM always passes it as a
        tool parameter.  Also includes guidance for no-data scenarios.
        Mirrors the Unique orchestrator Jinja template rendering step.
        """
        tool_hints = "\n".join(
            f"- {tool.tool_description_for_system_prompt()}"
            for tool in self._tools.values()
            if tool.is_enabled() and tool.tool_description_for_system_prompt()
        )
        prompt = (
            "You are a relationship manager orchestrator running an iterative tool loop. "
            "Plan which tools are needed, call them, then produce a final concise answer. "
            "Only use facts from tool outputs — do not invent data.\n\n"
            f"Current customer ID: {customer_id}\n"
            f"When calling any customer-specific tool, always pass "
            f"\"customer_id\": \"{customer_id}\" in the arguments.\n"
            f"When calling MCP tools (names starting with mcp__), use the exact argument "
            f"names from the tool's own schema.  The customer's ID is {customer_id}.\n\n"
            "Tool selection:\n"
            "- Choose the specific tool(s) whose description best matches the question. "
            "You may call several tools when a question spans multiple areas.\n"
            "- Portfolio detail tools cover: current position/holdings, performance/returns, "
            "and compliance (credit/tax). CRM detail tools cover: profile/KYC, interactions/"
            "history, and advisory/suggestions.\n\n"
            "CRITICAL — tool call requirement:\n"
            "- You MUST call at least one tool before answering. Never answer directly from memory.\n"
            "- For broad or general questions (e.g. 'what details do you have about me', "
            "'tell me about this customer', 'give me a summary', 'what do you know'), call the "
            "core portfolio AND CRM tools needed to cover the customer's position and profile.\n"
            "- Only produce a final answer AFTER tool results have been returned.\n\n"
            "Handling missing or unavailable data:\n"
            "- If a tool returns an error (customer not found, retrieval failed), acknowledge "
            "that clearly in the final answer — do NOT retry the same failing tool.\n"
            "- If one source is unavailable but another succeeded, summarise what is available "
            "and note which source could not be reached.\n"
            "- If both sources fail, respond with a helpful message explaining that no data could "
            "be found for the given customer ID and advise verifying it.\n\n"
        )
        if tool_hints:
            prompt += f"Tool usage guidance:\n{tool_hints}\n"
        return prompt

    # ── Tool call parsing and filtering ──────────────────────────────────────

    def _parse_tool_calls(self, raw_tool_calls: list[dict[str, Any]]) -> list[ToolCall]:
        """Parse raw LLM tool call payloads into typed ToolCall objects."""
        parsed: list[ToolCall] = []
        for index, raw in enumerate(raw_tool_calls):
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            call_id = str(raw.get("id", "")).strip() or f"tool_call_{index}_{name}"
            arguments = str(raw.get("arguments", "{}"))
            parsed.append(ToolCall(id=call_id, name=name, arguments=arguments))
        logger.debug("Orchestrator: tool calls parsed", extra={"count": len(parsed)})
        return parsed

    def _deterministic_tool_calls(self, *, customer_id: str, question: str) -> list[ToolCall]:
        """Choose granular tool(s) by keyword when the LLM genuinely declines to call any.

        This is a TRUE last-resort safety net — it only runs when the LLM explicitly
        returns zero tool calls (e.g. API version limitation, model refusal). It must
        never run in place of a valid LLM response.

        Routing maps keywords to the SPECIFIC granular tool that serves them, so the
        right API is fetched (e.g. a "performance" question routes to
        portfolio_performance, not the generic snapshot). When nothing matches, it
        falls back to the two core per-customer views (snapshot + profile) so the
        answer is never empty. Only tools that are actually registered and enabled
        are selected, so this adapts automatically to the configured tool set.
        """
        text = question.lower()

        # Ordered keyword → granular tool routing. First match per tool wins; a
        # question may select several tools across domains.
        keyword_routes: list[tuple[str, tuple[str, ...]]] = [
            ("portfolio_performance", (
                "performance", "return", "returns", "alpha", "sharpe", "benchmark",
                "yield", "sector", "geographic", "exposure", "upcoming event",
            )),
            ("portfolio_compliance", (
                "line of credit", "loc", "tax", "portfolio alert",
            )),
            ("portfolio_snapshot", (
                "portfolio", "holding", "holdings", "allocation", "asset", "position",
                "p&l", "pnl", "profit", "loss", "invest", "investment", "aum",
                "equity", "equities", "fund", "funds", "stock", "stocks", "bond", "bonds",
            )),
            ("portfolio_book_summary", (
                "all customers", "book of business", "every customer", "across customers",
                "book summary", "all portfolios",
            )),
            ("crm_interactions", (
                "interaction", "interactions", "conversation", "meeting", "last call",
                "service request", "service ticket", "follow-up", "follow up", "sentiment",
            )),
            ("crm_advisory", (
                "advisory", "suggestion", "recommendation", "compliance flag",
            )),
            ("crm_profile", (
                "crm", "profile", "kyc", "nps", "churn", "segment", "demographic",
                "relationship manager", "contact",
            )),
            ("crm_book_summary", (
                "all customers", "pipeline", "every customer", "across customers",
                "book summary",
            )),
        ]

        selected: list[str] = []
        for tool_name, keywords in keyword_routes:
            if tool_name not in self._tools or not self._tools[tool_name].is_enabled():
                continue
            if any(kw in text for kw in keywords) and tool_name not in selected:
                selected.append(tool_name)

        if not selected:
            # No strong keyword match — fetch the two core per-customer views so the
            # final answer is complete across both domains.
            for default_name in ("portfolio_snapshot", "crm_profile"):
                if default_name in self._tools and self._tools[default_name].is_enabled():
                    selected.append(default_name)

        arguments = json.dumps({"customer_id": customer_id})
        calls = [
            ToolCall(id=f"deterministic_{index}_{name}", name=name, arguments=arguments)
            for index, name in enumerate(selected)
        ]
        logger.info(
            "Orchestrator: deterministic routing selected tools (LLM declined)",
            extra={"selected_tools": [c.name for c in calls]},
        )
        return calls

    def _filter_duplicate_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Remove duplicate (name, arguments) pairs.

        Mirrors ToolManager deduplication logic from unique_toolkit docs.
        """
        unique: list[ToolCall] = []
        seen: set[tuple[str, str]] = set()
        for tc in tool_calls:
            key = (tc.name, tc.arguments)
            if key in seen:
                logger.debug("Orchestrator: duplicate tool call filtered", extra={"tool_name": tc.name})
                continue
            seen.add(key)
            unique.append(tc)
        return unique

    def _limit_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Limit tool calls per iteration to avoid overload.

        Mirrors ToolManager call-count limit enforcement in unique_toolkit.
        """
        max_calls = max(1, self.settings.unique_max_tool_calls_per_iteration)
        if len(tool_calls) <= max_calls:
            return tool_calls
        logger.warning(
            "Orchestrator: tool calls capped at limit",
            extra={"requested": len(tool_calls), "limit": max_calls},
        )
        return tool_calls[:max_calls]

    # ── Response assembly helpers ─────────────────────────────────────────────

    def _combine_answers(self, agent_answers: list[AgentAnswer]) -> str:
        """Compose a fallback final answer from sub-agent outputs."""
        if not agent_answers:
            return (
                "I was unable to retrieve data for this customer from the available sources. "
                "Please verify the customer ID is correct and try again. "
                "If the issue persists, the customer record may not exist in the system."
            )
        return " ".join(answer.summary for answer in agent_answers if answer.summary)

    def _build_references_section(self, reference_manager: ReferenceManager) -> str:
        """Build a formatted sources block from ReferenceManager chunks.

        Uses build_reference_map() to assign sequential [1], [2], ... numbers
        across all chunks collected from every tool call in this request.
        """
        ref_map = reference_manager.build_reference_map()
        if not ref_map:
            return ""
        lines = ["\n\n**Sources:**"]
        for ref_num, chunk in ref_map.items():
            source = chunk.metadata.get("source", "")
            section = chunk.metadata.get("section", "")
            label = f"{source} · {section}" if source and section else chunk.id
            lines.append(f"[{ref_num}] {label}")
        logger.debug(
            "Orchestrator: reference section built",
            extra={"reference_count": len(ref_map)},
        )
        return "\n".join(lines)

    def _routing_from_answers(self, agent_answers: list[AgentAnswer]) -> list[str]:
        """Derive the routing decision from which agents were actually called."""
        unique_names = {answer.agent_name for answer in agent_answers}
        ordered = ["portfolio", "crm"]
        return [name for name in ordered if name in unique_names]

    def _deduplicate_agent_answers(self, agent_answers: list[AgentAnswer]) -> list[AgentAnswer]:
        """Keep the latest answer per (domain, tool) so each granular tool is represented once.

        Multiple tools in the same domain (e.g. portfolio_snapshot + portfolio_performance)
        are all preserved; only exact re-runs of the same tool collapse. Ordered portfolio
        answers first, then crm, preserving tool call order within each domain.
        """
        latest: dict[tuple[str, str], AgentAnswer] = {}
        order: list[tuple[str, str]] = []
        for answer in agent_answers:
            key = (answer.agent_name, answer.tool_name)
            if key not in latest:
                order.append(key)
            latest[key] = answer
        domain_rank = {"portfolio": 0, "crm": 1}
        order.sort(key=lambda k: domain_rank.get(k[0], 99))
        return [latest[key] for key in order]
