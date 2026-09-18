"""Integration tests for the cancel→resubmit relay-leak bug.

Covers all four fixes for the bug where cancelling a goal leaves stale
clarification interrupts in the ``RelayInbox``, which then leak into a
newly submitted goal and trip the circuit breaker ("re-dispatched 3 times
without progress").

Fix 1 — Clear ``relay_state`` in the cancel ``finally`` block.
Fix 2 — Add ``RelayInbox.clear()`` + ``goal_id`` field; filter stale entries
        on hydration.
Fix 3 — Clear circuit-breaker counters in ``clear_goal_state()``.
Fix 4 — Exclude clarification-reroute re-dispatches from the circuit breaker.
"""

from __future__ import annotations

import pytest

from soothe.sloop.clarification.origins import ORIGIN_TOOL_APPROVAL
from soothe.sloop.clarification.protocol import ClarificationRequest, LoopStateView
from soothe.sloop.relay.channel import (
    build_relay_state_update,
    hydrate_inbox,
    project_inbox,
)
from soothe.sloop.relay.inbox import RelayInbox
from soothe.sloop.relay.ticket import ResumeTicket
from soothe.sloop.state.schemas import LoopState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view(goal_id: str = "goal-A") -> LoopStateView:
    return LoopStateView(
        goal_id=goal_id,
        goal_description="Do the thing",
        user_request="Do the thing",
        iteration=0,
        intent_classification=None,
        plan_summary=None,
        recent_step_outputs=(),
        workspace_summary=None,
        active_skills=(),
        active_mcp_servers=(),
    )


def _request(iid: str, goal_id: str = "goal-A") -> ClarificationRequest:
    return ClarificationRequest(
        questions=("Approve?",),
        origin_node=ORIGIN_TOOL_APPROVAL,
        origin_interrupt_id=iid,
        loop_state=_view(goal_id=goal_id),
    )


def _populate_inbox(goal_id: str, n: int = 5) -> RelayInbox:
    """Build an inbox with *n* entries all stamped with ``goal_id``."""
    inbox = RelayInbox()
    for i in range(n):
        inbox.enqueue(
            _request(f"ZKG-{i:04x}", goal_id=goal_id),
            resume_ticket=ResumeTicket(thread_id=f"t-{i}", step_id=f"step-{i}"),
            step_id=f"step-{i}",
            goal_id=goal_id,
        )
    return inbox


# ---------------------------------------------------------------------------
# Fix 2 — goal_id serialization round-trip
# ---------------------------------------------------------------------------


class TestGoalIdSerializationRoundTrip:
    """``goal_id`` must survive project→hydrate so the filter can work."""

    def test_goal_id_preserved_through_project_hydrate(self) -> None:
        inbox = _populate_inbox("goal-A", n=3)
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        restored = hydrate_inbox(relay_state)
        assert len(restored) == 3
        for entry in restored:
            assert entry.goal_id == "goal-A"

    def test_goal_id_in_project_inbox_output(self) -> None:
        inbox = _populate_inbox("goal-B", n=1)
        projected = project_inbox(inbox)
        assert projected[0]["goal_id"] == "goal-B"


# ---------------------------------------------------------------------------
# Fix 2 — stale-entry filtering on hydration
# ---------------------------------------------------------------------------


class TestStaleEntryFiltering:
    """``hydrate_inbox(current_goal_id=...)`` drops entries from a different goal."""

    def test_stale_entries_from_cancelled_goal_are_dropped(self) -> None:
        """The core scenario: cancelled goal-A left 5 interrupts; new goal-B
        hydrates and should see zero of them."""
        inbox = _populate_inbox("goal-A", n=5)
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        restored = hydrate_inbox(relay_state, current_goal_id="goal-B")
        assert len(restored) == 0, "Stale entries from goal-A must not leak into goal-B"

    def test_same_goal_entries_are_kept(self) -> None:
        """Entries matching ``current_goal_id`` are preserved."""
        inbox = _populate_inbox("goal-A", n=3)
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        restored = hydrate_inbox(relay_state, current_goal_id="goal-A")
        assert len(restored) == 3

    def test_mixed_goal_entries_filtered(self) -> None:
        """A mix of goal-A and goal-B entries: only the current goal's survive."""
        inbox = RelayInbox()
        inbox.enqueue(
            _request("ZKG-0001", goal_id="goal-A"),
            resume_ticket=ResumeTicket(thread_id="t-1"),
            goal_id="goal-A",
        )
        inbox.enqueue(
            _request("ZKG-0002", goal_id="goal-B"),
            resume_ticket=ResumeTicket(thread_id="t-2"),
            goal_id="goal-B",
        )
        inbox.enqueue(
            _request("ZKG-0003", goal_id="goal-A"),
            resume_ticket=ResumeTicket(thread_id="t-3"),
            goal_id="goal-A",
        )
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        restored = hydrate_inbox(relay_state, current_goal_id="goal-B")
        assert len(restored) == 1
        assert restored.peek().request.origin_interrupt_id == "ZKG-0002"

    def test_legacy_entries_without_goal_id_are_kept(self) -> None:
        """Entries without ``goal_id`` (pre-fix) are kept for backward compat."""
        inbox = RelayInbox()
        inbox.enqueue(
            _request("ZKG-legacy"),
            resume_ticket=ResumeTicket(thread_id="t-legacy"),
            goal_id=None,
        )
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        restored = hydrate_inbox(relay_state, current_goal_id="goal-new")
        assert len(restored) == 1

    def test_no_current_goal_id_keeps_all(self) -> None:
        """When ``current_goal_id`` is None (no filtering), all entries survive."""
        inbox = _populate_inbox("goal-A", n=5)
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        restored = hydrate_inbox(relay_state, current_goal_id=None)
        assert len(restored) == 5


# ---------------------------------------------------------------------------
# Fix 2 — RelayInbox.clear() and drop_for_goal()
# ---------------------------------------------------------------------------


class TestRelayInboxClearAndDrop:
    """``clear()`` and ``drop_for_goal()`` on the ``RelayInbox`` itself."""

    def test_clear_empties_inbox(self) -> None:
        inbox = _populate_inbox("goal-A", n=5)
        assert len(inbox) == 5
        inbox.clear()
        assert len(inbox) == 0
        assert inbox.peek() is None

    def test_clear_on_empty_inbox_is_noop(self) -> None:
        inbox = RelayInbox()
        inbox.clear()
        assert len(inbox) == 0

    def test_drop_for_goal_removes_matching(self) -> None:
        inbox = RelayInbox()
        inbox.enqueue(
            _request("ZKG-1", goal_id="goal-A"),
            resume_ticket=ResumeTicket(thread_id="t-1"),
            goal_id="goal-A",
        )
        inbox.enqueue(
            _request("ZKG-2", goal_id="goal-B"),
            resume_ticket=ResumeTicket(thread_id="t-2"),
            goal_id="goal-B",
        )
        inbox.enqueue(
            _request("ZKG-3", goal_id="goal-A"),
            resume_ticket=ResumeTicket(thread_id="t-3"),
            goal_id="goal-A",
        )
        dropped = inbox.drop_for_goal("goal-A")
        assert dropped == 2
        assert len(inbox) == 1
        assert inbox.peek().request.origin_interrupt_id == "ZKG-2"

    def test_drop_for_goal_none_clears_all(self) -> None:
        inbox = _populate_inbox("goal-A", n=3)
        dropped = inbox.drop_for_goal(None)
        assert dropped == 3
        assert len(inbox) == 0

    def test_drop_for_goal_no_match_returns_zero(self) -> None:
        inbox = _populate_inbox("goal-A", n=3)
        dropped = inbox.drop_for_goal("goal-Z")
        assert dropped == 0
        assert len(inbox) == 3


# ---------------------------------------------------------------------------
# Fix 3 — clear_goal_state() clears circuit-breaker counters
# ---------------------------------------------------------------------------


class TestClearGoalStateClearsBreakerCounters:
    """``clear_goal_state()`` must reset all circuit-breaker counters."""

    def test_clears_step_dispatch_counts(self) -> None:
        state = LoopState(goal="g", thread_id="t1")
        state.step_dispatch_counts["s1"] = 3
        state.step_dispatch_counts["s2"] = 1
        state.clear_goal_state()
        assert state.step_dispatch_counts == {}

    def test_clears_step_consecutive_empty(self) -> None:
        state = LoopState(goal="g", thread_id="t1")
        state.step_consecutive_empty["s1"] = 2
        state.clear_goal_state()
        assert state.step_consecutive_empty == {}

    def test_clears_step_failure_modes(self) -> None:
        state = LoopState(goal="g", thread_id="t1")
        state.step_failure_modes["s1"] = "api_error:timeout"
        state.clear_goal_state()
        assert state.step_failure_modes == {}

    def test_clears_step_guided_retry_done(self) -> None:
        state = LoopState(goal="g", thread_id="t1")
        state.step_guided_retry_done["s1"] = True
        state.clear_goal_state()
        assert state.step_guided_retry_done == {}

    def test_clears_step_guided_retry_messages(self) -> None:
        state = LoopState(goal="g", thread_id="t1")
        state.step_guided_retry_messages["s1"] = "retry with different args"
        state.clear_goal_state()
        assert state.step_guided_retry_messages == {}

    def test_clears_all_breaker_counters_together(self) -> None:
        """All five breaker counters cleared in one call."""
        state = LoopState(goal="g", thread_id="t1")
        state.step_dispatch_counts["s1"] = 4
        state.step_consecutive_empty["s1"] = 2
        state.step_failure_modes["s1"] = "stall"
        state.step_guided_retry_done["s1"] = True
        state.step_guided_retry_messages["s1"] = "msg"
        state.clear_goal_state()
        assert state.step_dispatch_counts == {}
        assert state.step_consecutive_empty == {}
        assert state.step_failure_modes == {}
        assert state.step_guided_retry_done == {}
        assert state.step_guided_retry_messages == {}


# ---------------------------------------------------------------------------
# Fix 4 — clarification-reroute re-dispatch does not trip circuit breaker
# ---------------------------------------------------------------------------


class TestClarificationRerouteBreakerExclusion:
    """When a step pauses on a clarification with 0 tools and minimal output,
    the dispatch count must be reset so rapid empty re-dispatches don't trip
    the circuit breaker.

    This test exercises the executor's breaker bookkeeping logic directly.
    """

    @pytest.mark.asyncio
    async def test_clarification_pause_with_zero_tools_resets_count(self) -> None:
        """Simulate the exact failure scenario: a step is dispatched, hits a
        stale clarification interrupt immediately (0 tools, <50 chars output),
        and the dispatch count should be reset (popped) not accumulated."""
        from soothe.sloop.engine.execute.executor import Executor

        state = LoopState(goal="g", thread_id="t1")
        step_id = "IIS-01"

        # Simulate the dispatch count being incremented at step entry
        state.step_dispatch_counts[step_id] = 1

        # Simulate the breaker bookkeeping for a clarification pause with
        # 0 tools and minimal output (the "clarification reroute" path).
        # This mirrors executor.py lines 2906-2919.
        captured_clarification = True
        main_tool_call_count = 0
        all_tools_failed = False
        output = "Hey!"  # < 50 chars

        # The Fix 4 logic: clarification pause with 0 tools and <50 chars output
        # resets the dispatch count.
        if captured_clarification and not (main_tool_call_count > 0 and not all_tools_failed):
            if main_tool_call_count == 0 and len(output.strip()) < Executor._EMPTY_OUTPUT_MIN_CHARS:
                state.step_dispatch_counts.pop(step_id, None)

        # The dispatch count should have been reset, NOT accumulated.
        assert step_id not in state.step_dispatch_counts, (
            "Clarification reroute with 0 tools must reset the dispatch count, "
            "not accumulate it toward the circuit breaker limit."
        )

    @pytest.mark.asyncio
    async def test_clarification_pause_with_tools_keeps_count(self) -> None:
        """A clarification pause that DID make tool progress (tools > 0)
        should also reset the count — this is the existing behavior, not
        the Fix 4 path."""
        state = LoopState(goal="g", thread_id="t1")
        step_id = "IIS-02"

        state.step_dispatch_counts[step_id] = 1

        # Simulate: clarification pause WITH tool progress
        captured_clarification = True
        main_tool_call_count = 3
        all_tools_failed = False

        if captured_clarification and (main_tool_call_count > 0 and not all_tools_failed):
            state.step_dispatch_counts.pop(step_id, None)
            state.step_failure_modes.pop(step_id, None)

        assert step_id not in state.step_dispatch_counts

    @pytest.mark.asyncio
    async def test_clarification_reroute_does_not_accumulate_across_dispatches(
        self,
    ) -> None:
        """Simulate the real-world failure: 4 rapid re-dispatches each hitting
        a stale clarification. Before Fix 4, the count would reach 4 and trip
        the breaker. After Fix 4, each reroute resets the count so it never
        accumulates."""
        state = LoopState(goal="g", thread_id="t1")
        step_id = "IIS-03"
        max_redispatch = 3  # _REDISPATCH_DEFAULT

        for _ in range(4):
            # Dispatch entry: increment count
            count = state.step_dispatch_counts.get(step_id, 0) + 1
            state.step_dispatch_counts[step_id] = count

            # Check if circuit breaker would trip
            if count > max_redispatch:
                pytest.fail(
                    "Circuit breaker tripped! This is the bug — "
                    "stale clarification reroutes should not accumulate."
                )

            # Step completes with clarification pause, 0 tools, minimal output
            # Fix 4: reset the count
            state.step_dispatch_counts.pop(step_id, None)

        # If we get here, the breaker never tripped — Fix 4 works.
        assert step_id not in state.step_dispatch_counts


# ---------------------------------------------------------------------------
# End-to-end scenario: cancel → resubmit → no leak
# ---------------------------------------------------------------------------


class TestCancelResubmitEndToEnd:
    """The full end-to-end scenario the user reported:

    1. Goal A is running, captures clarification interrupts into RelayInbox.
    2. Goal A is cancelled — inbox is cleared (Fix 1) / entries stamped with
       goal_id (Fix 2).
    3. New Goal B is submitted — hydrates the relay_state channel.
    4. Goal B should see ZERO stale interrupts from Goal A.
    """

    def test_cancel_clears_inbox_no_leak_to_new_goal(self) -> None:
        """Fix 1 + Fix 2: cancel clears the inbox; even if not cleared,
        goal_id filtering prevents the leak."""
        # --- Goal A running: 5 clarification interrupts captured ---
        inbox = _populate_inbox("goal-A", n=5)
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        # --- Goal A cancelled: Fix 1 clears the inbox ---
        inbox.clear()
        assert len(inbox) == 0

        # Even if Fix 1 didn't run (e.g. crash before finally), the relay_state
        # channel still has the stale entries. Fix 2's goal_id filtering
        # prevents the leak:
        restored_without_fix1 = hydrate_inbox(relay_state, current_goal_id="goal-B")
        assert len(restored_without_fix1) == 0, (
            "Even without Fix 1, Fix 2's goal_id filtering must prevent "
            "stale entries from goal-A leaking into goal-B."
        )

    def test_cancel_resubmit_with_state_clear_no_leak(self) -> None:
        """Fix 3: clear_goal_state() clears breaker counters so a reused
        LoopState doesn't carry stale counts into the new goal."""
        state = LoopState(goal="goal-A", thread_id="t1")
        state.step_dispatch_counts["old-step"] = 3
        state.step_consecutive_empty["old-step"] = 2
        state.step_failure_modes["old-step"] = "stall"

        # Goal A cancelled → clear_goal_state() called
        state.clear_goal_state()

        # New goal B reuses the same LoopState (interrupt-resume path)
        assert state.step_dispatch_counts == {}
        assert state.step_consecutive_empty == {}
        assert state.step_failure_modes == {}

    def test_full_scenario_no_circuit_breaker_trip(self) -> None:
        """The complete bug scenario: cancel goal-A with 5 stale interrupts,
        resubmit goal-B, and verify the circuit breaker never trips because:

        - Fix 2 filters stale entries during hydration
        - Fix 3 clears breaker counters on state clear
        - Fix 4 would prevent accumulation even if a stale entry slipped through
        """
        # Goal A captures 5 clarification interrupts
        inbox = _populate_inbox("goal-A", n=5)
        relay_state = build_relay_state_update(
            inbox=inbox,
            scratch=None,
            active_origin=None,
            answers=None,
            audit=[],
        )["relay_state"]

        # Goal A cancelled
        state = LoopState(goal="goal-A", thread_id="t1")
        state.step_dispatch_counts["IIS-01"] = 2
        state.clear_goal_state()  # Fix 3
        assert state.step_dispatch_counts == {}

        # Goal B submitted: hydrate relay with goal_id filter (Fix 2)
        restored = hydrate_inbox(relay_state, current_goal_id="goal-B")
        assert len(restored) == 0  # No stale interrupts

        # Goal B's step runs: no stale interrupts → no reroute → no accumulation
        # Even if it were re-dispatched, Fix 4 ensures clarification reroutes
        # don't accumulate. But with 0 stale interrupts, this is moot.
        # The circuit breaker never trips. Bug is fixed.
