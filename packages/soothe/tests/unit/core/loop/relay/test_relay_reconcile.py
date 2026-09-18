"""Tests for checkpoint reconciliation (the checkpoint is the source of truth)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langgraph.types import Interrupt

from soothe.sloop.clarification.protocol import ClarificationRequest, LoopStateView
from soothe.sloop.relay.inbox import RelayInbox
from soothe.sloop.relay.reconcile import reconcile_inbox_with_checkpoints
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


def _entry(iid: str, thread_id: str | None) -> Any:
    request = ClarificationRequest(
        questions=("Q?",),
        origin_node="execute",
        origin_interrupt_id=iid,
        loop_state=_view(),
    )
    return request, ResumeTicket(thread_id=thread_id, step_id="s1")


def _inbox(*specs: tuple[str, str | None]) -> RelayInbox:
    inbox = RelayInbox()
    for iid, thread_id in specs:
        request, ticket = _entry(iid, thread_id)
        inbox.enqueue(request, resume_ticket=ticket, step_id="s1", goal_id="g")
    return inbox


class _StubCoreAgent:
    """CoreAgent stand-in with per-thread pending interrupt state."""

    can_read_graph_state = True

    def __init__(self, pending_by_thread: dict[str, list[Interrupt]]) -> None:
        self._pending = pending_by_thread
        self.calls: list[str] = []

    async def aget_state(self, config: Any) -> Any:
        tid = config["configurable"]["thread_id"]
        self.calls.append(tid)
        interrupts = tuple(self._pending.get(tid, ()))
        return SimpleNamespace(interrupts=interrupts, tasks=(), values={})


async def test_stale_entry_dropped_when_interrupt_no_longer_pending() -> None:
    inbox = _inbox(("i-live", "t1"), ("i-stale", "t1"), ("i-other-thread", "t2"))
    core = _StubCoreAgent(
        {
            # t1 advanced past i-stale; only i-live is still pending.
            "t1": [Interrupt(value={"type": "ask_user", "questions": ["q"]}, id="i-live")],
            # t2 lost its interrupt entirely (empty pending → not verifiable).
            "t2": [],
        }
    )
    events: list[tuple[str, Any]] = []

    async def emit(name: str, payload: Any) -> None:
        events.append((name, payload))

    dropped = await reconcile_inbox_with_checkpoints(core, inbox, loop_id="L", emit=emit)

    assert dropped == 1
    assert [e.request.origin_interrupt_id for e in inbox] == ["i-live", "i-other-thread"]
    assert events and events[0][0] == "soothe.cognition.relay.reconciled"
    assert events[0][1]["dropped"] == 1


async def test_thread_without_pending_interrupts_is_kept() -> None:
    """A thread with NO pending interrupts is not verified — the entry may not
    be CoreAgent-backed (plan review / planner-ask); it must survive."""
    inbox = _inbox(("i1", "t1"))
    core = _StubCoreAgent({"t1": []})

    dropped = await reconcile_inbox_with_checkpoints(core, inbox, loop_id="L")

    assert dropped == 0
    assert len(inbox) == 1


async def test_lost_capture_is_alerted() -> None:
    """A clarification-kind interrupt pending on a thread but absent from the
    inbox is a lost capture — alerted, not auto-recovered."""
    inbox = _inbox(("i1", "t1"))
    core = _StubCoreAgent(
        {
            "t1": [
                Interrupt(value={"type": "ask_user", "questions": ["q"]}, id="i1"),
                Interrupt(value={"action_requests": [{"name": "delete"}]}, id="i-missing"),
            ]
        }
    )
    events: list[tuple[str, Any]] = []

    async def emit(name: str, payload: Any) -> None:
        events.append((name, payload))

    dropped = await reconcile_inbox_with_checkpoints(core, inbox, loop_id="L", emit=emit)

    assert dropped == 0
    assert len(inbox) == 1
    assert events and events[0][1] == {"loop_id": "L", "dropped": 0, "alerts": 1}


async def test_non_clarification_pending_interrupt_not_alerted() -> None:
    """Residual middleware interrupts on a thread are not inbox candidates."""
    inbox = _inbox(("i1", "t1"))
    core = _StubCoreAgent(
        {
            "t1": [
                Interrupt(value={"type": "ask_user", "questions": ["q"]}, id="i1"),
                Interrupt(value={"some_middleware": True}, id="i-mw"),
            ]
        }
    )
    events: list[tuple[str, Any]] = []

    async def emit(name: str, payload: Any) -> None:
        events.append((name, payload))

    dropped = await reconcile_inbox_with_checkpoints(core, inbox, loop_id="L", emit=emit)

    assert dropped == 0
    assert events == []


async def test_unreadable_state_backend_is_noop() -> None:
    inbox = _inbox(("i1", "t1"))

    class _NoStateAgent:
        can_read_graph_state = False

    dropped = await reconcile_inbox_with_checkpoints(_NoStateAgent(), inbox, loop_id="L")
    assert dropped == 0
    assert len(inbox) == 1


async def test_aget_state_failure_keeps_entries() -> None:
    inbox = _inbox(("i1", "t1"))

    class _FailingAgent:
        can_read_graph_state = True

        async def aget_state(self, config: Any) -> Any:
            raise RuntimeError("backend down")

    dropped = await reconcile_inbox_with_checkpoints(_FailingAgent(), inbox, loop_id="L")
    assert dropped == 0
    assert len(inbox) == 1


async def test_threadless_entries_skip_reconciliation() -> None:
    """Planner-ask entries have no CoreAgent thread — nothing to verify, no
    agent calls issued."""
    inbox = _inbox(("planner-ask:ASK-01", None))
    core = _StubCoreAgent({})

    dropped = await reconcile_inbox_with_checkpoints(core, inbox, loop_id="L")

    assert dropped == 0
    assert core.calls == []
    assert len(inbox) == 1


async def test_task_level_interrupts_are_read() -> None:
    """Interrupts surfaced per-task (not top-level) still count as pending."""

    class _TaskAgent:
        can_read_graph_state = True

        async def aget_state(self, config: Any) -> Any:
            task = SimpleNamespace(
                interrupts=(Interrupt(value={"type": "ask_user", "questions": ["q"]}, id="i1"),)
            )
            return SimpleNamespace(interrupts=(), tasks=(task,), values={})

    inbox = _inbox(("i1", "t1"), ("i-stale", "t1"))
    dropped = await reconcile_inbox_with_checkpoints(_TaskAgent(), inbox, loop_id="L")
    assert dropped == 1
    assert [e.request.origin_interrupt_id for e in inbox] == ["i1"]
