"""Shared helpers for the /context modal (goal DAG + token usage).

Dual-mode support: detects autopilot loops (bound rail + step DAG) and
loads step/rail flow state via the daemon's CE query endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from soothe_cli.runtime.state.session_stats import format_token_count
from soothe_cli.runtime.token_usage import fetch_conversation_token_count

logger = logging.getLogger(__name__)

LoadTokenSnapshotFn = Callable[[], Awaitable["TokenUsageSnapshot"]]

__all__ = [
    "AutopilotContext",
    "LoadTokenSnapshotFn",
    "RailFlowState",
    "StepDagNode",
    "StepDagSnapshot",
    "TokenUsageSnapshot",
    "detect_autopilot_mode",
    "format_token_usage",
    "load_autopilot_context",
    "load_ce_goals",
    "load_step_dag_snapshot",
    "load_token_usage_snapshot",
    "summarize_goal_statuses",
]


@dataclass(frozen=True, slots=True)
class TokenUsageSnapshot:
    """Token usage fields shown in the context modal."""

    context_tokens: int
    approximate: bool = False
    conv_tokens: int | None = None
    model_name: str | None = None
    context_limit: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0


async def load_token_usage_snapshot(
    *,
    context_tokens: int,
    approximate: bool = False,
    loop_id: str | None,
    daemon_session: Any,
    model_name: str | None = None,
    context_limit: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> TokenUsageSnapshot:
    """Build the token snapshot shown in the context modal."""
    conv_tokens: int | None = None
    if loop_id and daemon_session is not None:
        conv_tokens = await fetch_conversation_token_count(daemon_session, loop_id)

    effective_tokens = context_tokens
    effective_approximate = approximate
    if effective_tokens <= 0 and conv_tokens:
        effective_tokens = conv_tokens
        effective_approximate = True

    return TokenUsageSnapshot(
        context_tokens=effective_tokens,
        approximate=effective_approximate,
        conv_tokens=conv_tokens,
        model_name=model_name,
        context_limit=context_limit,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _goal_viewer_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a daemon goal snapshot (or CE-shaped dict) for the DAG panel.

    Preserves `step_outcomes` and `plan_summary` so autopilot detection
    and step DAG rendering can access them without a second RPC round-trip.
    """
    goal_id = str(raw.get("id") or raw.get("goal_id") or "").strip()
    description = str(raw.get("description") or raw.get("goal_text") or "").strip()
    status = str(raw.get("status") or "unknown").strip() or "unknown"
    depends_raw = raw.get("depends_on")
    depends_on: list[str] = []
    if isinstance(depends_raw, list):
        depends_on = [str(dep) for dep in depends_raw if str(dep).strip()]
    normalized: dict[str, Any] = {
        "id": goal_id or "?",
        "goal_id": goal_id or "?",
        "description": description,
        "goal_text": description,
        "status": status,
        "depends_on": depends_on,
    }
    step_outcomes = raw.get("step_outcomes")
    if isinstance(step_outcomes, list):
        normalized["step_outcomes"] = step_outcomes
    plan_summary = raw.get("plan_summary")
    if plan_summary is not None:
        normalized["plan_summary"] = plan_summary
    return normalized


async def load_ce_goals(loop_id: str, daemon_session: Any = None) -> list[dict[str, Any]]:
    """Load goals for `loop_id` via the daemon session (loop history RPC).

    Prefers Context Engine-shaped fields when present; otherwise maps snapshots (`goal_id` / `goal_text`). Dependency edges are only
    shown when the daemon includes `depends_on`.
    """
    raw_loop_id = str(loop_id or "").strip()
    if not raw_loop_id or raw_loop_id == "unknown" or daemon_session is None:
        return []

    fetch = getattr(daemon_session, "fetch_loop_history", None)
    if not callable(fetch):
        return []

    try:
        history = await fetch(raw_loop_id)
    except Exception:
        logger.debug("Failed to load goals for loop %s", raw_loop_id, exc_info=True)
        return []

    goals_raw = getattr(history, "goals", None)
    if not isinstance(goals_raw, list):
        return []

    goals: list[dict[str, Any]] = []
    for item in goals_raw:
        if isinstance(item, dict):
            goals.append(_goal_viewer_dict(item))
    return goals


def format_token_usage(snapshot: TokenUsageSnapshot) -> str:
    """Render token usage lines for the context modal."""
    count = snapshot.context_tokens
    model_name = (snapshot.model_name or "").strip()
    context_limit = snapshot.context_limit
    suffix = "+" if snapshot.approximate else ""
    in_count = snapshot.input_tokens
    out_count = snapshot.output_tokens

    if count <= 0:
        parts: list[str] = ["No token usage yet"]
        if context_limit is not None:
            parts.append(f"{format_token_count(context_limit)} token context window")
        if model_name:
            parts.append(model_name)
        return " · ".join(parts)

    formatted = format_token_count(count)
    usage = f"{formatted}{suffix} tokens used this loop"
    if in_count > 0 or out_count > 0:
        usage += f" (↑{format_token_count(in_count)} · ↓{format_token_count(out_count)})"

    msg = f"{usage} · {model_name}" if model_name else usage

    if context_limit is not None:
        limit_str = format_token_count(context_limit)
        msg += f"\n├ Context window: {limit_str} tokens"

    conv_tokens = snapshot.conv_tokens
    if conv_tokens is not None:
        conv_str = format_token_count(conv_tokens)
        conv_unit = " tokens" if conv_tokens < 1000 else ""  # noqa: PLR2004
        msg += f"\n└ Conversation (est.): ~{conv_str}{conv_unit}"
    return msg


def summarize_goal_statuses(goals: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """Return total goal count and per-status tallies."""
    counts: dict[str, int] = {}
    for goal in goals:
        status = str(goal.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return len(goals), counts


# ── Autopilot dual-mode support ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StepDagNode:
    """One step in a goal's step DAG, normalized for display.

    Attributes:
    id: Step identifier (may be abbreviated for compact display).
    description: Truncated step description.
    status: Step status string (pending, active, completed, failed, etc.).
    dependencies: Tuple of step IDs this step depends on.
    kind: Step kind (action, eval, ask_user, etc.) when available.
    execution_summary: Brief execution record text when the step has run.
    """

    id: str
    description: str
    status: str
    dependencies: tuple[str, ...] = ()
    kind: str = ""
    execution_summary: str = ""


@dataclass(frozen=True, slots=True)
class StepDagSnapshot:
    """Step DAG snapshot for a single goal.

    Attributes:
    goal_id: Goal identifier owning this step DAG.
    goal_description: Truncated goal description.
    goal_status: Goal status string.
    steps: Normalized step nodes (may be empty pre-decomposition).
    steps_completed: Count of completed steps.
    steps_total: Total step count.
    plan_summary: Optional plan summary text when the daemon provides one.
    """

    goal_id: str
    goal_description: str
    goal_status: str
    steps: list[StepDagNode] = field(default_factory=list)
    steps_completed: int = 0
    steps_total: int = 0
    plan_summary: str | None = None


@dataclass(frozen=True, slots=True)
class RailFlowState:
    """Rail flow state derived from loop channel values.

    Attributes:
    rail_id: Bound rail identifier when a rail is active.
    current_phase: Current execution phase label when derivable.
    last_fired_rules: Recently fired rail rule identifiers.
    wave_index: Current fan-out wave index (None when not a fan-out rail).
    feedback_round: Current feedback round for find→optimize→verify rails.
    acceptance_met: Whether acceptance criteria have been met.
    """

    rail_id: str | None = None
    current_phase: str | None = None
    last_fired_rules: tuple[str, ...] = ()
    wave_index: int | None = None
    feedback_round: int | None = None
    acceptance_met: bool = False


@dataclass(frozen=True, slots=True)
class AutopilotContext:
    """Aggregated autopilot-mode context for the viewer.

    Attributes:
    step_dags: Step DAG snapshots, one per goal with steps.
    rail_flow: Rail flow state (all-None when no rail is bound).
    progress_text: Human-readable progress summary line.
    active_runner: Whether a live runner task is bound to the loop.
    """

    step_dags: list[StepDagSnapshot] = field(default_factory=list)
    rail_flow: RailFlowState = field(default_factory=RailFlowState)
    progress_text: str = ""
    active_runner: bool | None = None


def _truncate_step_description(value: str, *, max_len: int = 80) -> str:
    """Truncate step description to `max_len` chars with ellipsis."""
    normalized = (value or "").strip()
    if max_len <= 3:
        return normalized[:max_len]
    if len(normalized) <= max_len:
        return normalized
    return f"{normalized[: max_len - 3]}..."


def _normalize_step_outcomes(
    raw_outcomes: list[dict[str, Any]] | None,
) -> list[StepDagNode]:
    """Normalize daemon `step_outcomes` list into display step nodes.

    Args:
    raw_outcomes: List of step outcome dicts from the goal display snapshot.

    Returns:
    List of normalized StepDagNode objects (empty when input is None/empty).
    """
    if not isinstance(raw_outcomes, list) or not raw_outcomes:
        return []
    nodes: list[StepDagNode] = []
    for item in raw_outcomes:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or item.get("step_id") or "?")
        desc = _truncate_step_description(str(item.get("description") or ""))
        status = str(item.get("status") or "unknown")
        deps_raw = item.get("dependencies") or item.get("depends_on") or []
        deps = (
            tuple(str(d) for d in deps_raw if isinstance(d, (str, int)))
            if isinstance(deps_raw, list)
            else ()
        )
        kind = str(item.get("kind") or "")
        exec_raw = item.get("execution") or item.get("execution_summary")
        exec_text = str(exec_raw) if exec_raw else ""
        if len(exec_text) > 120:
            exec_text = exec_text[:117] + "..."
        nodes.append(
            StepDagNode(
                id=step_id,
                description=desc,
                status=status,
                dependencies=deps,
                kind=kind,
                execution_summary=exec_text,
            )
        )
    return nodes


def _normalize_plan_steps(plan: Any) -> list[StepDagNode]:
    """Normalize a plan object (from execution state) into step nodes.

    The daemon's `fetch_execution_state` returns a `plan` that may be a
    list of step dicts, a dict with a `steps` key, or a structured plan
    object. This handles all shapes defensively.

    Args:
    plan: Raw plan value from execution state RPC.

    Returns:
    List of normalized StepDagNode objects (empty when plan is None/empty).
    """
    if plan is None:
        return []
    steps_raw: list[dict[str, Any]] = []
    if isinstance(plan, list):
        steps_raw = [s for s in plan if isinstance(s, dict)]
    elif isinstance(plan, dict):
        inner = plan.get("steps") or plan.get("plan_steps") or plan.get("nodes")
        if isinstance(inner, list):
            steps_raw = [s for s in inner if isinstance(s, dict)]
        elif isinstance(plan, dict) and plan.get("id"):
            steps_raw = [plan]
    return _normalize_step_outcomes(steps_raw)


def detect_autopilot_mode(
    goals: list[dict[str, Any]],
    execution_state: Any = None,
) -> bool:
    """Detect whether the current loop is autopilot-mode.

    A loop is autopilot-mode when at least one goal has step outcomes
    (a decomposed step DAG) or a plan summary, or when the execution
    state carries a non-empty plan with step data.

    Args:
    goals: Normalized goal dicts from `load_ce_goals`.
    execution_state: Optional SimpleNamespace from `fetch_execution_state`.

    Returns:
    True when the loop shows autopilot-mode characteristics.
    """
    for goal in goals:
        step_outcomes = goal.get("step_outcomes")
        if isinstance(step_outcomes, list) and step_outcomes:
            return True
        plan_summary = goal.get("plan_summary")
        if plan_summary is not None and str(plan_summary).strip():
            return True
    if execution_state is not None:
        plan = getattr(execution_state, "plan", None)
        if plan is not None:
            if isinstance(plan, list) and plan:
                return True
            if isinstance(plan, dict):
                steps = plan.get("steps")
                if isinstance(steps, dict) and steps.get("nodes"):
                    return True
                if isinstance(steps, list) and steps:
                    return True
                if plan.get("id"):
                    return True
    return False


def _build_step_dag_from_goal(goal: dict[str, Any]) -> StepDagSnapshot:
    """Build a step DAG snapshot from a single goal dict.

    Reads `step_outcomes` and `plan_summary` from the normalized goal.
    Returns a zero-step snapshot when the goal has not been decomposed.

    Args:
    goal: Normalized goal dict from `load_ce_goals`.

    Returns:
    StepDagSnapshot for this goal (steps may be empty pre-decomposition).
    """
    goal_id = str(goal.get("goal_id") or goal.get("id") or "?")
    goal_desc = _truncate_step_description(
        str(goal.get("goal_text") or goal.get("description") or "")
    )
    goal_status = str(goal.get("status") or "unknown")

    step_outcomes_raw = goal.get("step_outcomes")
    steps = _normalize_step_outcomes(
        step_outcomes_raw if isinstance(step_outcomes_raw, list) else None
    )

    plan_summary_raw = goal.get("plan_summary")
    plan_summary = str(plan_summary_raw).strip() if plan_summary_raw else None

    steps_total = len(steps)
    steps_completed = sum(1 for s in steps if s.status == "completed")

    return StepDagSnapshot(
        goal_id=goal_id,
        goal_description=goal_desc,
        goal_status=goal_status,
        steps=steps,
        steps_completed=steps_completed,
        steps_total=steps_total,
        plan_summary=plan_summary,
    )


def load_step_dag_snapshot(
    goals: list[dict[str, Any]],
    execution_state: Any = None,
) -> list[StepDagSnapshot]:
    """Build step DAG snapshots from goal history and execution state.

    Combines frozen goal display snapshots (step_outcomes / plan_summary)
    with the live execution state plan for the currently running goal.

    Args:
    goals: Raw goal dicts from the daemon loop history response.
    execution_state: Optional SimpleNamespace from `fetch_execution_state`.

    Returns:
    List of StepDagSnapshot, one per goal that has step data. May be
    empty when no goals have steps (pre-decomposition or non-autopilot).
    """
    snapshots: list[StepDagSnapshot] = []
    live_steps = (
        _normalize_plan_steps(getattr(execution_state, "plan", None)) if execution_state else []
    )

    for goal in goals:
        snapshot = _build_step_dag_from_goal(goal)

        if (
            snapshot.steps_total == 0
            and live_steps
            and snapshot.goal_status in ("active", "running")
        ):
            snapshot = StepDagSnapshot(
                goal_id=snapshot.goal_id,
                goal_description=snapshot.goal_description,
                goal_status=snapshot.goal_status,
                steps=live_steps,
                steps_completed=sum(1 for s in live_steps if s.status == "completed"),
                steps_total=len(live_steps),
                plan_summary=snapshot.plan_summary,
            )

        if snapshot.steps_total == 0 and snapshot.plan_summary is None:
            continue

        snapshots.append(snapshot)
    return snapshots


def _extract_rail_flow_from_state(values: dict[str, Any]) -> RailFlowState:
    """Derive rail flow state from StrangeLoop channel values.

    Args:
    values: Full channel-value dict from `aget_loop_state`.

    Returns:
    RailFlowState with rail_id, phase, fired rules, and wave metadata.
    """
    rail_id = values.get("rail_id") or values.get("bound_rail_id")
    rail_id_str = str(rail_id) if rail_id else None

    phase = values.get("current_phase") or values.get("phase")
    phase_str = str(phase).strip() if phase else None

    fired_raw = values.get("last_fired_rules") or values.get("fired_rules")
    fired: tuple[str, ...] = ()
    if isinstance(fired_raw, list):
        fired = tuple(
            str(r.get("rule_id") or r) if isinstance(r, dict) else str(r)
            for r in fired_raw[-6:]
            if str(r).strip()
        )

    wave_index = values.get("wave_index")
    wave_int = int(wave_index) if isinstance(wave_index, (int, float)) and wave_index >= 0 else None

    feedback_raw = values.get("feedback_round")
    feedback_int = (
        int(feedback_raw) if isinstance(feedback_raw, (int, float)) and feedback_raw >= 0 else None
    )

    acceptance = values.get("acceptance_met")
    acceptance_bool = bool(acceptance) if isinstance(acceptance, bool) else False

    return RailFlowState(
        rail_id=rail_id_str,
        current_phase=phase_str,
        last_fired_rules=fired,
        wave_index=wave_int,
        feedback_round=feedback_int,
        acceptance_met=acceptance_bool,
    )


async def _fetch_execution_state_safe(loop_id: str, daemon_session: Any) -> Any:
    """Fetch execution state, returning None on failure.

    Args:
    loop_id: Loop ID.
    daemon_session: Daemon session with `fetch_execution_state`.

    Returns:
    Execution state SimpleNamespace or None.
    """
    if daemon_session is None:
        return None
    fetch = getattr(daemon_session, "fetch_execution_state", None)
    if not callable(fetch):
        return None
    try:
        return await fetch(loop_id)
    except Exception:
        logger.debug("fetch_execution_state failed for %s", loop_id, exc_info=True)
        return None


async def _load_rail_flow(loop_id: str, daemon_session: Any) -> RailFlowState:
    """Load rail flow state from the loop's StrangeLoop channels.

    Falls back to an empty `RailFlowState` when the daemon does not
    expose loop state or the rail is unbound.

    Args:
    loop_id: Loop ID.
    daemon_session: Daemon session with `aget_loop_state`.

    Returns:
    Rail flow state (possibly with None fields).
    """
    if daemon_session is None:
        return RailFlowState()
    fetch = getattr(daemon_session, "aget_loop_state", None)
    if not callable(fetch):
        return RailFlowState()
    try:
        state = await fetch(loop_id)
    except Exception:
        logger.debug("aget_loop_state failed for %s", loop_id, exc_info=True)
        return RailFlowState()
    values = getattr(state, "values", None)
    if not isinstance(values, dict):
        return RailFlowState()
    return _extract_rail_flow_from_state(values)


def _build_progress_text(
    step_dags: list[StepDagSnapshot],
    execution_state: Any,
    rail_flow: RailFlowState,
) -> str:
    """Build a one-line progress summary for the autopilot context.

    Args:
    step_dags: Step DAG snapshots.
    execution_state: Execution state SimpleNamespace (may be None).
    rail_flow: Rail flow state.

    Returns:
    Human-readable progress summary string.
    """
    parts: list[str] = []
    total_steps = sum(dag.steps_total for dag in step_dags)
    completed_steps = sum(dag.steps_completed for dag in step_dags)
    if total_steps > 0:
        parts.append(f"Steps: {completed_steps}/{total_steps}")
    if rail_flow.rail_id:
        parts.append(f"Rail: {rail_flow.rail_id}")
    if rail_flow.current_phase:
        parts.append(f"Phase: {rail_flow.current_phase}")
    if execution_state is not None:
        status = getattr(execution_state, "status", None)
        if status:
            parts.append(str(status))
        iteration = getattr(execution_state, "iteration", 0)
        if iteration:
            parts.append(f"Iteration: {iteration}")
    if not parts:
        return "Awaiting decomposition"
    return " · ".join(parts)


async def load_autopilot_context(
    loop_id: str,
    daemon_session: Any,
    *,
    goals: list[dict[str, Any]] | None = None,
) -> AutopilotContext | None:
    """Load the full autopilot context for the viewer.

    Combines step DAG snapshots, rail flow state, and a progress summary.
    Returns None when the loop is not autopilot-mode.

    Args:
    loop_id: Current loop ID.
    daemon_session: Active daemon session for RPC calls.
    goals: Pre-loaded goals (avoids a redundant fetch when available).

    Returns:
    Autopilot context, or None when not autopilot-mode.
    """
    if goals is None:
        goals = await load_ce_goals(loop_id, daemon_session)

    exec_state = await _fetch_execution_state_safe(loop_id, daemon_session)
    if not detect_autopilot_mode(goals, exec_state):
        return None

    step_dags = load_step_dag_snapshot(goals, exec_state)
    rail_flow = await _load_rail_flow(loop_id, daemon_session)

    total_steps = sum(d.steps_total for d in step_dags)
    completed_steps = sum(d.steps_completed for d in step_dags)
    iteration = getattr(exec_state, "iteration", 0) if exec_state else 0
    status = getattr(exec_state, "status", None) if exec_state else None
    active_runner = getattr(exec_state, "active_runner", None) if exec_state else None

    parts: list[str] = []
    if status:
        parts.append(str(status))
    if total_steps > 0:
        parts.append(f"Steps: {completed_steps}/{total_steps}")
    if iteration:
        parts.append(f"Iteration: {iteration}")
    progress_text = " · ".join(parts) if parts else "Initializing…"

    return AutopilotContext(
        step_dags=step_dags,
        rail_flow=rail_flow,
        progress_text=progress_text,
        active_runner=active_runner,
    )
