
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
PortfolioAgent and CrmAgent are registered as Tool subclasses.
The orchestrator exposes their tool_description() as LLM function definitions,
executes them via run(), and feeds ToolCallResponse into all managers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agents.base_tool import Tool
from app.agents.crm_agent import CrmAgent
from app.agents.portfolio_agent import PortfolioAgent
from app.errors import RoutingError
from app.schemas import AgentAnswer, EvaluationMetricResult, RelationshipManagerRequest, RelationshipManagerResponse, ToolCallResponse
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
        portfolio_agent: PortfolioAgent,
        crm_agent: CrmAgent,
        unique_toolkit: UniqueToolkit,
        settings: Settings,
    ) -> None:
        """Initialize orchestrator with sub-agent tools and runtime settings."""
        self.portfolio_agent = portfolio_agent
        self.crm_agent = crm_agent
        self.unique_toolkit = unique_toolkit
        self.settings = settings
        # Tool registry — mirrors ToolManager.get_tools()
        self._tools: dict[str, Tool] = {
            portfolio_agent.name: portfolio_agent,
            crm_agent.name: crm_agent,
        }

    async def handle_request(self, request: RelationshipManagerRequest) -> RelationshipManagerResponse:
        """Run the iterative plan-and-execute loop aligned with Unique orchestrator semantics.

        Instantiates all five managers fresh per request (stateless-per-request pattern).
        Logs progress at each stage so every action is visible in application logs.
        """
        logger.info(
            "Orchestrator: request received",
            extra={"customer_id": request.customer_id, "question": request.question},
        )

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

        # ── Fresh session check — mirrors orchestrator startup indicator ────────
        if history_manager.has_no_loop_messages():
            logger.info(
                "Orchestrator: fresh session detected — starting agentic loop",
                extra={"customer_id": request.customer_id},
            )

        # ── Seed history with system prompt and user message ──────────────────
        system_prompt = self._build_system_prompt(request.customer_id)
        history_manager.add_system_message(system_prompt)
        history_manager.add_user_message(request.question)
        logger.debug("Orchestrator: initial history seeded", extra={"system_prompt_length": len(system_prompt)})

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
                    "tools_called_so_far": len(all_agent_answers),
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
            logger.debug("Orchestrator: HistoryManager updated with tool results")

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
                    agent_name_key = resp.name.replace("_agent", "")  # "portfolio_agent" → "portfolio"
                    all_agent_answers.append(
                        AgentAnswer(
                            agent_name=agent_name_key,  # type: ignore[arg-type]
                            summary=resp.content,
                            # retrieved_context is intentionally empty here — actual customer
                            # data lives in resp.content (summary) and resp.content_chunks
                            # (processed by ReferenceManager for citations).
                            retrieved_context={},
                        )
                    )
                    logger.info(
                        "Orchestrator: agent answer collected",
                        extra={"agent_name": agent_name_key, "summary_length": len(resp.content)},
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

        logger.info(
            "Orchestrator: running EvaluationManager",
            extra={"final_answer_length": len(final_answer)},
        )
        evaluation_results = await evaluation_manager.run_evaluations(final_answer)

        logger.info(
            "Orchestrator: running PostprocessorManager",
            extra={"postprocessor_count": len(postprocessor_manager.get_postprocessors())},
        )
        final_answer = await postprocessor_manager.run_postprocessors(final_answer)

        # ── Tool call persistence (mirrors Unique Toolkit pattern) ─────────────
        persisted_tools = history_manager.extract_message_tools()
        debug_info_manager.add("persisted_tool_calls", persisted_tools)
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
            f"When calling any tool always pass \"customer_id\": \"{customer_id}\" in the arguments.\n\n"
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

    def _routing_from_answers(self, agent_answers: list[AgentAnswer]) -> list[str]:
        """Derive the routing decision from which agents were actually called."""
        unique_names = {answer.agent_name for answer in agent_answers}
        ordered = ["portfolio", "crm"]
        return [name for name in ordered if name in unique_names]

    def _deduplicate_agent_answers(self, agent_answers: list[AgentAnswer]) -> list[AgentAnswer]:
        """Keep only the last answer per agent to avoid duplicate entries in the response."""
        latest: dict[str, AgentAnswer] = {}
        for answer in agent_answers:
            latest[answer.agent_name] = answer
        ordered = ["portfolio", "crm"]
        return [latest[name] for name in ordered if name in latest]
