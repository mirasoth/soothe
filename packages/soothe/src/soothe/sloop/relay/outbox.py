"""Resume command builders (StrangeLoop → CoreAgent).

Single resume translator for every clarification origin. Adding a new origin
means editing this module only, not the executor capture site. The
`GraphStreamChunkReader` and `DispatchTimeoutError` stay in
`engine/execute/graph_interrupt.py` (stream mechanics, not relay mechanics).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from soothe.sloop.clarification.interrupt_kinds import (
    InterruptKind,
    classify_interrupt_payload,
)
from soothe.sloop.clarification.origins import ORIGIN_TOOL_APPROVAL

if TYPE_CHECKING:
    from soothe.sloop.clarification.protocol import (
        ClarificationAnswer,
        ClarificationRequest,
    )

# Decision tokens for mapping relay answers to HITL DecisionType. The veritas
# LLM / TUI may emit prose ("no, reject — too risky") instead of a bare token;
# word-boundary matching maps the verdict without re-judging it. Reject wins
# over edit on a tie so "reject and edit the args" stays safe.
_REJECT_TOKENS = ("reject", "no", "deny", "block", "cancel", "n", "denied", "rejecting")
_EDIT_TOKENS = ("edit", "modify", "change", "revise", "editing", "modifying")
_REJECT_RE = re.compile(r"\b(" + "|".join(_REJECT_TOKENS) + r")\b", re.IGNORECASE)
_EDIT_RE = re.compile(r"\b(" + "|".join(_EDIT_TOKENS) + r")\b", re.IGNORECASE)


def answer_to_decision(answer: str) -> str:
    """Map a tool-approval answer string to a HITL `DecisionType`.

    The relay answers with a free-form string (from veritas or the TUI). The
    deepagents middleware expects `"approve"` / `"edit"` / `"reject"`. Defaults
    to `"approve"` for unrecognized positive-ish answers and `"reject"` only
    on an explicit reject token.

    Word-boundary match so prose-shaped answers ("no, reject — too risky")
    resolve as cleanly as a bare "reject" token. Reject wins over edit on a
    tie so "reject and edit the args" stays safe.
    """
    text = (answer or "").strip()
    if not text:
        return "approve"
    if _REJECT_RE.search(text):
        return "reject"
    if _EDIT_RE.search(text):
        return "edit"
    return "approve"


def build_tool_approval_resume_payload(
    interrupt_id: str,
    *,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the resume payload for a tool-approval interrupt.

    Translates the relay's answer (approve/reject/edit per action request)
    into the `{"decisions": [...]}` shape the deepagents
    `HumanInTheLoopMiddleware` expects on `Command(resume=...)`.
    """
    return {interrupt_id: {"decisions": decisions}}


def build_auto_resume_payload(pending_interrupts: Mapping[str, Any]) -> dict[str, Any]:
    """Build a `Command(resume=...)` payload for residual non-clarification interrupts.

    `ask_user` and `tool_approval` interrupts are captured by the relay
    before this runs; they never reach `pending_interrupts`. This auto-approves
    any other interrupt type (typically deepagents middleware interrupts
    unrelated to clarification).
    """
    payload: dict[str, Any] = {}
    for iid, value in pending_interrupts.items():
        if classify_interrupt_payload(value) in (
            InterruptKind.ASK_USER,
            InterruptKind.TOOL_APPROVAL,
        ):
            continue
        payload[iid] = {"decisions": [{"type": "approve"}]}
    return payload


def build_clarification_resume_payload(
    request: ClarificationRequest,
    answer: ClarificationAnswer,
) -> dict[str, Any]:
    """Build the `Command(resume=...)` payload for a clarified interrupt.

    - `tool_approval` — map the relay's answer to a HITL `decisions` shape. One
      decision per pending action request; the decisions list length must
      match the number of hanging tool calls. When the answer has fewer
      entries, remaining slots default to the first answer (or `"approve"`).
    - otherwise (`ask_user` / execute) — deliver answers verbatim so the
      `ask_user` tool returns the Q&A and the agent continues its turn.

    Instructive rejects (audit `instructive: True`) attach the safety `reason`
    as a `message` on each reject decision so the model's `ToolMessage`
    explains why the call was blocked.
    """
    if request.origin_node == ORIGIN_TOOL_APPROVAL:
        action_requests = request.metadata.get("action_requests", [])
        n_pending = (
            len(action_requests) if isinstance(action_requests, list) and action_requests else 0
        )
        answers = list(answer.answers) if answer.answers else ["approve"]
        if n_pending == 0:
            n_pending = len(answers) if answers else 1
        audit = answer.audit if isinstance(answer.audit, dict) else {}
        instructive_reason: str | None = None
        if audit.get("instructive"):
            instructive_reason = str(audit.get("reason") or "").strip() or None
        decisions: list[dict[str, Any]] = []
        for i in range(n_pending):
            ans = answers[i] if i < len(answers) else answers[0]
            decision_type = answer_to_decision(ans)
            decision: dict[str, Any] = {"type": decision_type}
            if decision_type == "reject" and instructive_reason is not None:
                decision["message"] = instructive_reason
            decisions.append(decision)
        return build_tool_approval_resume_payload(
            request.origin_interrupt_id,
            decisions=decisions,
        )
    return {request.origin_interrupt_id: {"answers": list(answer.answers)}}


__all__ = [
    "answer_to_decision",
    "build_auto_resume_payload",
    "build_clarification_resume_payload",
    "build_tool_approval_resume_payload",
]
