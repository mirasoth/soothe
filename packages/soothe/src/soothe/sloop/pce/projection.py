"""Goal-DAG snapshot and fingerprint for PCE passes (RFC-SLP-005).

The snapshot is a compact projection of the full Context Engine goal DAG
(completed, ongoing, interrupted goals plus their steps) with a ledger
digest. The fingerprint hashes the snapshot plus the monitor's latest
milestone counters so identical state never re-evaluates — the loop 7d10
lesson where a classifier ran 295 times on byte-identical input.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from soothe.utils.text import truncate_text

_GOAL_DESC_MAX_CHARS = 300
_LEDGER_DIGEST_MAX_CHARS = 2000


class PceStepView(BaseModel):
    id: str
    kind: str
    status: str
    description: str = ""
    close_requires_eval: bool = False


class PceGoalView(BaseModel):
    id: str
    status: str
    description: str = ""
    steps: list[PceStepView] = Field(default_factory=list)


class PceSnapshot(BaseModel):
    goals: list[PceGoalView] = Field(default_factory=list)
    active_goal_id: str | None = None
    ledger_digest: str = ""


def snapshot_goal_dag(context_engine: Any) -> PceSnapshot:
    """Project the CE goal DAG into a compact, fingerprintable snapshot."""
    goals: list[PceGoalView] = []
    active_goal_id: str | None = None
    try:
        all_goals = context_engine.get_all_goals()
    except Exception:  # noqa: BLE001 — snapshot failure skips the pass
        all_goals = []
    for goal in all_goals:
        steps = [
            PceStepView(
                id=node.id,
                kind=str(getattr(node, "kind", "action")),
                status=str(getattr(node, "status", "pending")),
                description=truncate_text(
                    str(getattr(node, "description", "")), limit=_GOAL_DESC_MAX_CHARS
                ),
                close_requires_eval=bool(
                    getattr(node, "close_report", None) is not None
                    and getattr(node.close_report, "requires_eval", False)
                ),
            )
            for node in getattr(goal, "steps", None).nodes.values()
        ]
        goals.append(
            PceGoalView(
                id=goal.id,
                status=str(goal.status),
                description=truncate_text(goal.description, limit=_GOAL_DESC_MAX_CHARS),
                steps=steps,
            )
        )
        if str(goal.status) == "active":
            active_goal_id = goal.id

    ledger_digest = ""
    try:
        entries = context_engine.get_ledger_entries()
        tail = entries[-8:]
        parts: list[str] = []
        for message, phase in tail:
            content = getattr(message, "content", "")
            parts.append(f"[{phase}] {truncate_text(str(content), limit=250)}")
        ledger_digest = truncate_text("\n".join(parts), limit=_LEDGER_DIGEST_MAX_CHARS)
    except Exception:  # noqa: BLE001
        ledger_digest = ""

    return PceSnapshot(goals=goals, active_goal_id=active_goal_id, ledger_digest=ledger_digest)


def snapshot_text(snapshot: PceSnapshot) -> str:
    """Render a snapshot as the verdict input text."""
    lines: list[str] = []
    for goal in snapshot.goals:
        marker = " (ACTIVE)" if goal.id == snapshot.active_goal_id else ""
        lines.append(f"GOAL {goal.id} status={goal.status}{marker}: {goal.description}")
        for step in goal.steps:
            close = " close_report_requires_eval" if step.close_requires_eval else ""
            lines.append(
                f"  - {step.id} kind={step.kind} status={step.status}{close}: {step.description}"
            )
    if snapshot.ledger_digest:
        lines.append(f"LEDGER TAIL:\n{snapshot.ledger_digest}")
    return "\n".join(lines)


def fingerprint(snapshot: PceSnapshot, milestone_state: dict[str, Any] | None = None) -> str:
    """Hash the snapshot plus latest milestone counters.

    Milestone counters (per-step tool/token/elapsed) are folded in so a
    budget milestone on an otherwise unchanged DAG still re-evaluates —
    while a repeat event on the same state does not.
    """
    data = {
        "snapshot": snapshot.model_dump(),
        "milestones": milestone_state or {},
    }
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
