"""Typed classification of LangGraph interrupt payloads (single owner).

Every interrupt payload that crosses the relay boundary is classified here.
Soothe-owned emitters tag payloads with a `type` discriminator (`ask_user` tool,
`InteractiveClarificationPolicy`). The deepagents `HumanInTheLoopMiddleware`
emits `{"action_requests": [...]}` without a `type` key — that key is its
documented wire contract (PyPI-owned package) and is the one structural-key
rule allowed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

INTERRUPT_TYPE_ASK_USER = "ask_user"
INTERRUPT_TYPE_CLARIFICATION = "clarification"

_KEY_ACTION_REQUESTS = "action_requests"


class InterruptKind(StrEnum):
    """Closed vocabulary of interrupt payload kinds crossing the relay."""

    ASK_USER = "ask_user"
    TOOL_APPROVAL = "tool_approval"
    CLARIFICATION = "clarification"
    OTHER = "other"


def classify_interrupt_payload(value: Any) -> InterruptKind | None:
    """Classify an interrupt payload by its declared type or wire contract.

    Returns `None` for non-mapping payloads; `InterruptKind.OTHER` for mappings
    that carry no recognized contract.
    """
    if not isinstance(value, Mapping):
        return None
    declared = value.get("type")
    if declared == INTERRUPT_TYPE_ASK_USER:
        return InterruptKind.ASK_USER
    if declared == INTERRUPT_TYPE_CLARIFICATION:
        return InterruptKind.CLARIFICATION
    if _KEY_ACTION_REQUESTS in value:
        return InterruptKind.TOOL_APPROVAL
    return InterruptKind.OTHER


def is_clarification_interrupt_payload(value: Any) -> bool:
    """True when the payload is an ask_user or tool_approval interrupt.

    The two kinds the clarification relay captures; everything else
    (`CLARIFICATION`, `OTHER`, non-mappings) is not a relay candidate.
    """
    return classify_interrupt_payload(value) in (
        InterruptKind.ASK_USER,
        InterruptKind.TOOL_APPROVAL,
    )


__all__ = [
    "INTERRUPT_TYPE_ASK_USER",
    "INTERRUPT_TYPE_CLARIFICATION",
    "InterruptKind",
    "classify_interrupt_payload",
    "is_clarification_interrupt_payload",
]
