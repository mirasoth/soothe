"""Tests for the typed interrupt payload classification (single owner)."""

from __future__ import annotations

from soothe.sloop.clarification.interrupt_kinds import (
    InterruptKind,
    classify_interrupt_payload,
)


def test_ask_user_payload_classified_by_declared_type() -> None:
    assert (
        classify_interrupt_payload({"type": "ask_user", "questions": ["q"]})
        is InterruptKind.ASK_USER
    )


def test_tool_approval_payload_classified_by_wire_contract() -> None:
    """The deepagents HITL payload carries no `type` key — the
    `action_requests` key is its documented wire contract."""
    assert (
        classify_interrupt_payload({"action_requests": [{"name": "edit_file"}]})
        is InterruptKind.TOOL_APPROVAL
    )


def test_clarification_policy_pause_classified() -> None:
    assert (
        classify_interrupt_payload(
            {"type": "clarification", "interrupt_id": "i1", "questions": ["q"]}
        )
        is InterruptKind.CLARIFICATION
    )


def test_unknown_mapping_is_other() -> None:
    assert classify_interrupt_payload({"foo": "bar"}) is InterruptKind.OTHER


def test_non_mapping_is_none() -> None:
    assert classify_interrupt_payload(None) is None
    assert classify_interrupt_payload("ask_user") is None
    assert classify_interrupt_payload([("type", "ask_user")]) is None


def test_declared_type_takes_priority_over_wire_contract() -> None:
    """A payload carrying both a known `type` and `action_requests` classifies
    by its declared type — soothe-owned emitters win over shape keys."""
    assert (
        classify_interrupt_payload({"type": "ask_user", "questions": ["q"], "action_requests": []})
        is InterruptKind.ASK_USER
    )
