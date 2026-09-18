"""Tests for config-gated static breakpoints on the StrangeLoop graph."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from soothe.config.models import LoopDebugConfig
from soothe.sloop.orchestrator.builder import (
    _validated_breakpoints,
)
from soothe.sloop.orchestrator.runner import (
    _loop_breakpoints_active,
    _paused_at_static_breakpoint,
)

_GRAPH_NODES = {
    "intake",
    "enter_loop",
    "delegate",
    "dispatch",
    "execute",
    "record_progress",
    "reconcile",
    "root_eval",
    "finalize",
    "await_user",
    "plan_review",
}


def test_loop_debug_config_defaults_inactive() -> None:
    cfg = LoopDebugConfig()
    assert cfg.is_active() is False
    assert cfg.interrupt_before == []
    assert cfg.interrupt_after == []


def test_loop_debug_config_active_when_configured() -> None:
    cfg = LoopDebugConfig(interrupt_before=["execute"], interrupt_after=["dispatch"])
    assert cfg.is_active() is True


def test_validated_breakpoints_keeps_known_nodes() -> None:
    assert _validated_breakpoints(["execute", "dispatch"], _GRAPH_NODES) == [
        "execute",
        "dispatch",
    ]


def test_validated_breakpoints_drops_unknown_nodes() -> None:
    assert _validated_breakpoints(["execute", "nonexistent_station"], _GRAPH_NODES) == ["execute"]


def test_breakpoints_active_reads_agent_loop_debug() -> None:
    debug = LoopDebugConfig(interrupt_before=["finalize"])
    cfg = SimpleNamespace(agent=SimpleNamespace(loop=SimpleNamespace(debug=debug)))
    ctx = SimpleNamespace(strange_loop=SimpleNamespace(config=cfg))
    assert _loop_breakpoints_active(ctx) is True


def test_breakpoints_inactive_by_default() -> None:
    cfg = SimpleNamespace(agent=SimpleNamespace(loop=SimpleNamespace(debug=LoopDebugConfig())))
    ctx = SimpleNamespace(strange_loop=SimpleNamespace(config=cfg))
    assert _loop_breakpoints_active(ctx) is False


def _snapshot(next_nodes: tuple[str, ...], interrupts: tuple = ()) -> Any:
    return SimpleNamespace(next=next_nodes, interrupts=interrupts, tasks=())


def test_paused_at_static_breakpoint_detected() -> None:
    assert _paused_at_static_breakpoint(_snapshot(("execute",))) is True


def test_completed_graph_is_not_paused() -> None:
    assert _paused_at_static_breakpoint(_snapshot(())) is False


def test_dynamic_interrupt_pause_is_not_a_static_breakpoint() -> None:
    """A `interrupt()` pause shows pending nodes AND interrupts — the runner
    must route it through the clarification-resume path instead."""
    snapshot = _snapshot(("await_user",), interrupts=(SimpleNamespace(),))
    assert _paused_at_static_breakpoint(snapshot) is False


def test_task_level_interrupts_pause_is_not_a_static_breakpoint() -> None:
    task = SimpleNamespace(interrupts=(SimpleNamespace(),))
    snapshot = SimpleNamespace(next=("await_user",), interrupts=(), tasks=(task,))
    assert _paused_at_static_breakpoint(snapshot) is False
