"""PCE monitor pass-control tests (RFC-SLP-005).

Fingerprint cache, debounce, caps, ring bound, and fail-safety. The
typesafe verdict is patched so no real classifier is invoked.
"""

from __future__ import annotations

from typing import Any

from soothe.config.models import PceLoopConfig
from soothe.context.models import CoverageSignal, GoalNode, StepNode
from soothe.sloop.pce import monitor as monitor_mod
from soothe.sloop.pce.events import PceEvent
from soothe.sloop.pce.monitor import PceMonitor
from soothe.sloop.pce.typesafe_pce import PceVerdict


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


def _monitor(ce: _FakeCE, **cfg_over: Any) -> PceMonitor:
    cfg = PceLoopConfig(**cfg_over)
    monitor = PceMonitor(loop_id="loop-1", context_engine=ce, pce_config=cfg)
    # Back-to-back passes in tests: pretend the previous pass was long ago.
    monitor._last_pass_monotonic = 0.0
    return monitor


def _arm(monitor: PceMonitor) -> None:
    """Bypass the debounce so consecutive test passes run immediately."""
    monitor._last_pass_monotonic = 0.0


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
