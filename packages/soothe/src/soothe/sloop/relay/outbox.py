"""Resume command builders (StrangeLoop → CoreAgent).

Single resume translator for every clarification origin. Adding a new origin
means editing this module only, not the executor capture site. The
`GraphStreamChunkReader` and `DispatchTimeoutError` stay in
`engine/execute/graph_interrupt.py` (stream mechanics, not relay mechanics).
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

# Canonical decision vocabulary for tool-approval answers. The answer slot is
# a single action token chosen on the decision card (structured selector);
# the comment slot carries any prose. Only exact (whitespace/case-normalized)
# matches resolve a decision — anything else, including empty answers, maps to
# "reject" so a malformed or missing answer can never approve a gated tool
# call. Content judgment must not be re-derived from prose via keyword search.
_APPROVE_TOKENS = frozenset({"approve", "approved", "allow", "yes", "y", "ok", "proceed"})
_REJECT_TOKENS = frozenset({"reject", "rejected", "deny", "denied", "no", "n", "block", "cancel"})
_EDIT_TOKENS = frozenset({"edit", "modify", "revise", "change"})
_DECISION_BY_TOKEN: dict[str, str] = {
    **{token: "approve" for token in _APPROVE_TOKENS},
    **{token: "reject" for token in _REJECT_TOKENS},
    **{token: "edit" for token in _EDIT_TOKENS},
}


def answer_to_decision(answer: str) -> str:
    """Map a tool-approval answer token to a HITL `DecisionType`.

    The deepagents middleware expects `"approve"` / `"edit"` / `"reject"`.
    Answers are canonical action tokens from the decision card; the mapping
    is an exact match against the decision vocabulary (case/whitespace
    normalized). Unrecognized or empty answers default to `"reject"` — the
    fail-safe direction — and are logged so a misconfigured client is
    observable instead of silently approving gated tool calls.
    """
    text = (answer or "").strip().lower()
    decision = _DECISION_BY_TOKEN.get(text)
    if decision is not None:
        return decision
    if text:
        logger.warning(
            "Unrecognized tool-approval answer %r — failing safe to reject",
            answer,
        )
    return "reject"


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
      entries, remaining slots default to the first answer (missing answers
      fail safe to `"reject"` — see `answer_to_decision`).
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
        # Missing answers must fail safe (reject), never default-approve a
        # gated tool call. The decision tokenizer logs unrecognized answers.
        answers = list(answer.answers) if answer.answers else ["reject"]
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
