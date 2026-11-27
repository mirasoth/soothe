"""Per-loop outbound event-seq persistence tests (IG-DAEM-004)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from soothe_daemon.persistence.loop_input_store import LoopInputStore
from soothe_daemon.query.engine import QueryEngine


@pytest.fixture
async def store(tmp_path: Path) -> LoopInputStore:
    s = LoopInputStore(db_path=str(tmp_path / "loop_runtime.db"))
    yield s
    await s.close()


def _daemon(store: LoopInputStore | None) -> SimpleNamespace:
    dispatcher = SimpleNamespace(_durable_store=(lambda: store) if store else (lambda: None))
    return SimpleNamespace(_loop_input_dispatcher=dispatcher)


@pytest.mark.asyncio
async def test_next_seq_continues_from_persisted_value(store: LoopInputStore) -> None:
    await store.set_meta("event_seq:loop-a", "41")
    engine = QueryEngine(_daemon(store))

    await engine.hydrate_loop_seq("loop-a")
    assert engine._next_loop_seq("loop-a") == 42


@pytest.mark.asyncio
async def test_schedule_and_flush_persist_current_seq(store: LoopInputStore) -> None:
    engine = QueryEngine(_daemon(store))
    await engine.hydrate_loop_seq("loop-a")

    engine._next_loop_seq("loop-a")
    engine._next_loop_seq("loop-a")
    engine._schedule_seq_persist("loop-a")
    await engine.flush_loop_seqs()

    assert await store.get_meta("event_seq:loop-a") == "2"


@pytest.mark.asyncio
async def test_rehydrate_does_not_lower_running_counter(store: LoopInputStore) -> None:
    engine = QueryEngine(_daemon(store))
    await engine.hydrate_loop_seq("loop-a")
    await store.set_meta("event_seq:loop-a", "5")

    engine._next_loop_seq("loop-a")
    await engine.hydrate_loop_seq("loop-a")

    assert engine._next_loop_seq("loop-a") == 2


@pytest.mark.asyncio
async def test_flush_writes_every_known_loop(store: LoopInputStore) -> None:
    engine = QueryEngine(_daemon(store))
    await engine.hydrate_loop_seq("loop-a")
    await engine.hydrate_loop_seq("loop-b")

    engine._next_loop_seq("loop-a")
    engine._next_loop_seq("loop-b")
    engine._next_loop_seq("loop-b")
    await engine.flush_loop_seqs()

    assert await store.get_meta("event_seq:loop-a") == "1"
    assert await store.get_meta("event_seq:loop-b") == "2"


@pytest.mark.asyncio
async def test_no_store_is_a_noop() -> None:
    engine = QueryEngine(_daemon(None))
    await engine.hydrate_loop_seq("loop-a")
    engine._schedule_seq_persist("loop-a")
    await engine.flush_loop_seqs()

    assert engine._next_loop_seq("loop-a") == 1


@pytest.mark.asyncio
async def test_persist_task_completes_without_leaking(store: LoopInputStore) -> None:
    engine = QueryEngine(_daemon(store))
    await engine.hydrate_loop_seq("loop-a")
    engine._next_loop_seq("loop-a")
    engine._schedule_seq_persist("loop-a")

    # One-shot hydration marker must not block a second loop from hydrating.
    await engine.hydrate_loop_seq("loop-b")
    engine._next_loop_seq("loop-b")
    engine._schedule_seq_persist("loop-b")

    await engine.flush_loop_seqs()
    await asyncio.sleep(0)
    assert not engine._seq_persist_tasks


@pytest.mark.asyncio
async def test_broadcast_records_frame_into_stream_replay() -> None:
    """IG-DAEM-004 follow-up: `_broadcast_loop_message` feeds the replay buffer."""
    broadcasts: list[dict] = []

    async def _broadcast(frame: dict) -> None:
        broadcasts.append(frame)

    daemon = SimpleNamespace(
        _loop_input_dispatcher=SimpleNamespace(_durable_store=lambda: None),
        _broadcast=_broadcast,
        _loop_broadcast_budget=None,
    )
    engine = QueryEngine(daemon)
    await engine._broadcast_loop_message(
        "loop-a",
        {"type": "event", "namespace": [], "mode": "messages", "data": {"content": "hi"}},
    )

    assert len(broadcasts) == 1
    result = engine.stream_resume("loop-a", 0)
    assert [f["seq"] for f in result["frames"]] == [1]
    assert result["gap"] is False
