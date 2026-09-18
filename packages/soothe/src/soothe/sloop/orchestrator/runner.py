"""Invoke the compiled Strange Loop graph."""

from __future__ import annotations

import logging
import traceback
from typing import Any

from soothe.events import STRANGE_LOOP_BREAKPOINT_PAUSED
from soothe.sloop.orchestrator.builder import build_strange_loop_graph
from soothe.sloop.orchestrator.checkpoint import strange_loop_configurable
from soothe.sloop.orchestrator.runtime_context import LoopRuntimeContext
from soothe.sloop.relay.snapshot import (
    snapshot_has_resumable_interrupt,
    snapshot_has_unanswered_pending,
)
from soothe.sloop.utils.plan_action_text import resolve_plan_action_text
from soothe.utils.observability.langfuse import (
    SootheLangfuse,
    loop_graph_langfuse_run_display_name,
    merge_langfuse_runnable_config,
)

logger = logging.getLogger(__name__)


def _loop_breakpoints_active(ctx: LoopRuntimeContext) -> bool:
    """True when `agent.loop.debug` static breakpoints are configured."""
    debug_cfg = getattr(getattr(ctx.strange_loop.config.agent, "loop", None), "debug", None)
    return debug_cfg is not None and debug_cfg.is_active()


def _paused_at_static_breakpoint(snapshot: Any) -> bool:
    """True when the graph stopped at a static breakpoint (not a dynamic interrupt).

    A static-breakpoint pause has pending nodes (`next`) but no interrupts —
    a dynamic `interrupt()` pause always shows interrupts, and a completed or
    hard-deferred turn has an empty `next`.
    """
    pending_nodes = getattr(snapshot, "next", None) or ()
    if not pending_nodes:
        return False
    return not snapshot_has_resumable_interrupt(snapshot)


async def _breakpoint_pending_nodes(compiled: Any, config: dict[str, Any]) -> list[str]:
    """Return the pending station names when paused at a static breakpoint."""
    snapshot = await compiled.aget_state(config)
    if not _paused_at_static_breakpoint(snapshot):
        return []
    return [str(node) for node in (getattr(snapshot, "next", None) or ())]


def _langfuse_goal_output_text(ctx: LoopRuntimeContext) -> str:
    """Best-effort final user-visible text for Langfuse trace output."""
    from soothe.sloop.engine.completion.continuation_context import ledger_goal_completion_text
    from soothe.sloop.intention.models import IntakeLabel

    completion = ledger_goal_completion_text(ctx.loop_state.loop_messages)
    if completion:
        return completion

    intent = getattr(ctx.loop_state, "intent", None)
    if intent is not None and getattr(intent, "intake_label", None) == IntakeLabel.CHITCHAT:
        chitchat_response = (getattr(intent, "chitchat_response", None) or "").strip()
        if chitchat_response:
            return chitchat_response
    pp = ctx.loop_state.previous_plan
    if pp is not None:
        if pp.full_output and str(pp.full_output).strip():
            return str(pp.full_output).strip()
        action_text = resolve_plan_action_text(pp)
        if action_text:
            return action_text
    return ""


def build_loop_graph_invoke_config(ctx: LoopRuntimeContext) -> dict[str, Any]:
    """Build RunnableConfig for `CompiledGraph.ainvoke` with Langfuse + loop metadata.

    Configurable `thread_id` is `{loop_id}__strange_loop` so CoreAgent /
    intake-only graphs (`thread_id=loop_id`) cannot orphan `await_user`
    interrupts. Langfuse session correlation uses `loop_state.thread_id`.

    Args:
        ctx: Runtime context for the current goal run.

    Returns:
        RunnableConfig dict safe to pass to `ainvoke`.
    """
    loop_id = ctx.state_manager.loop_id
    extra: dict[str, Any] = {}
    if ctx.loop_state.workspace:
        extra["workspace"] = ctx.loop_state.workspace
    configurable = strange_loop_configurable(loop_id, **extra)

    cfg = ctx.strange_loop.config
    if ctx.goal_trace is not None:
        return ctx.goal_trace.graph_invoke_config(configurable=configurable)

    base = {"configurable": configurable}
    run_name = loop_graph_langfuse_run_display_name(cfg.observability.langfuse.trace_name)
    merged = merge_langfuse_runnable_config(
        base,
        cfg,
        session_id=ctx.loop_state.thread_id,
        run_name=run_name,
        loop_id=loop_id,
    )
    out = dict(merged)
    meta = dict(out.get("metadata") or {})
    meta.setdefault("loop_id", loop_id)
    meta.setdefault("soothe_component", "strange_loop_graph")
    meta.setdefault("soothe_component_version", "strange-loop-v2")
    tags = list(meta.get("langfuse_tags") or [])
    for label in ("goal_execution_loop", "strange-loop-graph"):
        if label not in tags:
            tags.append(label)
    meta["langfuse_tags"] = tags
    out["metadata"] = meta
    return out


async def _clarification_resume_command(
    *,
    snapshot: Any,
    resume_answers: list[str],
    loop_id: str,
    ctx: LoopRuntimeContext | None = None,
) -> Any | None:
    """Build `Command(resume=…)` or orphaned-interrupt `goto` recovery.

    Delegates to `LoopRelay.build_resume_command` (the single owner of the
    StrangeLoop-level resume: live `interrupt()` resume vs orphan goto).
    Returns a LangGraph `Command`, or `None` when there is no pending
    clarification or the orphaned origin cannot be mapped to a safe resume
    station (caller falls back to normal invoke).
    """
    if ctx is None or ctx.relay is None:
        logger.warning(
            "[runner] no relay on context; cannot build clarification resume (loop=%s)",
            loop_id,
        )
        return None
    relay_state = (getattr(snapshot, "values", {}) or {}).get("relay_state")
    ctx.relay.hydrate_from_channels(relay_state)
    try:
        return await ctx.relay.build_resume_command(
            answers=resume_answers,
            snapshot=snapshot,
            relay_state=relay_state,
        )
    except Exception:
        logger.exception(
            "[runner] relay.build_resume_command failed (loop=%s); falling back to normal invocation",
            loop_id,
        )
        return None


async def invoke_strange_loop_graph(ctx: LoopRuntimeContext) -> None:
    """Run the compiled graph once until END.

    Progress is emitted through `ctx.emit`, which `StrangeLoop.run_with_progress` wires
    to an asyncio queue consumer.

    Args:
        ctx: Fully initialized runtime context including `emit`.
    """
    from langgraph.types import Command

    loop_id = ctx.state_manager.loop_id

    compiled = build_strange_loop_graph(ctx)
    config = build_loop_graph_invoke_config(ctx)

    # RFC-622: if the caller flagged this turn as a clarification answer AND
    # the persisted graph state shows a pending clarification with no answer,
    # resume the suspended ``interrupt(...)`` instead of starting a new
    # iteration. Falls back to a normal invocation when no clarification is
    # actually pending (defensive against a stale flag).
    graph_input: dict[str, Any] | Command | None = {"last_outcome": None}
    answer_text = (ctx.clarification_resume_text or "").strip()
    answer_list = ctx.clarification_resume_answers
    if answer_text or answer_list:
        try:
            snapshot = await compiled.aget_state(config)
            if snapshot_has_unanswered_pending(snapshot):
                # Prefer the per-question list when provided so the policy
                # returns answers paired 1:1 with questions instead of
                # broadcasting a single concatenated string.
                resume_answers = [str(a) for a in answer_list] if answer_list else [answer_text]
                resume_cmd = await _clarification_resume_command(
                    snapshot=snapshot,
                    resume_answers=resume_answers,
                    loop_id=loop_id,
                    ctx=ctx,
                )
                if resume_cmd is not None:
                    graph_input = resume_cmd
                else:
                    logger.warning(
                        "[runner] clarification resume aborted (unsafe origin); "
                        "falling back to normal invocation (loop=%s)",
                        loop_id,
                    )
            else:
                logger.warning(
                    "[runner] clarification_answer flag set but no pending clarification "
                    "in state (loop=%s); falling back to normal invocation",
                    loop_id,
                )
        except Exception:
            logger.exception(
                "[runner] failed to read graph state for clarification resume (loop=%s); "
                "falling back to normal invocation",
                loop_id,
            )
    elif _loop_breakpoints_active(ctx):
        # A prior turn parked at a static breakpoint: resume the paused node
        # with `None` (LangGraph breakpoint resume) instead of starting a
        # fresh iteration with new input.
        try:
            pending_nodes = await _breakpoint_pending_nodes(compiled, config)
            if pending_nodes:
                logger.info(
                    "[runner] Resuming static breakpoint pause at %s (loop=%s)",
                    pending_nodes,
                    loop_id,
                )
                graph_input = None
        except Exception:
            logger.exception(
                "[runner] failed to read graph state for breakpoint resume (loop=%s); "
                "falling back to normal invocation",
                loop_id,
            )

    logger.info(
        "[runner] Graph invoke start loop_id=%s thread_id=%s resume=%s",
        loop_id,
        ctx.loop_state.thread_id,
        isinstance(graph_input, Command),
    )
    from soothe.sloop.utils.token_usage import loop_token_accumulation_scope

    try:
        with loop_token_accumulation_scope(ctx.loop_state):
            await compiled.ainvoke(graph_input, config=config)
        logger.info("[runner] Graph invoke complete loop_id=%s", loop_id)
    except Exception as e:
        logger.error(
            "[runner] Graph invocation failed for loop=%s: %s\n%s",
            loop_id,
            e,
            traceback.format_exc(),
        )
        raise

    if _loop_breakpoints_active(ctx):
        # The turn stopped at a static breakpoint: surface the pending nodes
        # so operators know the loop is parked, not finished. The next turn
        # resumes via the breakpoint-resume branch above.
        try:
            pending_nodes = await _breakpoint_pending_nodes(compiled, config)
        except Exception:
            logger.debug("[runner] breakpoint pause check failed (loop=%s)", loop_id, exc_info=True)
            pending_nodes = []
        if pending_nodes:
            await ctx.emit(
                STRANGE_LOOP_BREAKPOINT_PAUSED,
                {"loop_id": loop_id, "pending_nodes": pending_nodes},
            )
            logger.info(
                "[runner] Paused at static breakpoint before %s (loop=%s); next turn resumes",
                pending_nodes,
                loop_id,
            )

    cfg = ctx.strange_loop.config
    if cfg.observability.langfuse.enabled and ctx.goal_trace is not None:
        trace_goal = ctx.loop_state.goal_user_submission or ctx.loop_state.goal
        SootheLangfuse(cfg).patch_goal_io(
            config,
            goal_text=trace_goal,
            output_text=_langfuse_goal_output_text(ctx),
            trace_display_name=loop_graph_langfuse_run_display_name(
                cfg.observability.langfuse.trace_name
            ),
            session_id=ctx.loop_state.thread_id,
        )
