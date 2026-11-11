"""PCE monitor — per-loop parallel coverage observer (RFC-SLP-005).

Signal-only: the monitor snapshots the goal DAG, runs typesafe-first
verdicts (fingerprint-cached), escalates to a fast LLM when needed, and
appends CoverageSignal records onto the active GoalNode. It never
mutates goals/steps or interrupts execution; every failure path is
log-and-skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from soothe.context.models import CoverageSignal
from soothe.sloop.pce.events import (
    BOUNDARY_EVENT_KINDS,
    PceEvent,
    PceEventQueue,
    register_loop,
    register_monitor,
    unregister_loop,
)
from soothe.sloop.pce.projection import fingerprint, snapshot_goal_dag, snapshot_text
from soothe.sloop.pce.typesafe_pce import (
    PceVerdict,
    decide_pce_verdict_typesafe,
    pce_llm_verdict,
)

logger = logging.getLogger(__name__)

_DRAIN_LIMIT = 64


class PceMonitor:
    """Owns the event queue and the evaluation loop for one loop run."""

    def __init__(
        self,
        *,
        loop_id: str,
        context_engine: Any,
        pce_config: Any,
        soothe_config: Any = None,
        fast_model: Any = None,
    ) -> None:
        self.loop_id = loop_id
        self._ce = context_engine
        self._cfg = pce_config
        self._soothe_config = soothe_config
        self._fast_model = fast_model
        self.queue = PceEventQueue()

        # RFC-SLP-005: fingerprint cache is per-goal so switching between
        # goals neither suppresses nor invalidates another goal's passes.
        self._last_fingerprints: dict[str, str] = {}
        self._last_pass_monotonic: float = 0.0
        self._passes_per_goal: dict[str, int] = {}
        self._escalations_per_goal: dict[str, int] = {}
        self._last_kinds_per_goal: dict[str, str] = {}
        # Latest milestone counters per step (folded into the fingerprint).
        self._milestone_state: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._pass_in_flight = False

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> asyncio.Task:
        register_loop(self.loop_id, self.queue)
        register_monitor(self.loop_id, self)
        self._task = asyncio.create_task(self._run(), name=f"loop-pce:{self.loop_id}")
        logger.info("[pce] monitor started (loop=%s)", self.loop_id)
        return self._task

    async def stop(self) -> None:
        unregister_loop(self.loop_id)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("[pce] monitor stopped (loop=%s)", self.loop_id)

    async def settle(self, timeout_s: float) -> None:
        """Wait for pending events and any in-flight pass (bounded, fail-open).

        The root_eval gate calls this so freshly-emitted boundary events
        (step completion) produce their signal before the gate reads it.
        """
        if timeout_s <= 0:
            return
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if not self._pass_in_flight and self.queue.empty():
                return
            await asyncio.sleep(0.1)

    # ── main loop ───────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                event = await asyncio.wait_for(
                    self.queue.get(), timeout=float(self._cfg.elapsed_milestone_s)
                )
            except TimeoutError:
                event = PceEvent(kind="elapsed_milestone")
            events = [event, *self.queue.drain(limit=_DRAIN_LIMIT)]
            try:
                await self._maybe_pass(events)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — monitor never blocks the loop
                logger.warning("[pce] pass failed (loop=%s)", self.loop_id, exc_info=True)

    async def _maybe_pass(self, events: list[PceEvent]) -> None:
        self._pass_in_flight = True
        try:
            await self._maybe_pass_inner(events)
        finally:
            self._pass_in_flight = False

    async def _maybe_pass_inner(self, events: list[PceEvent]) -> None:
        now = time.monotonic()
        if now - self._last_pass_monotonic < float(self._cfg.min_pass_interval_s):
            # Debounced: absorb milestone counters so they are not lost.
            self._absorb_milestones(events)
            return
        self._absorb_milestones(events)

        snapshot = snapshot_goal_dag(self._ce)
        goal = self._active_goal(snapshot)
        if goal is None:
            return
        if self._passes_per_goal.get(goal.id, 0) >= int(self._cfg.max_passes_per_goal):
            return

        state_fp = fingerprint(snapshot, self._goal_milestone_state(goal))
        if state_fp == self._last_fingerprints.get(goal.id):
            # A skipped pass costs no verdict call, so it must not advance
            # the debounce window — otherwise a follow-up event carrying the
            # real state change (e.g. the post-commit step_completed) would
            # be swallowed inside the interval.
            logger.debug("[pce] pass skipped: unchanged fingerprint (loop=%s)", self.loop_id)
            return

        boundary = any(e.kind in BOUNDARY_EVENT_KINDS for e in events)
        verdict: PceVerdict | None = None
        if boundary:
            # Boundary latency: the gate is about to read signals, so go
            # straight to the LLM; fall back to typesafe when it fails.
            verdict = await self._escalate(goal, snapshot)
        if verdict is None:
            try:
                verdict = await decide_pce_verdict_typesafe(
                    snapshot_text(snapshot), soothe_config=self._soothe_config
                )
            except Exception:  # noqa: BLE001 — classifier failure skips the pass
                logger.warning("[pce] typesafe verdict failed", exc_info=True)
                self._last_fingerprints[goal.id] = state_fp
                self._last_pass_monotonic = now
                return
        if verdict is not None and not boundary:
            last_kind = self._last_kinds_per_goal.get(goal.id)
            if last_kind is not None and last_kind != verdict.kind:
                verdict = await self._escalate(goal, snapshot) or verdict
        elif verdict is None:
            verdict = await self._escalate(goal, snapshot)
        if verdict is None:
            # No usable verdict — record nothing, do not block.
            self._last_fingerprints[goal.id] = state_fp
            self._last_pass_monotonic = now
            return

        signal = CoverageSignal(
            goal_id=goal.id,
            source="pce",
            kind=verdict.kind,
            confidence=verdict.confidence,
            evidence={
                "dag_fingerprint": state_fp,
                "source": verdict.source,
                "reasoning": verdict.reasoning,
                "events": [e.kind for e in events[:8]],
            },
        )
        self._append_signal(goal.id, signal)
        self._last_fingerprints[goal.id] = state_fp
        self._last_pass_monotonic = now
        self._last_kinds_per_goal[goal.id] = verdict.kind
        self._passes_per_goal[goal.id] = self._passes_per_goal.get(goal.id, 0) + 1
        logger.info(
            "[pce] signal goal=%s kind=%s conf=%.2f source=%s",
            goal.id,
            verdict.kind,
            verdict.confidence,
            verdict.source,
        )

    # ── helpers ─────────────────────────────────────────────────────

    # LLM escalation under the per-goal cap; None when unavailable.
    async def _escalate(self, goal: Any, snapshot: Any) -> PceVerdict | None:
        if self._escalations_per_goal.get(goal.id, 0) >= int(
            self._cfg.max_llm_escalations_per_goal
        ):
            return None
        try:
            llm = await pce_llm_verdict(
                self._fast_model,
                snapshot_text(snapshot),
                soothe_config=self._soothe_config,
            )
        except Exception:  # noqa: BLE001 — escalation failure keeps typesafe
            logger.warning("[pce] LLM escalation failed", exc_info=True)
            return None
        if llm is None:
            return None
        self._escalations_per_goal[goal.id] = self._escalations_per_goal.get(goal.id, 0) + 1
        return PceVerdict(
            kind=llm.kind,
            confidence=float(llm.confidence),
            source="llm",
            reasoning=llm.reasoning,
        )

    # Milestone counters scoped to this goal's steps.
    def _goal_milestone_state(self, goal: Any) -> dict[str, Any]:
        try:
            step_ids = set(goal.steps.nodes.keys())
        except AttributeError:
            return dict(self._milestone_state)
        return {sid: counters for sid, counters in self._milestone_state.items() if sid in step_ids}

    def _absorb_milestones(self, events: list[PceEvent]) -> None:
        for event in events:
            if event.kind == "tool_milestone":
                key = event.step_id or "_loop"
                if event.payload.get("tool_count") is not None:
                    self._milestone_state.setdefault(key, {})["tool_count"] = event.payload[
                        "tool_count"
                    ]

    def _active_goal(self, snapshot: Any) -> Any:
        active_id = snapshot.active_goal_id
        if active_id is None:
            return None
        try:
            return self._ce.get_goal_sync(active_id)
        except Exception:  # noqa: BLE001
            return None

    def _append_signal(self, goal_id: str, signal: CoverageSignal) -> None:
        goal = self._ce.get_goal_sync(goal_id)
        if goal is None:
            return
        goal.coverage_signals.append(signal)
        ring = int(self._cfg.signal_ring_size)
        if len(goal.coverage_signals) > ring:
            del goal.coverage_signals[: len(goal.coverage_signals) - ring]


def start_pce_monitor(
    *,
    loop_id: str,
    context_engine: Any,
    strange_loop: Any,
    interaction_mode: str | None,
) -> PceMonitor | None:
    """Spawn the monitor unless disabled or in a read-only mode."""
    if context_engine is None or strange_loop is None:
        return None
    config = getattr(strange_loop, "config", None)
    agent = getattr(config, "agent", None)
    loop_cfg = getattr(agent, "loop", None)
    pce_cfg = getattr(loop_cfg, "pce", None)
    if pce_cfg is None or not getattr(pce_cfg, "enabled", False):
        return None
    if interaction_mode in ("plan", "ask"):
        return None
    monitor = PceMonitor(
        loop_id=loop_id,
        context_engine=context_engine,
        pce_config=pce_cfg,
        soothe_config=config,
        fast_model=getattr(strange_loop, "_fast_llm", None),
    )
    monitor.start()
    return monitor


async def stop_pce_monitor(monitor: PceMonitor | None) -> None:
    if monitor is not None:
        with contextlib.suppress(Exception):
            await monitor.stop()
