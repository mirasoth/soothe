"""PCE event queue and registry tests."""

from __future__ import annotations

from soothe.sloop.pce.events import (
    BOUNDARY_EVENT_KINDS,
    PceEvent,
    PceEventQueue,
    emit_pce_event,
    register_loop,
    reset_pce_registry,
    unregister_loop,
)


def _fill(queue: PceEventQueue, count: int) -> None:
    for i in range(count):
        queue.put(PceEvent(kind="tool_milestone", step_id=f"s{i}"))


async def test_queue_drops_oldest_on_overflow():
    queue = PceEventQueue(maxsize=4)
    _fill(queue, 6)
    events = queue.drain(limit=64)
    # The two oldest events were dropped; the newest four survive.
    assert [e.step_id for e in events] == ["s2", "s3", "s4", "s5"]


async def test_emit_routes_to_registered_loop():
    reset_pce_registry()
    queue = PceEventQueue()
    register_loop("loop-1", queue)
    emit_pce_event("loop-1", "step_started", step_id="S1", goal_id="G1")
    events = queue.drain()
    assert len(events) == 1
    assert events[0].kind == "step_started"
    assert events[0].step_id == "S1"
    # Unknown loop and unknown kind are ignored silently.
    emit_pce_event("loop-2", "step_started")
    emit_pce_event("loop-1", "not_a_kind")
    emit_pce_event(None, "step_started")
    assert queue.empty()


async def test_unregister_stops_delivery():
    reset_pce_registry()
    queue = PceEventQueue()
    register_loop("loop-1", queue)
    unregister_loop("loop-1")
    emit_pce_event("loop-1", "step_started")
    assert queue.empty()


def test_close_report_filed_is_an_escalation_boundary():
    # RFC-SLP-005: close reports are step boundaries — they must escalate.
    assert "close_report_filed" in BOUNDARY_EVENT_KINDS
    for kind in ("step_started", "step_completed", "step_failed", "decompose_committed"):
        assert kind in BOUNDARY_EVENT_KINDS


async def test_registry_reset():
    queue = PceEventQueue()
    register_loop("loop-1", queue)
    reset_pce_registry()
    emit_pce_event("loop-1", "step_started")
    assert queue.empty()
