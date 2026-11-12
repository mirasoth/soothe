"""ROOT_EVAL station: insert/skip gate for Eval steps."""

from __future__ import annotations

import logging
from typing import Any

from soothe_nano.protocols.routing import TaskComplexity

from soothe.sloop.orchestrator.node_base import (
    LoopNode,
    NodeResult,
    RouteDecision,
    _maybe_await,
)
from soothe.sloop.orchestrator.runtime_context import LoopRuntimeContext
from soothe.sloop.orchestrator.stations import ROOT_EVAL
from soothe.sloop.state.schemas import allocate_plan_id
from soothe.sloop.utils.config_keys import positive_config_int
from soothe.sloop.utils.goal_text import resolve_user_request

logger = logging.getLogger(__name__)

# Read-only interaction modes skip the coverage Eval.
_READONLY_MODES = frozenset({"plan", "ask"})


def _is_toolless_single_leaf(goal: Any) -> bool:
    """True when the goal ran as one root leaf with zero tool calls.

    Conversational turns (greetings, identity/meta questions) finish this way;
    there is nothing for a coverage audit to check.
    """
    nodes = list(goal.steps.nodes.values())
    if len(nodes) != 1 or nodes[0].parent_step_id is not None:
        return False
    execution = nodes[0].execution
    return execution is not None and not execution.tool_call_count


def _try_auto_to_manual_fallback(
    ctx: LoopRuntimeContext,
    goal: Any,
) -> NodeResult | None:
    """Reset failed steps and switch to manual clarification when possible.

    Returns a dispatch NodeResult, or None when not applicable.
    """
    policy = getattr(ctx, "clarification_policy", None)
    if policy is None:
        return None
    try:
        from soothe.sloop.clarification.auto import AutoClarificationPolicy

        if not isinstance(policy, AutoClarificationPolicy):
            return None
    except ImportError:
        return None
    if getattr(policy, "_interactive_fallback", None) is None:
        return None
    failed_steps = [node for node in goal.steps.nodes.values() if node.status == "failed"]
    if not failed_steps:
        return None
    swapped = ctx.strange_loop.set_clarification_mode("manual")
    if not swapped:
        logger.warning("[root_eval] auto→manual fallback: mode swap failed; fatal")
        return None
    for node in failed_steps:
        goal.steps.reset_failed_step(node.id)
    logger.info(
        "[root_eval] auto→manual fallback: reset %d failed step(s), routing to dispatch",
        len(failed_steps),
    )
    return NodeResult(payload={"root_eval_route": "dispatch"})


def _try_readonly_step_retry(
    ctx: LoopRuntimeContext,
    goal: Any,
) -> NodeResult | None:
    """Retry failed steps in read-only modes before finalize/plan-review.

    Returns a dispatch NodeResult if any failed step was reset, else None.
    """
    max_retries = ctx.strange_loop.config.agent.loop.max_step_retries
    if max_retries <= 0:
        return None

    failed_steps = [n for n in goal.steps.nodes.values() if n.status == "failed"]
    retried: list[Any] = []
    for n in failed_steps:
        if n.retry_count >= max_retries:
            continue
        goal.steps.reset_failed_step(n.id)
        n.retry_count += 1
        retried.append(n)
        logger.info(
            "[root_eval] read-only retry: reset %s (retry_count=%d/%d)",
            n.id,
            n.retry_count,
            max_retries,
        )
    if not retried:
        if failed_steps:
            logger.warning(
                "[root_eval] retry budget exhausted for %d failed step(s)",
                len(failed_steps),
            )
        return None
    ctx.ce.defer_save()
    return NodeResult(payload={"root_eval_route": "dispatch"})


def _dispatch_waves_exhausted(ctx: LoopRuntimeContext) -> tuple[bool, int, int]:
    """Whether DISPATCH has hit its ``max_waves`` cap for the current goal.

    Mirrors the wave check in ``DispatchNode.process`` so ROOT_EVAL can break
    the ``DISPATCH → ROOT_EVAL → DISPATCH`` spin that otherwise burns the whole
    LangGraph recursion budget on one goal when ``max_waves`` is reached while
    the action tree still has ready (non-eval) steps.

    Returns ``(exhausted, wave, max_waves)``.
    """
    dec_cfg = getattr(ctx.strange_loop.config.agent.loop, "decompose", None)
    max_waves = positive_config_int(getattr(dec_cfg, "max_waves", 10) if dec_cfg else 10, 10)
    wave = positive_config_int(getattr(ctx.loop_state, "iteration", 0), 0, minimum=0)
    return wave >= max_waves, wave, max_waves


async def _settle_pce_monitor(ctx: LoopRuntimeContext) -> None:
    """Bounded wait for in-flight PCE boundary passes.

    Step-completion events fire just before this gate runs; the boundary
    pass takes seconds (LLM). Settling lets the gate read the fresh
    completion signal instead of deciding without it. Fail-open: no
    monitor, no config, or timeout simply proceeds.
    """
    import contextlib

    from soothe.sloop.pce.events import lookup_monitor

    monitor = lookup_monitor(getattr(ctx.state_manager, "loop_id", None))
    if monitor is None:
        return
    timeout = 0.0
    try:
        timeout = float(
            getattr(ctx.strange_loop.config.agent.loop.pce, "gate_settle_timeout_s", 0) or 0
        )
    except AttributeError:
        return
    if timeout > 0:
        with contextlib.suppress(Exception):
            await monitor.settle(timeout)


def _pce_audit_signals(goal: Any) -> list[Any]:
    """Unconsumed PCE signals that force a coverage Eval."""
    return [s for s in goal.coverage_signals if s.consumed_by is None and s.requires_audit()]


def _consume_pce_signals(goal: Any, consumer: str, signals: list[Any]) -> None:
    for signal in signals:
        signal.consumed_by = consumer


def _pce_signal_lines(goal: Any, limit: int = 5) -> list[str]:
    """Latest unconsumed signals rendered as untrusted evidence lines."""
    signals = [s for s in goal.coverage_signals if s.consumed_by is None]
    return [
        f"- kind={s.kind} conf={s.confidence:.2f} source={s.source} (untrusted monitor signal)"
        for s in signals[-limit:]
    ]


def _eval_envelope(goal_text: str, nodes: list[Any]) -> str:
    rows: list[str] = []
    for node in nodes:
        close = node.close_report.model_dump() if node.close_report is not None else None
        outcome = node.execution.outcome if node.execution is not None else None
        rows.append(
            f"- {node.id} kind={node.kind} status={node.status}: {node.description}\n"
            f"  expected={node.expected_output or '(unspecified)'}\n"
            f"  close_report={close or '(none)'}\n"
            f"  outcome={outcome or '(none)'}"
        )
    history = "\n".join(rows)
    return (
        "Assess whether the ORIGINAL USER GOAL below is fully achieved. This is "
        "a coverage audit, not a work session — produce a verdict quickly.\n\n"
        "How to proceed:\n"
        "1. Read the step history and close reports below first.\n"
        "2. Run a verification command ONLY if coverage cannot be determined "
        "from the evidence shown — run the single most decisive check, not a "
        "battery of exploratory calls.\n"
        "3. If you have called 2+ tools without a verdict, stop and decide "
        "based on what you have.\n"
        "4. Do NOT implement, edit files, browse the codebase, or write "
        "reports — those are out of scope for this step.\n"
        "5. Conclude with exactly one coverage_verdict tool call: complete=true "
        "when the goal is fully achieved, or complete=false with explicit "
        "remaining_subtasks (in_scope=true, necessary_for_user_goal=true) when "
        "in-scope work remains. Never write the verdict as prose — a prose "
        "verdict is treated as unconfirmed and forces a re-audit.\n\n"
        f"ORIGINAL USER GOAL:\n{goal_text}\n\n"
        f"INTRA-GOAL STEP HISTORY:\n{history}"
    )


def _eval_envelope_with_signals(goal_text: str, nodes: list[Any], goal: Any) -> str:
    """Eval envelope plus unconsumed PCE signals as untrusted evidence."""
    envelope = _eval_envelope(goal_text, nodes)
    lines = _pce_signal_lines(goal)
    if not lines:
        return envelope
    return (
        envelope
        + "\n\nPCE COVERAGE SIGNALS (untrusted, from the loop monitor):\n"
        + "\n".join(lines)
    )


def _eval_verdict_complete(latest: Any) -> bool:
    """Return True when the latest Eval recorded a complete coverage verdict.

    Reads the structured ``coverage_verdict`` dict carried on the Eval step's
    execution outcome (queued by the coverage_verdict tool). A missing or
    incomplete verdict is NOT complete — callers must re-audit rather than
    finalize, so prose-only verdicts cannot silently close a goal.
    """
    outcome = latest.execution.outcome if latest.execution is not None else None
    if not isinstance(outcome, dict):
        return False
    verdict = outcome.get("coverage_verdict")
    return isinstance(verdict, dict) and bool(verdict.get("complete"))


async def _emit_dag_idle_rail_event(
    ctx: LoopRuntimeContext,
    goal: Any,
) -> None:
    """Emit a dag_idle RailEvent when the action tree is green and idle.

    Fires once at the coverage-ready gate so rail rules can trigger
    complete_job or review/qa transitions. Reads `ctx.rail_interpreter`;
    no-op when unbound. Handle failures are logged and swallowed so a rail
    rule error never blocks the ROOT_EVAL decision.

    Args:
        ctx: Loop runtime context carrying the rail interpreter.
        goal: CE goal whose `steps` StepDAG is coverage-ready.
    """
    rail = ctx.rail_interpreter
    goal_id = ctx.ce_goal_id
    if rail is None or not goal_id:
        return
    from soothe.rails.interpreter import RailEvent

    event = RailEvent(
        name="dag_idle",
        job_id=goal_id,
        goal_id=goal_id,
        payload={
            "completed_step_ids": sorted(goal.steps.completed_step_ids()),
            "failed_step_ids": sorted(goal.steps.failed_step_ids()),
            "decomposed_step_ids": sorted(goal.steps.decomposed_step_ids()),
            "node_count": len(goal.steps.nodes),
        },
    )
    try:
        await rail.handle(event)
    except Exception:
        logger.warning("[root_eval] RailEvent dag_idle handle failed", exc_info=True)


def _sibling_dependency_shape(children: list[Any]) -> tuple[bool, int, int]:
    """(sequential_only, independent_children, deepest_chain_len) over siblings."""
    ids = {c.id for c in children}
    sibling_deps = {
        c.id: [d for d in (c.dependencies or []) if d in ids and d != c.id] for c in children
    }
    independent = sum(1 for deps in sibling_deps.values() if not deps)

    def chain_len(node_id: str, seen: frozenset[str] = frozenset()) -> int:
        best = 0
        for dep in sibling_deps.get(node_id, []):
            if dep not in seen:
                best = max(best, chain_len(dep, seen | {node_id}))
        return 1 + best

    deepest = max((chain_len(c.id) for c in children), default=0)
    sequential_only = (
        len(children) >= 2
        and all(len(deps) <= 1 for deps in sibling_deps.values())
        and independent == 1
        and deepest == len(children)
    )
    return sequential_only, independent, deepest


async def _record_decompose_quality(ctx: LoopRuntimeContext, goal: Any) -> None:
    """Append DecomposeQuality records for terminal decomposed parents.

    Signal-only: computed from the StepDAG at action-tree green; idempotent
    per parent; ring-bounded. Never gates execution.
    """
    from soothe.context.models import DecomposeQuality

    cfg = getattr(ctx.strange_loop.config.agent.loop, "decompose", None)
    ring_size = positive_config_int(getattr(cfg, "quality_ring_size", 20), 20)
    recorded = {q.parent_step_id for q in goal.decompose_quality}
    appended = False
    for parent in goal.steps.nodes.values():
        if parent.status != "decomposed" or parent.id in recorded:
            continue
        children = [
            n
            for n in goal.steps.nodes.values()
            if n.parent_step_id == parent.id or parent.id in (n.secondary_parent_step_ids or [])
        ]
        if not children or any(c.status in ("pending", "active", "decomposed") for c in children):
            continue
        sequential_only, independent, deepest = _sibling_dependency_shape(children)
        record = DecomposeQuality(
            parent_step_id=parent.id,
            child_count=len(children),
            single_child=len(children) == 1,
            sequential_only=sequential_only,
            independent_children=independent,
            deepest_chain_len=deepest,
            child_failures=sum(1 for c in children if c.status == "failed"),
            child_retries=sum(c.retry_count for c in children),
            child_tool_calls_sum=sum(
                (c.execution.tool_call_count if c.execution else 0) for c in children
            ),
            child_duration_ms_sum=sum(
                (c.execution.duration_ms if c.execution else 0) for c in children
            ),
        )
        goal.decompose_quality.append(record)
        appended = True
        logger.info(
            "[root_eval] decompose_quality parent=%s children=%d single=%s seq_only=%s "
            "independent=%d deepest=%d",
            parent.id,
            record.child_count,
            record.single_child,
            record.sequential_only,
            record.independent_children,
            record.deepest_chain_len,
        )
        try:
            await ctx.emit("decompose_quality", record.model_dump(mode="python"))
        except Exception:
            logger.debug("[root_eval] decompose_quality event emit failed", exc_info=True)
    if appended:
        if len(goal.decompose_quality) > ring_size:
            del goal.decompose_quality[: len(goal.decompose_quality) - ring_size]
        ctx.ce.defer_save()


class RootEvalNode(LoopNode):
    """Insert a fresh Eval StepNode when coverage audit is required."""

    station = ROOT_EVAL
    call_kind = None

    async def process(
        self,
        ctx: LoopRuntimeContext,
        state: dict[str, Any],
        messages: list,
    ) -> NodeResult:
        """Evaluate root goal completion: retry, dispatch, or finalize."""
        if getattr(ctx, "interaction_mode", None) in _READONLY_MODES:
            # Retry failed steps before finalizing in read-only modes.
            if ctx.ce is not None and ctx.ce_goal_id:
                goal = await _maybe_await(ctx.ce.get_goal(ctx.ce_goal_id))
                if goal is not None:
                    retry_result = _try_readonly_step_retry(ctx, goal)
                    if retry_result is not None:
                        return retry_result
            logger.info(
                "[root_eval] %s interaction mode; skip Eval; finalize",
                ctx.interaction_mode,
            )
            return NodeResult(payload={"root_eval_route": "finalize"})
        if ctx.ce is not None and ctx.ce_goal_id:
            goal = await _maybe_await(ctx.ce.get_goal(ctx.ce_goal_id))
            if goal is not None:
                if any(n.status == "failed" for n in goal.steps.nodes.values()):
                    logger.warning("[root_eval] unresolved failed steps present")
                    fallback = _try_auto_to_manual_fallback(ctx, goal)
                    if fallback is not None:
                        return fallback
                    return NodeResult(payload={"root_eval_route": "fatal"})
                if not goal.steps.action_tree_green():
                    if goal.steps.ready_steps():
                        # When DISPATCH waves are exhausted, DISPATCH rejects
                        # non-eval ready steps (max_waves cap) and bounces back
                        # here, which sees the same ready steps and routes to
                        # DISPATCH again — an unbounded root_eval↔dispatch cycle
                        # that exhausts the LangGraph recursion limit and aborts
                        # the whole loop. When waves are exhausted, fall through
                        # to coverage Eval insertion / finalize so the goal is
                        # assessed from the work done so far instead of spinning.
                        exhausted, wave, max_waves = _dispatch_waves_exhausted(ctx)
                        if not exhausted:
                            return NodeResult(payload={"root_eval_route": "dispatch"})
                        logger.info(
                            "[root_eval] action tree not green but waves exhausted "
                            "(%d>=%d); assess coverage via Eval instead of cycling "
                            "to DISPATCH",
                            wave,
                            max_waves,
                        )
                        # Fall through to Eval insertion below.
                    else:
                        logger.warning("[root_eval] action tree not green and no ready steps")
                        return NodeResult(payload={"root_eval_route": "fatal"})

                # RFC-231 LoopRail: action tree is green and no pending action
                # steps remain. Emit dag_idle so rail rules can trigger
                # complete_job or review/qa transitions before coverage Eval.
                # Skip dag_idle when the tree isn't green (waves-exhausted fall
                # through above) so rail rules don't fire a false complete_job.
                if goal.steps.action_tree_green():
                    await _emit_dag_idle_rail_event(ctx, goal)
                    await _record_decompose_quality(ctx, goal)

                # RFC-SLP-005: let in-flight boundary passes (step
                # completion) land their signal before the gate decides.
                await _settle_pce_monitor(ctx)

                latest = goal.steps.latest_eval()
                if latest is not None:
                    if latest.status in ("pending", "active"):
                        return NodeResult(payload={"root_eval_route": "dispatch"})
                    if latest.status == "completed":
                        if _eval_verdict_complete(latest):
                            logger.info("[root_eval] latest Eval verdict=complete; finalize")
                            return NodeResult(payload={"root_eval_route": "finalize"})
                        # The Eval finished without a binding complete verdict
                        # (prose leakage, or incomplete with no continuation
                        # subtasks). Try TypeSafe (JEV) to decide whether to
                        # re-audit or finalize; fall back to unconditional
                        # re-audit when unavailable or untrusted.
                        from soothe.sloop.stations.decompose.root_eval_typesafe import (
                            decide_reaudit_vs_finalize_typesafe,
                        )

                        reaudit_decision = await decide_reaudit_vs_finalize_typesafe(
                            user_goal=(resolve_user_request(ctx.loop_state) or goal.description),
                            step_history_table=_eval_envelope_with_signals(
                                resolve_user_request(ctx.loop_state) or goal.description,
                                list(goal.steps.nodes.values()),
                                goal,
                            ),
                            soothe_config=ctx.strange_loop.config,
                        )
                        if reaudit_decision is False:
                            logger.info("[root_eval] typesafe: finalize despite missing verdict")
                            return NodeResult(payload={"root_eval_route": "finalize"})
                        logger.warning(
                            "[root_eval] latest Eval completed without complete verdict; re-audit"
                        )

                # Intake-gated Eval insertion decides only whether to run the
                # FIRST coverage Eval (no prior latest). Once an Eval has run,
                # a missing complete verdict or a decomposed latest routes
                # straight to the bounded re-audit below.
                if latest is None:
                    routing = getattr(ctx.loop_state, "routing_classification", None)
                    forced_complexity = getattr(routing, "task_complexity", None)
                    if forced_complexity == TaskComplexity.MINIMAL:
                        logger.info("[root_eval] minimal task; skip Eval; finalize")
                        return NodeResult(payload={"root_eval_route": "finalize"})

                    if forced_complexity == TaskComplexity.SIMPLE:
                        from soothe.sloop.eval.eval_decision import decide_eval_required

                        decision = await decide_eval_required(
                            fast_model=ctx.strange_loop._fast_llm,
                            user_goal=(resolve_user_request(ctx.loop_state) or goal.description),
                            step_history=list(goal.steps.nodes.values()),
                            task_complexity=forced_complexity,
                            soothe_config=ctx.strange_loop.config,
                            goal_trace=ctx.goal_trace,
                        )
                        logger.info(
                            "[root_eval] SIMPLE eval decision: should_run=%s reasoning=%s",
                            decision.should_run_eval,
                            decision.reasoning,
                        )
                        if not decision.should_run_eval:
                            return NodeResult(payload={"root_eval_route": "finalize"})
                        # should_run_eval=True → fall through to Eval insertion.

                    elif not goal.steps.eval_required():
                        if forced_complexity != TaskComplexity.COMPLEX:
                            pending_signals = _pce_audit_signals(goal)
                            if pending_signals:
                                # RFC-SLP-005: PCE signals force an audit even
                                # for single-leaf goals; consume on decision.
                                _consume_pce_signals(goal, "root_eval", pending_signals)
                                logger.info(
                                    "[root_eval] %d PCE signal(s) require audit; force Eval",
                                    len(pending_signals),
                                )
                            else:
                                logger.info("[root_eval] eval skip predicate matched; finalize")
                                return NodeResult(payload={"root_eval_route": "finalize"})
                        logger.info(
                            "[root_eval] complex goal ran as single leaf; force Eval (no decomposition)"
                        )
                        # Fall through to Eval insertion below.

                    elif _is_toolless_single_leaf(goal):
                        logger.info("[root_eval] toolless single leaf; skip Eval; finalize")
                        return NodeResult(payload={"root_eval_route": "finalize"})

                eval_cfg = getattr(ctx.strange_loop.config.agent.loop, "eval", None)
                max_rounds = positive_config_int(
                    getattr(eval_cfg, "max_eval_rounds", 10),
                    10,
                )
                eval_round = sum(1 for node in goal.steps.nodes.values() if node.kind == "eval")
                if eval_round >= max_rounds:
                    logger.warning("[root_eval] max Eval rounds reached")
                    return NodeResult(payload={"root_eval_route": "fatal"})

                from soothe.context.models import StepNode

                root = next(
                    (node for node in goal.steps.nodes.values() if node.parent_step_id is None),
                    None,
                )
                eval_node = StepNode(
                    id=f"{allocate_plan_id()}-EVAL",
                    description="Evaluate user-goal coverage",
                    full_description=_eval_envelope_with_signals(
                        resolve_user_request(ctx.loop_state) or goal.description,
                        list(goal.steps.nodes.values()),
                        goal,
                    ),
                    expected_output="Structured coverage_verdict (complete or continuation subtasks)",
                    status="pending",
                    parent_step_id=root.id if root is not None else None,
                    plan_iteration=eval_round + 1,
                    kind="eval",
                )
                await _maybe_await(ctx.ce.add_step(ctx.ce_goal_id, eval_node))
                logger.info("[root_eval] inserted Eval step %s", eval_node.id)
                return NodeResult(payload={"root_eval_route": "dispatch"})

        logger.info("[root_eval] no CE; finalize")
        return NodeResult(payload={"root_eval_route": "finalize"})

    def post(
        self,
        ctx: LoopRuntimeContext,
        state: dict[str, Any],
        result: NodeResult,
    ) -> RouteDecision:
        """Route based on root-eval decision: dispatch, finalize, or fatal."""
        payload = result.payload if isinstance(result.payload, dict) else {}
        route = str(payload.get("root_eval_route") or "finalize")
        patch = {"root_eval_route": route}
        if route == "fatal":
            patch["last_outcome"] = "fatal"
        return RouteDecision(
            kind="proceed",
            state_patch=patch,
        )


node = RootEvalNode()
