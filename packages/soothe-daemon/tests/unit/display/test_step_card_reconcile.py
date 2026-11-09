"""Resume-time verification of step card phases against daemon step state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from soothe.display.card_ledger import CardMutation, card_to_wire_dict, utc_now_iso
from soothe.display.snapshot_types import GoalDisplaySnapshot
from soothe.display.transcript_types import MessageData, MessageType
from soothe_nano.core.events import CARD_CREATED

from soothe_daemon.display import step_card_reconcile
from soothe_daemon.display.loop_card_manager import LoopCardManager
from soothe_daemon.display.step_card_reconcile import (
    StepOutcome,
    StepStatusContext,
    has_running_step_cards,
    load_step_status_context,
    reconcile_step_card_groups,
    reconcile_step_cards,
)


def _step_card(step_id: str, phase: str = "running") -> MessageData:
    return MessageData(
        type=MessageType.STEP_PROGRESS,
        content="",
        id=f"card-{step_id}",
        step_progress_id=step_id,
        step_progress_description=f"step {step_id}",
        step_progress_phase=phase,
    )


def _context(
    outcomes: dict[str, StepOutcome] | None = None,
    *,
    runner_active: bool = False,
    clarification_pending: bool = False,
) -> StepStatusContext:
    return StepStatusContext(
        outcomes=outcomes or {},
        runner_active=runner_active,
        clarification_pending=clarification_pending,
    )


def _fake_context(context: StepStatusContext):
    async def _load(_daemon: Any, _loop_id: str) -> StepStatusContext:
        return context

    return _load


def test_completed_step_becomes_success_with_durable_metrics() -> None:
    card = _step_card("s1")
    context = _context({"s1": StepOutcome("completed", duration_ms=2500, tool_call_count=7)})

    [result] = reconcile_step_cards([card], context, allow_running=True)

    assert result.step_progress_phase == "success"
    assert result.step_success is True
    assert result.step_duration_ms == 2500
    assert result.step_tool_call_count == 7


def test_failed_step_becomes_error_with_durable_error_text() -> None:
    card = _step_card("s1")
    context = _context(
        {"s1": StepOutcome("failed", duration_ms=900, tool_call_count=2, error="tool crashed")}
    )

    [result] = reconcile_step_cards([card], context, allow_running=True)

    assert result.step_progress_phase == "error"
    assert result.step_success is False
    assert result.step_summary == "tool crashed"


def test_skipped_step_becomes_interrupted() -> None:
    [result] = reconcile_step_cards(
        [_step_card("s1")], _context({"s1": StepOutcome("skipped")}), allow_running=True
    )

    assert result.step_progress_phase == "interrupted"
    assert result.step_summary


def test_active_step_stays_running_with_live_runner() -> None:
    context = _context({"s1": StepOutcome("active")}, runner_active=True)

    [result] = reconcile_step_cards([_step_card("s1")], context, allow_running=True)

    assert result.step_progress_phase == "running"


def test_active_step_stays_running_while_clarification_is_parked() -> None:
    context = _context({"s1": StepOutcome("active")}, clarification_pending=True)

    [result] = reconcile_step_cards([_step_card("s1")], context, allow_running=True)

    assert result.step_progress_phase == "running"


def test_active_step_is_interrupted_without_a_runner() -> None:
    context = _context({"s1": StepOutcome("active")})

    [result] = reconcile_step_cards([_step_card("s1")], context, allow_running=True)

    assert result.step_progress_phase == "interrupted"


def test_unknown_step_is_interrupted_without_a_runner() -> None:
    [result] = reconcile_step_cards([_step_card("s1")], _context(), allow_running=True)

    assert result.step_progress_phase == "interrupted"
    assert result.step_summary


def test_unknown_step_stays_running_with_a_live_runner() -> None:
    [result] = reconcile_step_cards(
        [_step_card("s1")], _context(runner_active=True), allow_running=True
    )

    assert result.step_progress_phase == "running"


def test_historical_goal_cards_never_keep_running() -> None:
    context = _context({"s1": StepOutcome("active")}, runner_active=True)

    [result] = reconcile_step_cards([_step_card("s1")], context, allow_running=False)

    assert result.step_progress_phase == "interrupted"


def test_terminal_cards_and_other_types_are_unchanged() -> None:
    terminal = _step_card("s1", phase="success")
    user = MessageData(type=MessageType.USER, content="hello", id="user-1")
    context = _context({"s1": StepOutcome("failed")})

    result = reconcile_step_cards([terminal, user], context, allow_running=False)

    assert result == [terminal, user]
    assert result[0] is terminal


def test_reconcile_does_not_mutate_the_ledger_card() -> None:
    card = _step_card("s1")

    [result] = reconcile_step_cards([card], _context(), allow_running=True)

    assert card.step_progress_phase == "running"
    assert result is not card


def test_has_running_step_cards() -> None:
    assert has_running_step_cards([_step_card("s1")]) is True
    assert has_running_step_cards([_step_card("s1", "success")]) is False
    assert has_running_step_cards([MessageData(type=MessageType.USER, content="hi")]) is False


@pytest.mark.asyncio
async def test_groups_fast_path_never_reads_daemon_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unexpected(_daemon: Any, _loop_id: str) -> StepStatusContext:
        raise AssertionError("no running cards should skip daemon reads")

    monkeypatch.setattr(step_card_reconcile, "load_step_status_context", _unexpected)

    [cards] = await reconcile_step_card_groups(
        SimpleNamespace(),
        "loop_fast",
        [([_step_card("s1", "success")], True)],
    )

    assert cards[0].step_progress_phase == "success"


@pytest.mark.asyncio
async def test_load_step_status_context_reads_the_durable_dag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dag = SimpleNamespace(
        goals={
            "g1": SimpleNamespace(
                steps=SimpleNamespace(
                    nodes={
                        "s1": SimpleNamespace(
                            status="completed",
                            execution=SimpleNamespace(
                                duration_ms=2500, tool_call_count=7, error=None
                            ),
                        )
                    }
                )
            )
        }
    )

    class _Persistence:
        async def load_dag(self) -> Any:
            return dag

    monkeypatch.setattr(
        "soothe.context.store_factory.resolve_context_engine_persistence",
        lambda _config, _loop_id: _Persistence(),
    )
    daemon = SimpleNamespace(_config=object(), _runner=None)

    context = await load_step_status_context(daemon, "loop_dag")

    assert context.runner_active is False
    assert context.clarification_pending is False
    assert context.outcomes["s1"] == StepOutcome("completed", duration_ms=2500, tool_call_count=7)


@pytest.mark.asyncio
async def test_load_step_status_context_keeps_parked_clarification_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _pending(_daemon: Any, _loop_id: str) -> bool:
        return True

    class _Persistence:
        async def load_dag(self) -> Any:
            return None

    monkeypatch.setattr("soothe_daemon.runtime.auto_resume.peek_clarification_pending", _pending)
    monkeypatch.setattr(
        "soothe.context.store_factory.resolve_context_engine_persistence",
        lambda _config, _loop_id: _Persistence(),
    )

    context = await load_step_status_context(
        SimpleNamespace(_config=object(), _runner=object()), "loop_clarify"
    )

    assert context.clarification_pending is True
    assert context.step_may_run is True
    assert context.outcomes == {}


async def _seed_running_step(manager: LoopCardManager, loop_id: str, step_id: str) -> None:
    ledger = await manager.ensure_for_loop(loop_id)
    card = _step_card(step_id)
    await ledger.append(
        CardMutation(
            seq=ledger.next_seq(),
            ts=utc_now_iso(),
            op="create",
            card_id=card.id,
            kind=str(card.type),
            data=card_to_wire_dict(card),
        )
    )


@pytest.mark.asyncio
async def test_fetch_loop_history_marks_orphan_step_interrupted(
    isolated_display_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    await _seed_running_step(manager, "loop_orphan", "s1")
    monkeypatch.setattr(step_card_reconcile, "load_step_status_context", _fake_context(_context()))

    payload = await manager.fetch_loop_history("loop_orphan", loop_status="idle")

    [card] = payload["live_cards"]
    assert card["step_progress_phase"] == "interrupted"
    assert card["step_summary"]


@pytest.mark.asyncio
async def test_fetch_loop_history_restores_completed_step_outcome(
    isolated_display_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    await _seed_running_step(manager, "loop_done", "s1")
    monkeypatch.setattr(
        step_card_reconcile,
        "load_step_status_context",
        _fake_context(_context({"s1": StepOutcome("completed", 3200, 11)})),
    )

    payload = await manager.fetch_loop_history("loop_done", loop_status="idle")

    [card] = payload["live_cards"]
    assert card["step_progress_phase"] == "success"
    assert card["step_duration_ms"] == 3200
    assert card["step_tool_call_count"] == 11


@pytest.mark.asyncio
async def test_fetch_loop_history_keeps_a_live_step_running(
    isolated_display_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    await _seed_running_step(manager, "loop_live", "s1")
    monkeypatch.setattr(
        step_card_reconcile,
        "load_step_status_context",
        _fake_context(_context({"s1": StepOutcome("active")}, runner_active=True)),
    )

    payload = await manager.fetch_loop_history("loop_live", loop_status="running")

    [card] = payload["live_cards"]
    assert card["step_progress_phase"] == "running"


@pytest.mark.asyncio
async def test_fetch_loop_history_closes_running_step_in_frozen_goal(
    isolated_display_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = GoalDisplaySnapshot(
        goal_id="g1",
        goal_index=0,
        goal_text="old goal",
        status="completed",
        started_at="2026-11-09T00:00:00+00:00",
        completed_at="2026-11-09T00:01:00+00:00",
        duration_ms=60000,
        tokens_used=0,
        plan_summary=None,
        step_outcomes=[],
        goal_completion="done",
        display_cards=[_step_card("s_old")],
        card_count=1,
    )
    isolated_display_db.insert_goal_snapshot_with_auto_index(
        "loop_frozen", goal_id="g1", snapshot=snapshot.to_wire_dict()
    )
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    monkeypatch.setattr(
        step_card_reconcile,
        "load_step_status_context",
        _fake_context(_context({"s_old": StepOutcome("active")}, runner_active=True)),
    )

    payload = await manager.fetch_loop_history("loop_frozen", loop_status="running")

    [goal] = payload["goals"]
    [card] = goal["display_cards"]
    assert card["step_progress_phase"] == "interrupted"


@pytest.mark.asyncio
async def test_replay_to_client_streams_verified_step_phase(
    isolated_display_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    await _seed_running_step(manager, "loop_replay", "s1")
    monkeypatch.setattr(step_card_reconcile, "load_step_status_context", _fake_context(_context()))
    frames: list[dict[str, Any]] = []

    async def collect(frame: dict[str, Any]) -> None:
        frames.append(frame)

    await manager.replay_to_client("loop_replay", collect)

    created = [frame for frame in frames if frame.get("type") == CARD_CREATED]
    assert len(created) == 1
    assert created[0]["data"]["step_progress_phase"] == "interrupted"
