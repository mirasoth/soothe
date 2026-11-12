"""PCE monitor pass-control tests.

Fingerprint cache, debounce, caps, ring bound, and fail-safety. The
typesafe verdict is patched so no real classifier is invoked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from soothe.config.models import PceLoopConfig
from soothe.context.models import CoverageSignal, GoalNode, StepNode
from soothe.sloop.pce import monitor as monitor_mod
from soothe.sloop.pce.events import PceEvent, lookup_monitor
from soothe.sloop.pce.monitor import PceMonitor
from soothe.sloop.pce.typesafe_pce import PceLlmVerdict, PceVerdict


class _FakeCE:
    """Minimal ContextEngine double: one active goal with steps."""

    def __init__(self) -> None:
        step = StepNode(id="S1", description="do the thing", status="active")
        self.goal = GoalNode(description="finish the work", status="active")
        self.goal.steps.nodes[step.id] = step

    def get_all_goals(self) -> list[GoalNode]:
        return [self.goal]

    def get_goal_sync(self, goal_id: str) -> GoalNode | None:
        if goal_id == self.goal.id:
            return self.goal
        return None

    def get_ledger_entries(self) -> list[tuple[Any, str | None]]:
        return []


class _VerdictStub:
    def __init__(self, verdict: PceVerdict | None = None, error: Exception | None = None):
        self.verdict = verdict
        self.error = error
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> PceVerdict | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.verdict


class _TwoGoalCE:
    """CE double with two goals; the active goal is switchable."""

    def __init__(self) -> None:
        self.goal_a = GoalNode(description="goal a", status="active")
        step_a = StepNode(id="SA", description="a work", status="active")
        self.goal_a.steps.nodes[step_a.id] = step_a
        self.goal_b = GoalNode(description="goal b", status="pending")
        step_b = StepNode(id="SB", description="b work", status="pending")
        self.goal_b.steps.nodes[step_b.id] = step_b
        self.goals = [self.goal_a, self.goal_b]

    def set_active(self, goal: GoalNode) -> None:
        for g in self.goals:
            g.status = "active" if g is goal else "pending"

    def get_all_goals(self) -> list[GoalNode]:
        return list(self.goals)

    def get_goal_sync(self, goal_id: str) -> GoalNode | None:
        return next((g for g in self.goals if g.id == goal_id), None)

    def get_ledger_entries(self) -> list[tuple[Any, str | None]]:
        return []


def _monitor(ce: _FakeCE, **cfg_over: Any) -> PceMonitor:
    cfg = PceLoopConfig(**cfg_over)
    monitor = PceMonitor(loop_id="loop-1", context_engine=ce, pce_config=cfg)
    # Back-to-back passes in tests: pretend the previous pass was long ago.
    monitor._last_pass_monotonic = float("-inf")
    return monitor


def _arm(monitor: PceMonitor) -> None:
    """Bypass the debounce so consecutive test passes run immediately."""
    monitor._last_pass_monotonic = float("-inf")


async def test_fingerprint_cache_skips_unchanged_state(monkeypatch):
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce)

    await monitor._maybe_pass([PceEvent(kind="tool_milestone", step_id="S1")])
    _arm(monitor)
    await monitor._maybe_pass([PceEvent(kind="tool_milestone", step_id="S1")])

    assert stub.calls == 1
    assert len(ce.goal.coverage_signals) == 1


async def test_milestone_counter_change_re_evaluates(monkeypatch):
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="runaway_step", confidence=0.7, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce)

    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="S1", payload={"tool_count": 25})]
    )
    _arm(monitor)
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="S1", payload={"tool_count": 50})]
    )

    assert stub.calls == 2
    assert len(ce.goal.coverage_signals) == 2


async def test_first_pass_runs_on_host_with_short_uptime(monkeypatch):
    """A freshly booted host has monotonic() < interval; pass 1 must still run."""
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monkeypatch.setattr(monitor_mod, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monitor = PceMonitor(
        loop_id="loop-1", context_engine=ce, pce_config=PceLoopConfig(min_pass_interval_s=3600)
    )

    await monitor._maybe_pass([PceEvent(kind="step_started", step_id="S1")])

    assert stub.calls == 1


async def test_debounce_absorbs_events_without_pass(monkeypatch):
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce, min_pass_interval_s=3600)

    await monitor._maybe_pass([PceEvent(kind="step_started", step_id="S1")])
    # First pass runs, records the timestamp.
    assert stub.calls == 1
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="S1", payload={"tool_count": 25})]
    )
    # Debounced — no second classifier call, but the counter is absorbed.
    assert stub.calls == 1
    assert monitor._milestone_state["S1"]["tool_count"] == 25


async def test_fingerprint_skip_does_not_advance_debounce(monkeypatch):
    """A skipped pass must not eat the debounce window for the next real pass.

    The completion-race fix: close_report_filed arrives on unchanged state
    (skipped), then the post-commit step_completed changes the DAG — that
    pass must run immediately, not be debounced by the skip.
    """
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce, min_pass_interval_s=3600)

    # Real pass: classifier runs, signal written, debounce window opens.
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="S1", payload={"tool_count": 25})]
    )
    assert stub.calls == 1

    # Simulate the real-world gap: completion events arrive minutes after
    # the last milestone pass, outside the debounce window.
    _arm(monitor)

    # Unchanged state (close report filed before the CE status flip):
    # skipped, no classifier call.
    await monitor._maybe_pass(
        [PceEvent(kind="close_report_filed", step_id="S1", payload={"requires_eval": False})]
    )
    assert stub.calls == 1

    # Changed state within the debounce window: must still run — the skip
    # above did not advance the window.
    ce.goal.steps.nodes["S1"].status = "completed"
    await monitor._maybe_pass([PceEvent(kind="step_completed", step_id="S1")])
    assert stub.calls == 2
    assert ce.goal.coverage_signals[-1].evidence.get("events") == ["step_completed"]


async def test_fingerprint_cache_is_per_goal(monkeypatch):
    """Switching goals must not invalidate another goal's cache entry.

    Goal A runs a pass, goal B runs a pass, then A resumes with an
    unchanged DAG — A's pass must hit A's cache entry and skip. A global
    cache would hold B's fingerprint and force a redundant re-evaluation.
    """
    ce = _TwoGoalCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce)

    # Pass 1: goal A active with a milestone on its step.
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="SA", payload={"tool_count": 25})]
    )
    assert stub.calls == 1
    assert ce.goal_a.coverage_signals, "signal written to goal A"

    # Pass 2: goal B active — its step milestone is scoped to B only.
    _arm(monitor)
    ce.set_active(ce.goal_b)
    ce.goal_b.steps.nodes["SB"].status = "active"
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="SB", payload={"tool_count": 10})]
    )
    assert stub.calls == 2
    assert ce.goal_b.coverage_signals, "signal written to goal B"
    assert set(monitor._last_fingerprints) == {ce.goal_a.id, ce.goal_b.id}

    # Pass 3: A resumes with its original state (B back to pending).
    # B's milestone counter must not leak into A's fingerprint.
    _arm(monitor)
    ce.set_active(ce.goal_a)
    ce.goal_b.steps.nodes["SB"].status = "pending"
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="SA", payload={"tool_count": 25})]
    )
    assert stub.calls == 2, "unchanged goal A state must hit A's cache entry"
    assert len(ce.goal_a.coverage_signals) == 1


# Boundary latency: no typesafe round trip when the LLM succeeds.
async def test_boundary_events_go_straight_to_llm(monkeypatch):
    ce = _FakeCE()
    typesafe = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", typesafe)

    llm_calls = {"n": 0}

    async def _llm_ok(fast_model, snapshot_input, **kwargs):
        llm_calls["n"] += 1
        return PceLlmVerdict(kind="goal_likely_complete", confidence=0.9, reasoning="done")

    monkeypatch.setattr(monitor_mod, "pce_llm_verdict", _llm_ok)
    monitor = _monitor(ce)

    await monitor._maybe_pass([PceEvent(kind="step_completed", step_id="S1")])

    assert llm_calls["n"] == 1
    assert typesafe.calls == 0, "boundary pass must skip the typesafe round trip"
    assert ce.goal.coverage_signals[-1].kind == "goal_likely_complete"
    assert ce.goal.coverage_signals[-1].evidence.get("source") == "llm"


async def test_boundary_llm_failure_falls_back_to_typesafe(monkeypatch):
    ce = _FakeCE()
    typesafe = _VerdictStub(PceVerdict(kind="on_track", confidence=0.8, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", typesafe)

    async def _llm_fail(fast_model, snapshot_input, **kwargs):
        return None

    monkeypatch.setattr(monitor_mod, "pce_llm_verdict", _llm_fail)
    monitor = _monitor(ce)

    await monitor._maybe_pass([PceEvent(kind="step_completed", step_id="S1")])

    assert typesafe.calls == 1
    assert ce.goal.coverage_signals[-1].evidence.get("source") == "typesafe"


async def test_settle_returns_immediately_when_idle():
    import time as _time

    monitor = _monitor(_FakeCE())
    start = _time.monotonic()
    await monitor.settle(5.0)
    assert _time.monotonic() - start < 1.0


async def test_settle_waits_for_in_flight_pass():
    import time as _time

    monitor = _monitor(_FakeCE())
    monitor._pass_in_flight = True

    async def _finish_pass():
        await asyncio.sleep(0.2)
        monitor._pass_in_flight = False

    task = asyncio.create_task(_finish_pass())
    start = _time.monotonic()
    await monitor.settle(5.0)
    elapsed = _time.monotonic() - start
    await task
    assert 0.1 < elapsed < 2.0, "settle must return once the pass finishes"


async def test_monitor_registry_lookup(monkeypatch):
    from soothe.sloop.pce.events import reset_pce_registry

    reset_pce_registry()
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce)
    monitor.start()
    try:
        assert lookup_monitor(monitor.loop_id) is monitor
    finally:
        await monitor.stop()
    assert lookup_monitor(monitor.loop_id) is None


async def test_max_passes_per_goal_cap(monkeypatch):
    ce = _FakeCE()
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce, max_passes_per_goal=1)

    await monitor._maybe_pass([PceEvent(kind="step_started", step_id="S1")])
    _arm(monitor)
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="S1", payload={"tool_count": 25})]
    )

    assert stub.calls == 1


async def test_signal_ring_bound():
    ce = _FakeCE()
    monitor = _monitor(ce, signal_ring_size=2)
    for i in range(3):
        monitor._append_signal(
            ce.goal.id,
            CoverageSignal(goal_id=ce.goal.id, kind="on_track", confidence=0.9),
        )
    assert len(ce.goal.coverage_signals) == 2


async def test_classifier_failure_writes_nothing_and_survives(monkeypatch):
    ce = _FakeCE()
    stub = _VerdictStub(error=RuntimeError("classifier down"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce)

    await monitor._maybe_pass([PceEvent(kind="step_started", step_id="S1")])

    assert stub.calls == 1
    assert ce.goal.coverage_signals == []


async def test_no_active_goal_skips_pass(monkeypatch):
    ce = _FakeCE()
    ce.goal.status = "completed"
    stub = _VerdictStub(PceVerdict(kind="on_track", confidence=0.9, source="typesafe"))
    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", stub)
    monitor = _monitor(ce)

    await monitor._maybe_pass([PceEvent(kind="step_started", step_id="S1")])

    assert stub.calls == 0


async def test_verdict_kind_conflict_escalates_to_llm(monkeypatch):
    ce = _FakeCE()
    verdicts = [
        PceVerdict(kind="on_track", confidence=0.9, source="typesafe"),
        PceVerdict(kind="runaway_step", confidence=0.8, source="typesafe"),
    ]

    async def _next_verdict(*args: Any, **kwargs: Any):
        return verdicts.pop(0) if verdicts else None

    monkeypatch.setattr(monitor_mod, "decide_pce_verdict_typesafe", _next_verdict)

    from soothe.sloop.pce.typesafe_pce import PceLlmVerdict

    escalations = {"calls": 0}

    async def _llm_ok(fast_model: Any, snapshot_input: str, **kwargs: Any):
        escalations["calls"] += 1
        return PceLlmVerdict(kind="runaway_step", confidence=0.85, reasoning="conflict")

    monkeypatch.setattr(monitor_mod, "pce_llm_verdict", _llm_ok)
    monitor = _monitor(ce)

    await monitor._maybe_pass([PceEvent(kind="tool_milestone", step_id="S1")])
    _arm(monitor)
    await monitor._maybe_pass(
        [PceEvent(kind="tool_milestone", step_id="S1", payload={"tool_count": 25})]
    )

    assert escalations["calls"] == 1
    assert ce.goal.coverage_signals[-1].kind == "runaway_step"
    assert ce.goal.coverage_signals[-1].evidence.get("source") == "llm"
