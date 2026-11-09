"""Completion heuristics for goal and step-level planning."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from soothe.config.models import CompletionRulesConfig

if TYPE_CHECKING:
    from soothe.context.models import StepNode

logger = logging.getLogger(__name__)

_DEFAULT_RULES = CompletionRulesConfig()


def _rules(completion_rules: CompletionRulesConfig | None) -> CompletionRulesConfig:
    return completion_rules or _DEFAULT_RULES


# ── Core heuristic functions ───────────────────────────────────────────


def _is_linear_chain(
    nodes: dict[str, StepNode] | Any,
    max_depth: int,
) -> bool:
    """Check whether the StepDAG is a strictly linear chain.

    Returns True when all of the following hold:
    - Single root: exactly one node with ``parent_step_id is None``.
    - No parallel branches: no node is a dependency of more than one other
      node (forward fanout <= 1).
    - Chain depth <= ``max_depth``: longest dependency chain length.
    - No decomposition children: no node has ``parent_step_id`` pointing to
      another action node in the DAG (decomposition creates non-linear
      sub-chains).

    Args:
        nodes: ``StepDAG.nodes`` dict (or a ``StepDAG`` with a ``nodes``
            attribute).
        max_depth: Maximum allowed dependency chain depth.

    Returns:
        True when the DAG is a linear chain within the depth bound.
    """
    if hasattr(nodes, "nodes"):
        nodes = nodes.nodes
    if not nodes:
        return True

    node_list = list(nodes.values())

    # No decomposition children: no node has parent_step_id pointing to
    # another node in the DAG (decomposition creates parent->child links).
    for n in node_list:
        parent_id = getattr(n, "parent_step_id", None)
        if parent_id is not None and parent_id in nodes:
            return False

    # Build dependency graph from step.dependencies.
    dep_map: dict[str, list[str]] = {cid: [] for cid in nodes}
    in_degree: dict[str, int] = {cid: 0 for cid in nodes}
    for cid, n in nodes.items():
        for dep in getattr(n, "dependencies", []) or []:
            if dep in dep_map:
                dep_map[dep].append(cid)
                in_degree[cid] += 1

    # Single root: exactly one node with no dependencies (in_degree == 0).
    roots = [cid for cid, deg in in_degree.items() if deg == 0]
    if len(roots) != 1:
        return False

    # No parallel branches: no node is a dependency of more than one other
    # node (forward fanout <= 1).
    for deps in dep_map.values():
        if len(deps) > 1:
            return False

    # Chain depth <= max_depth: longest path in the dependency graph.
    from collections import deque

    depth: dict[str, int] = {cid: 1 for cid in nodes}
    queue = deque(cid for cid, deg in in_degree.items() if deg == 0)
    max_depth_seen = 1
    while queue:
        current = queue.popleft()
        current_depth = depth[current]
        max_depth_seen = max(max_depth_seen, current_depth)
        for child in dep_map[current]:
            depth[child] = max(depth[child], current_depth + 1)
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    return max_depth_seen <= max_depth


def _ledger_direct_eligible(
    *,
    dag_nodes: dict[str, StepNode] | Any | None = None,
    chain_depth: int = 0,
    failed_steps: int,
    total_steps: int,
    last_wave_tool_call_count: int,
    completion_rules: CompletionRulesConfig | None = None,
) -> bool:
    """Declarative structural gates for `ledger_direct` (no content heuristics)."""
    rules = _rules(completion_rules)
    if failed_steps != 0 or total_steps > rules.simple_ledger_direct_max_steps:
        return False

    if dag_nodes is not None:
        if not _is_linear_chain(dag_nodes, max_depth=rules.linear_chain_max_depth):
            return False
    elif chain_depth > rules.linear_chain_max_depth:
        return False

    if rules.ledger_direct_max_tool_calls > 0 and (
        last_wave_tool_call_count > rules.ledger_direct_max_tool_calls
    ):
        logger.info(
            "LedgerDirect: tool_calls=%d > max=%d → synthesize",
            last_wave_tool_call_count,
            rules.ledger_direct_max_tool_calls,
        )
        return False

    return True


def _dag_requires_synthesis(
    *,
    plan_wave_count: int,
    failed_steps: int,
    completed_steps: int,
    chain_depth: int,
    last_wave_hit_subagent_cap: bool,
    last_execute_wave_parallel_multi_step: bool,
    current_decision_steps: list[Any] | None = None,
    completion_rules: CompletionRulesConfig | None = None,
    assessment_terminal: bool = True,
) -> bool:
    """Check whether DAG complexity warrants synthesis."""
    rules = _rules(completion_rules)
    if plan_wave_count >= 2:
        if not assessment_terminal:
            logger.debug(
                "Synthesis: replan wave count=%d but assessment not terminal → skip",
                plan_wave_count,
            )
        else:
            logger.info("Synthesis: replan detected (plans=%d) → synthesize", plan_wave_count)
            return True

    if failed_steps > 0:
        logger.info("Synthesis: failed steps (%d) → synthesize", failed_steps)
        return True

    if chain_depth >= 3:
        logger.info("Synthesis: deep chain (depth=%d) → synthesize", chain_depth)
        return True

    if last_wave_hit_subagent_cap:
        logger.info("Synthesis: subagent cap hit → synthesize")
        return True

    if last_execute_wave_parallel_multi_step:
        logger.info("Synthesis: parallel multi-step → synthesize")
        return True

    if failed_steps > 0:
        total = completed_steps + failed_steps
        success_rate = completed_steps / total if total > 0 else 0.0
        if success_rate < rules.low_success_rate_threshold:
            logger.info("Synthesis: low success rate (%.0f%%) → synthesize", success_rate * 100)
            return True

    if current_decision_steps:
        has_deps = any(
            step.dependencies and len(step.dependencies) >= rules.dag_dependency_threshold
            for step in current_decision_steps
        )
        if has_deps:
            logger.info("Synthesis: dag_dependencies → synthesize")
            return True

    logger.debug("Synthesis: simple execution → no synthesis required")
    return False


# ── Composite decision functions ───────────────────────────────────────


def determine_completion_strategy(
    *,
    plan_result_require_goal_completion: bool,
    plan_wave_count: int,
    failed_steps: int,
    total_steps: int,
    completed_steps: int,
    chain_depth: int,
    last_wave_hit_subagent_cap: bool,
    last_execute_wave_parallel_multi_step: bool,
    last_wave_tool_call_count: int = 0,
    current_decision_steps: list[Any] | None = None,
    ledger_text: str | None = None,
    dag_nodes: dict[str, Any] | Any | None = None,
    completion_rules: CompletionRulesConfig | None = None,
    assessment_terminal: bool = True,
) -> str:
    """Determine goal completion strategy from DAG + history.

    LEDGER_DIRECT is always the first candidate; SYNTHESIZE is the fallback
    when structural gates or content checks fail. The fast-finalize path
    uses DAG topology (_is_linear_chain) + typesafe report validation to
    decide whether the final step output can be passed through directly.
    """
    if plan_result_require_goal_completion:
        logger.info("Completion: require_goal_completion=True → synthesize")
        return "synthesize"

    if _dag_requires_synthesis(
        plan_wave_count=plan_wave_count,
        failed_steps=failed_steps,
        completed_steps=completed_steps,
        chain_depth=chain_depth,
        last_wave_hit_subagent_cap=last_wave_hit_subagent_cap,
        last_execute_wave_parallel_multi_step=last_execute_wave_parallel_multi_step,
        current_decision_steps=current_decision_steps,
        completion_rules=completion_rules,
        assessment_terminal=assessment_terminal,
    ):
        return "synthesize"

    if not (ledger_text or "").strip():
        logger.info("Completion: empty ledger → synthesize")
        return "synthesize"

    if _ledger_direct_eligible(
        dag_nodes=dag_nodes,
        chain_depth=chain_depth,
        failed_steps=failed_steps,
        total_steps=total_steps,
        last_wave_tool_call_count=last_wave_tool_call_count,
        completion_rules=completion_rules,
    ):
        return "ledger_direct"

    return "synthesize"
