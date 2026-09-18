"""Tests for batch resume (resume-map semantics) on the relay answer lifecycle."""

from __future__ import annotations

from typing import Any

from soothe.sloop.clarification.protocol import (
    ClarificationAnswer,
    ClarificationRequest,
    LoopStateView,
    answer_to_state,
)
from soothe.sloop.orchestrator.runtime_context import LoopPhaseScratch
from soothe.sloop.relay.channel import recorded_answers
from soothe.sloop.relay.inbox import RelayInbox
from soothe.sloop.relay.relay import LoopRelay
from soothe.sloop.relay.ticket import ResumeTicket


def _view() -> LoopStateView:
    return LoopStateView(
        goal_id="g",
        goal_description="",
        user_request="",
        iteration=0,
        intent_classification=None,
        plan_summary=None,
        recent_step_outputs=(),
        workspace_summary=None,
        active_skills=(),
        active_mcp_servers=(),
    )


def _request(iid: str) -> ClarificationRequest:
    return ClarificationRequest(
        questions=(f"Q {iid}?",),
        origin_node="execute",
        origin_interrupt_id=iid,
        loop_state=_view(),
    )


def _make_relay() -> LoopRelay:
    async def emit(_name: str, _payload: Any) -> None:
        pass

    return LoopRelay(loop_id="loop-1", emit=emit)


def _enqueue(inbox: RelayInbox, iid: str, thread_id: str | None) -> None:
    inbox.enqueue(
        _request(iid),
        resume_ticket=ResumeTicket(thread_id=thread_id, step_id="s1"),
        step_id="s1",
    )


def _answers_state(*pairs: tuple[str, ClarificationAnswer]) -> dict[str, Any]:
    return {
        "answers": [
            {"interrupt_id": iid, "answer": answer_to_state(answer)} for iid, answer in pairs
        ]
    }


def _answered(text: str = "yes") -> ClarificationAnswer:
    return ClarificationAnswer(answers=(text,), source="human")


def test_record_answers_writes_id_keyed_records() -> None:
    relay = _make_relay()
    _enqueue(relay.inbox, "i1", "t1")
    _enqueue(relay.inbox, "i2", "t1")

    update = relay.record_answers(
        [(_request("i1"), _answered("a1")), (_request("i2"), _answered("a2"))],
        scratch=LoopPhaseScratch(),
    )
    records = recorded_answers(update["relay_state"])
    assert [r["interrupt_id"] for r in records] == ["i1", "i2"]
    assert records[0]["answer"]["answers"] == ["a1"]


def test_consume_batch_takes_answered_same_thread_prefix() -> None:
    relay = _make_relay()
    _enqueue(relay.inbox, "i1", "t1")
    _enqueue(relay.inbox, "i2", "t1")
    _enqueue(relay.inbox, "i3", "t1")
    _enqueue(relay.inbox, "i4", "t2")

    state = _answers_state(
        ("i1", _answered("a1")),
        ("i2", _answered("a2")),
        # i3 unanswered — batch stops here; i4 is a different thread anyway.
        ("i4", _answered("a4")),
    )
    consumed = relay.consume_answer_batch(state)

    assert consumed is not None
    assert [req.origin_interrupt_id for req, _, _ in consumed] == ["i1", "i2"]
    assert all(ticket.thread_id == "t1" for _, _, ticket in consumed)
    # Remaining: i3 (same thread, unanswered) then i4 (other thread).
    assert [e.request.origin_interrupt_id for e in relay.inbox] == ["i3", "i4"]


def test_consume_batch_stops_at_unanswered_same_thread_entry() -> None:
    """Even when a LATER same-thread entry is answered, the unanswered middle
    entry stops the batch — FIFO prefix semantics."""
    relay = _make_relay()
    _enqueue(relay.inbox, "i1", "t1")
    _enqueue(relay.inbox, "i2", "t1")
    _enqueue(relay.inbox, "i3", "t1")

    state = _answers_state(("i1", _answered()), ("i3", _answered()))
    consumed = relay.consume_answer_batch(state)

    assert [req.origin_interrupt_id for req, _, _ in consumed] == ["i1"]
    assert [e.request.origin_interrupt_id for e in relay.inbox] == ["i2", "i3"]


def test_consume_batch_threadless_head_consumes_only_head() -> None:
    relay = _make_relay()
    _enqueue(relay.inbox, "planner-ask:ASK-01", None)
    _enqueue(relay.inbox, "i2", None)

    state = _answers_state(("planner-ask:ASK-01", _answered()), ("i2", _answered()))
    consumed = relay.consume_answer_batch(state)

    assert [req.origin_interrupt_id for req, _, _ in consumed] == ["planner-ask:ASK-01"]
    assert [e.request.origin_interrupt_id for e in relay.inbox] == ["i2"]


def test_clear_answers_empties_records() -> None:
    relay = _make_relay()
    _enqueue(relay.inbox, "i1", "t1")
    update = relay.record_answers([(_request("i1"), _answered())], scratch=LoopPhaseScratch())
    assert recorded_answers(update["relay_state"])

    cleared = relay.clear_answers(scratch=LoopPhaseScratch())
    assert recorded_answers(cleared["relay_state"]) == []
