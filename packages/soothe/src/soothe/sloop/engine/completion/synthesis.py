"""Synthesis execution logic for comprehensive final report generation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from soothe_sdk.observability.langfuse import merge_langfuse_runnable_config

from soothe.config.constants import GOAL_COMPLETION_REPORT_MAX_CHARS
from soothe.sloop.engine.completion.scenario_classifier import (
    ScenarioClassification,
    _extract_execution_summary,
    _heuristic_classify,
)
from soothe.sloop.engine.completion.synthesis_projection import (
    build_synthesis_messages,
    render_synthesis_system_prompt,
)
from soothe.sloop.orchestrator.checkpoint import synthesis_thread_id
from soothe.sloop.state.schemas import LoopState
from soothe.sloop.utils.config_keys import SOOTHE_GOAL_SYNTHESIS_CONFIG_KEY
from soothe.sloop.utils.messages import tag_messages_stream_chunk_for_goal_completion
from soothe.sloop.utils.plan_action_text import resolve_plan_action_text
from soothe.utils.messages import extract_text_from_message_content

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from langchain_core.language_models.chat_models import BaseChatModel
    from soothe_sdk.protocols.core_agent import CoreAgentProtocol

    from soothe.config import SootheConfig
    from soothe.sloop.state.schemas import PlanResult

logger = logging.getLogger(__name__)

_DEFAULT_SYNTHESIS_EVIDENCE_MAX = GOAL_COMPLETION_REPORT_MAX_CHARS


class SynthesisGenerator:
    """Generate synthesis reports from execution evidence.

    Heuristic fast-path for obvious scenarios; otherwise single-pass
    scratchpad-mode LLM streaming with timeout-protected fallback.
    """

    def __init__(
        self,
        llm_client: BaseChatModel,
        core_agent: CoreAgentProtocol,
        soothe_config: SootheConfig | None = None,
        *,
        loop_id: str | None = None,
    ) -> None:
        """Initialize synthesis generator with LLM client and CoreAgent.

        Args:
            llm_client: Model for synthesis streaming.
            core_agent: CoreAgent for synthesis execution with streaming.
            soothe_config: Optional daemon config for evidence budgeting.
            loop_id: Optional loop identifier for Langfuse trace correlation.
        """
        self.llm = llm_client
        self.core_agent = core_agent
        self._soothe_config = soothe_config
        self._loop_id = loop_id

    async def generate_synthesis(
        self,
        goal: str,
        state: LoopState,
    ) -> AsyncGenerator:
        """Generate synthesis via LLM streaming.

        Heuristic fast-path for obvious scenarios (single-step, all-failed,
        high-step analysis). When the heuristic is inconclusive, renders the
        system prompt in scratchpad mode so the model self-classifies via
        `<analysis>` tags before writing the report body — collapsing the
        former two-phase classify→generate into a single LLM call.

        The scratchpad stream is wrapped in `asyncio.timeout` bounded by
        `dispatch_idle_seconds`. On timeout, falls back to
        `generate_user_fallback_summary`.

        Uses isolated checkpoint thread to prevent replay of parent StrangeLoop
        history.

        Args:
            goal: Goal description.
            state: Loop state with thread context and execution ledger (`loop_messages`).

        Yields:
            LangGraph `messages`-mode stream tuples tagged with `phase=goal_completion`.
        """
        classification, scratchpad_mode = self._resolve_classification(goal, state)

        max_total = self._synthesis_max_chars()
        ledger_cfg = None
        agent_instructions_max_chars = 8000
        if self._soothe_config is not None:
            ledger_cfg = self._soothe_config.agent.loop.plan_prompt_ledger
            agent_instructions_max_chars = int(
                self._soothe_config.agent.agent_instructions_max_chars
            )

        if scratchpad_mode:
            messages = self._build_scratchpad_messages(
                goal,
                state,
                classification,
                max_total,
                ledger_cfg,
                agent_instructions_max_chars,
            )
        else:
            messages = build_synthesis_messages(
                state,
                classification,
                user_query=goal,
                max_chars=max_total,
                ledger_cfg=ledger_cfg,
                agent_instructions_max_chars=agent_instructions_max_chars,
            )

        approx_chars = sum(
            len(extract_text_from_message_content(getattr(m, "content", ""))) for m in messages
        )
        execute_ledger_count = max(0, len(messages) - 2)
        logger.info(
            "Synthesis generator: scenario=%s sections=%d scratchpad=%s execute_ledger_msgs=%d prompt_msgs=%d approx_chars=%d",
            classification.scenario,
            len(classification.sections),
            scratchpad_mode,
            execute_ledger_count,
            len(messages),
            approx_chars,
        )

        graph_config = self._build_graph_config(state)

        from soothe.sloop.utils.token_usage import direct_llm_token_call_scope

        synthesis_start = time.perf_counter()
        logger.info(
            "Synthesis (generate): starting stream scenario=%s scratchpad=%s approx_chars=%d",
            classification.scenario,
            scratchpad_mode,
            approx_chars,
        )

        if scratchpad_mode:
            timeout_seconds = self._dispatch_idle_seconds()
            try:
                with direct_llm_token_call_scope():
                    if timeout_seconds > 0:
                        async with asyncio.timeout(timeout_seconds):
                            async for chunk in self.llm.astream(messages, config=graph_config):
                                yield tag_messages_stream_chunk_for_goal_completion(
                                    ((), "messages", (chunk, {})),
                                    thread_id=state.thread_id,
                                    iteration=state.iteration,
                                )
                    else:
                        async for chunk in self.llm.astream(messages, config=graph_config):
                            yield tag_messages_stream_chunk_for_goal_completion(
                                ((), "messages", (chunk, {})),
                                thread_id=state.thread_id,
                                iteration=state.iteration,
                            )
            except TimeoutError:
                logger.warning(
                    "Synthesis scratchpad stream timed out after %.1fs; using fallback summary",
                    timeout_seconds,
                )
                async for chunk in self._yield_fallback_chunk(state):
                    yield chunk
                synthesis_elapsed_ms = int((time.perf_counter() - synthesis_start) * 1000)
                logger.info(
                    "Synthesis (generate): fallback after timeout elapsed_ms=%d",
                    synthesis_elapsed_ms,
                )
                return
        else:
            with direct_llm_token_call_scope():
                async for chunk in self.llm.astream(messages, config=graph_config):
                    yield tag_messages_stream_chunk_for_goal_completion(
                        ((), "messages", (chunk, {})),
                        thread_id=state.thread_id,
                        iteration=state.iteration,
                    )

        synthesis_elapsed_ms = int((time.perf_counter() - synthesis_start) * 1000)
        logger.info(
            "Synthesis (generate): completed elapsed_ms=%d",
            synthesis_elapsed_ms,
        )

    def _resolve_classification(
        self,
        goal: str,
        state: LoopState,
    ) -> tuple[ScenarioClassification, bool]:
        """Run heuristic fast-path; return classification and scratchpad flag.

        Returns:
            Tuple of (classification, scratchpad_mode). When the heuristic is
            conclusive, scratchpad_mode is False and the classification drives
            the standard message-build path. When the heuristic returns None,
            a minimal fallback classification is returned with scratchpad_mode=True
            so the model self-classifies inside `<analysis>` tags.
        """
        intent_type = "agentic"
        execution_summary = _extract_execution_summary(state)

        scenario_rules = None
        if self._soothe_config is not None:
            scenario_rules = getattr(
                getattr(getattr(self._soothe_config, "agent", None), "loop", None),
                "rules",
                None,
            )
            if scenario_rules is not None:
                scenario_rules = scenario_rules.scenario

        heuristic = _heuristic_classify(
            goal,
            intent_type,
            execution_summary,
            scenario_rules=scenario_rules,
        )
        if heuristic is not None:
            logger.info(
                "Synthesis heuristic: scenario=%s steps=%d",
                heuristic.scenario,
                execution_summary["total_steps"],
            )
            return heuristic, False

        logger.info(
            "Synthesis heuristic inconclusive (steps=%d); using scratchpad mode",
            execution_summary["total_steps"],
        )
        fallback = ScenarioClassification(
            scenario="custom",
            sections=[],
            contextual_focus=[f"Summarize key findings for: {goal[:120]}"],
            evidence_emphasis=(
                "Group evidence by concern or outcome in bullets/tables; "
                "do not replay turns chronologically"
            ),
        )
        return fallback, True

    def _build_scratchpad_messages(
        self,
        goal: str,
        state: LoopState,
        classification: ScenarioClassification,
        max_total: int,
        ledger_cfg: Any,
        agent_instructions_max_chars: int,
    ) -> list:
        """Build messages with scratchpad-mode system prompt for self-classification.

        Renders the system prompt via `render_synthesis_system_prompt` with
        `scratchpad_mode=True`, then delegates to `build_synthesis_messages`
        to attach the projected execute ledger and TASK human trigger.
        """
        from langchain_core.messages import SystemMessage

        from soothe.sloop.engine.completion.synthesis_projection import normalize_user_query

        system_text = render_synthesis_system_prompt(
            classification,
            user_goal=normalize_user_query(goal),
            workspace=state.workspace,
            agent_instructions_max_chars=agent_instructions_max_chars,
            response_language=getattr(state, "response_language", None),
            scratchpad_mode=True,
        )

        base_messages = build_synthesis_messages(
            state,
            classification,
            user_query=goal,
            max_chars=max_total,
            ledger_cfg=ledger_cfg,
            agent_instructions_max_chars=agent_instructions_max_chars,
        )
        if base_messages and isinstance(base_messages[0], SystemMessage):
            base_messages[0] = SystemMessage(content=system_text)
        else:
            base_messages.insert(0, SystemMessage(content=system_text))
        return base_messages

    def _build_graph_config(self, state: LoopState) -> dict[str, Any]:
        """Build LangGraph config with isolated checkpoint thread and Langfuse wiring."""
        checkpoint_thread_id = synthesis_thread_id(state.thread_id)
        configurable: dict[str, Any] = {
            "thread_id": checkpoint_thread_id,
            SOOTHE_GOAL_SYNTHESIS_CONFIG_KEY: True,
        }
        if state.workspace:
            configurable["workspace"] = state.workspace
        logger.info(
            "Synthesis isolated checkpoint thread=%s parent_thread=%s",
            checkpoint_thread_id,
            state.thread_id,
        )

        graph_config: dict[str, Any] = {"configurable": configurable}
        parent_runnable_config: dict[str, Any] | None = None
        try:
            from langgraph.config import get_config as _lg_get_config

            parent_runnable_config = _lg_get_config()
        except RuntimeError:
            parent_runnable_config = None

        if self._soothe_config is not None:
            from soothe.utils.observability.langfuse import finalize_langfuse_run_display_name

            tn = (self._soothe_config.observability.langfuse.trace_name or "").strip()
            run_name = finalize_langfuse_run_display_name(tn or None)
            graph_config = merge_langfuse_runnable_config(
                graph_config,
                self._soothe_config,
                session_id=state.thread_id,
                run_name=run_name,
                loop_id=self._loop_id,
                inherit_callbacks_from=parent_runnable_config,
            )

        if parent_runnable_config is not None:
            from langchain_core.runnables.config import merge_configs

            from soothe.sloop.utils.graph_config import strip_parent_checkpoint_coordinates

            graph_config = strip_parent_checkpoint_coordinates(
                merge_configs(parent_runnable_config, graph_config)
            )
        return graph_config

    def _dispatch_idle_seconds(self) -> float:
        """Return the dispatch idle timeout for scratchpad-mode synthesis streaming."""
        if self._soothe_config is None:
            return 0.0
        return max(0.0, float(self._soothe_config.agent.loop.dispatch_idle_seconds))

    def _yield_fallback_chunk(self, state: LoopState) -> AsyncGenerator:
        """Yield a fallback summary as a tagged goal-completion message chunk."""
        from langchain_core.messages import AIMessage

        plan_result = state.previous_plan
        fallback_text = generate_user_fallback_summary(state, plan_result)
        fallback_msg = AIMessage(content=fallback_text)
        yield tag_messages_stream_chunk_for_goal_completion(
            ((), "messages", (fallback_msg, {})),
            thread_id=state.thread_id,
            iteration=state.iteration,
        )

    def _synthesis_max_chars(self) -> int:
        """Return max total extracted text for system + evidence payload."""
        max_chars = _DEFAULT_SYNTHESIS_EVIDENCE_MAX
        if self._soothe_config is not None:
            cap = self._soothe_config.agent.loop.report_output.synthesis_max_chars
            if cap > 0:
                max_chars = cap
        return max_chars


def generate_user_fallback_summary(
    state: LoopState,
    plan_result: PlanResult,
) -> str:
    """Generate user-friendly fallback summary.

    NEVER leak internal evidence_summary to users.
    Generate user-friendly completion summary instead.

    Args:
        state: Loop state with step_results.
        plan_result: Plan result with full_output or next_action.

    Returns:
        User-friendly summary text.
    """
    # Use planner's full_output if available
    if plan_result.full_output:
        final_output = plan_result.full_output
        logger.info("Fallback summary: use full_output chars=%d", len(final_output))
        return final_output

    action_text = resolve_plan_action_text(plan_result)

    # Generate from step results if available
    if state.step_results:
        successful_count = sum(1 for r in state.step_results if r.success)
        total_count = len(state.step_results)
        final_output = (
            f"Completed {successful_count}/{total_count} steps successfully. {action_text}"
        )
        logger.info(
            "Fallback summary: generated from steps success=%d/%d",
            successful_count,
            total_count,
        )
        return final_output

    # No steps executed, use internal action text as summary
    final_output = action_text or "Goal achieved successfully"
    logger.info("Fallback summary: use plan action text chars=%d", len(final_output))
    return final_output
