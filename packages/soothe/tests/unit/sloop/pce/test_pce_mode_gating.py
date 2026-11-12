"""PCE monitor spawn gating tests.

The monitor must never spawn in plan/ask interaction modes or when the
pce config is disabled.
"""

from __future__ import annotations

from types import SimpleNamespace

from soothe.config.models import PceLoopConfig, StrangeLoopConfig
from soothe.context.models import GoalNode
from soothe.sloop.pce.events import reset_pce_registry
from soothe.sloop.pce.monitor import start_pce_monitor, stop_pce_monitor


class _FakeCE:
    def get_all_goals(self) -> list[GoalNode]:
        return []


class _FakeStrangeLoop:
    """strange_loop.config mirrors SootheConfig: config.agent.loop.pce."""

    def __init__(self, pce: PceLoopConfig | None) -> None:
        loop_cfg = StrangeLoopConfig()
        if pce is not None:
            loop_cfg.pce = pce
        self.config = SimpleNamespace(agent=SimpleNamespace(loop=loop_cfg))
        self._fast_llm = None


async def test_no_monitor_in_plan_mode():
    reset_pce_registry()
    monitor = start_pce_monitor(
        loop_id="loop-1",
        context_engine=_FakeCE(),
        strange_loop=_FakeStrangeLoop(PceLoopConfig()),
        interaction_mode="plan",
    )
    assert monitor is None


async def test_no_monitor_in_ask_mode():
    reset_pce_registry()
    monitor = start_pce_monitor(
        loop_id="loop-1",
        context_engine=_FakeCE(),
        strange_loop=_FakeStrangeLoop(PceLoopConfig()),
        interaction_mode="ask",
    )
    assert monitor is None


async def test_no_monitor_when_disabled():
    reset_pce_registry()
    monitor = start_pce_monitor(
        loop_id="loop-1",
        context_engine=_FakeCE(),
        strange_loop=_FakeStrangeLoop(PceLoopConfig(enabled=False)),
        interaction_mode="agent",
    )
    assert monitor is None


async def test_monitor_spawns_in_agent_mode():
    reset_pce_registry()
    monitor = start_pce_monitor(
        loop_id="loop-1",
        context_engine=_FakeCE(),
        strange_loop=_FakeStrangeLoop(PceLoopConfig()),
        interaction_mode="agent",
    )
    assert monitor is not None
    await stop_pce_monitor(monitor)


async def test_no_monitor_without_context_engine():
    reset_pce_registry()
    monitor = start_pce_monitor(
        loop_id="loop-1",
        context_engine=None,
        strange_loop=_FakeStrangeLoop(PceLoopConfig()),
        interaction_mode="agent",
    )
    assert monitor is None
