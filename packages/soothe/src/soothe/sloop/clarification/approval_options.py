"""Shared tool-approval decision-option vocabulary (IG-LOOP-018).

One vocabulary for every surface that renders or decodes a tool-approval
decision — the relay answer token IS the card option label (case/whitespace
normalized on decode):

- `decision_options` — the card options for a pending action
  (``clarification/detector.py`` renders them into the question spec, so
  every human-decision surface — manual escalations, auto safety
  escalations, deferred tool approvals — offers the same options).
- `is_approve_answer` — approve ∪ remember tokens; consumed by the executor
  mint condition (``stations/execute/execute.py``) so any approve-family
  token (``Allow``/``Yes`` included) records a grant, matching the decision
  mapping in ``relay/outbox.py``.
- `is_remember_answer` — remember tokens; the executor mints a generalized
  `remember_record` (command prefix / parent directory) instead of the exact
  signature.

Approve-family token sets are re-exported from ``outbox`` semantics: keep the
two modules' tables in sync via the shared constants below (outbox imports
them; its decision mapping stays the single translate-to-HITL point).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from soothe.sloop.clarification.dynamic_rules import remember_record

# Canonical card label — doubles as the answer token after normalization.
APPROVE_LABEL = "Approve"
APPROVE_REMEMBER_LABEL = "Approve & remember"
REJECT_LABEL = "Reject"

# Wire label for the option description (server-rendered into the spec).
APPROVE_REMEMBER_DESCRIPTION = (
    "Approve this action and record a loop-scoped grant for the whole "
    "command prefix / parent directory — the same class of action will not "
    "ask again in this loop."
)

APPROVE_TOKENS: frozenset[str] = frozenset(
    {"approve", "approved", "allow", "yes", "y", "ok", "proceed"}
)
"""Approve-family answer tokens (decode surface for outbox/executor)."""
REMEMBER_TOKENS: frozenset[str] = frozenset(
    {"approve & remember", "approve_remember", "approve-remember", "always"}
)
"""Remember-family answer tokens — approve plus a generalized grant."""


def _normalize(answer: str) -> str:
    return (answer or "").strip().lower()


def is_approve_answer(answer: str) -> bool:
    """True for any approve-family token (plain or remember)."""
    token = _normalize(answer)
    return token in APPROVE_TOKENS or token in REMEMBER_TOKENS


def is_remember_answer(answer: str) -> bool:
    """True for remember tokens ("Approve & remember" et al.)."""
    return _normalize(answer) in REMEMBER_TOKENS


def decision_options(tool_name: str, args: Mapping[str, Any]) -> list[dict[str, str]]:
    """Card options for a pending tool action.

    The remember option is offered only when the action has a generalizable
    grant (`remember_record` returns a record); otherwise Approve/Reject as
    before.
    """
    options: list[dict[str, str]] = [{"label": APPROVE_LABEL}]
    if remember_record(tool_name, args) is not None:
        options.append(
            {
                "label": APPROVE_REMEMBER_LABEL,
                "description": APPROVE_REMEMBER_DESCRIPTION,
            }
        )
    options.append({"label": REJECT_LABEL})
    return options


__all__ = [
    "APPROVE_LABEL",
    "APPROVE_REMEMBER_DESCRIPTION",
    "APPROVE_REMEMBER_LABEL",
    "APPROVE_TOKENS",
    "REJECT_LABEL",
    "REMEMBER_TOKENS",
    "decision_options",
    "is_approve_answer",
    "is_remember_answer",
]
