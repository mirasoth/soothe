"""Durable loop-input queue tests for LoopInputDispatcher (IG-DAEM-004)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from soothe_daemon.persistence.loop_input_store import LoopInputStore
from soothe_daemon.runtime.loop_dispatcher import LoopInputDispatcher


class _FakeDaemon:
    """Minimal daemon stand-in recording processed loop-input messages."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self._config = None
        self._running = True
        self.processed: list[tuple[str, dict[str, Any]]] = []
        self._gate = gate

    async def _process_loop_input_message(self, loop_id: str, msg: dict[str, Any]) -> None:
        self.processed.append((loop_id, msg))
        if self._gate is not None:
            await self._gate.wait()


@pytest.fixture
async def store(tmp_path: Path) -> LoopInputStore:
    s = LoopInputStore(db_path=str(tmp_path / "loop_runtime.db"))
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_worker_pickup_deletes_durable_row(store: LoopInputStore) -> None:
    daemon = _FakeDaemon()
    dispatcher = LoopInputDispatcher(daemon, max_queue_size=0)
    dispatcher._store = store

    await dispatcher.enqueue("loop-a", {"type": "input", "text": "hello"})
    for _ in range(100):
        if daemon.processed:
            break
        await asyncio.sleep(0.01)

    assert daemon.processed[0][1]["text"] == "hello"
    assert await store.pending_rows("loop-a") == []

    await dispatcher.shutdown()


@pytest.mark.asyncio
async def test_shutdown_keeps_unpicked_row_and_recovery_reenqueues(store: LoopInputStore) -> None:
    gate = asyncio.Event()
    daemon = _FakeDaemon(gate=gate)
    dispatcher = LoopInputDispatcher(daemon, max_queue_size=0)
    dispatcher._store = store

    await dispatcher.enqueue("loop-a", {"type": "input", "text": "first"})
    for _ in range(100):
        if daemon.processed:
            break
        await asyncio.sleep(0.01)
    await dispatcher.enqueue("loop-a", {"type": "input", "text": "second"})

    # Graceful stop while the first message is mid-processing: the second
    # message never reached a worker and must survive on disk.
    await dispatcher.shutdown()
    gate.set()

    rows = await store.pending_rows("loop-a")
    assert len(rows) == 1
    assert '"second"' in rows[0]["payload"]

    restarted = _FakeDaemon()
    dispatcher2 = LoopInputDispatcher(restarted, max_queue_size=0)
    dispatcher2._store = store
    recovered = await dispatcher2.recover_pending_inputs()
    assert recovered == 1
    for _ in range(100):
        if restarted.processed:
            break
        await asyncio.sleep(0.01)
    assert restarted.processed[0][1]["text"] == "second"
    # The recovered message keeps its durable id until a worker picks it up.
    assert await store.pending_rows("loop-a") == []

    await dispatcher2.shutdown()


@pytest.mark.asyncio
async def test_cleanup_loop_deletes_durable_rows(store: LoopInputStore) -> None:
    daemon = _FakeDaemon()
    dispatcher = LoopInputDispatcher(daemon, max_queue_size=0)
    dispatcher._store = store

    await dispatcher.enqueue("loop-a", {"type": "input", "text": "x"})
    await dispatcher.cleanup_loop("loop-a")

    assert await store.pending_rows("loop-a") == []
    await dispatcher.shutdown()


@pytest.mark.asyncio
async def test_worker_survives_processing_exception(store: LoopInputStore) -> None:
    class _ExplodingDaemon(_FakeDaemon):
        async def _process_loop_input_message(self, loop_id: str, msg: dict[str, Any]) -> None:
            raise RuntimeError("boom")

    dispatcher = LoopInputDispatcher(_ExplodingDaemon(), max_queue_size=0)
    dispatcher._store = store

    await dispatcher.enqueue("loop-a", {"type": "input", "text": "x"})
    for _ in range(100):
        if await store.pending_rows("loop-a") == []:
            break
        await asyncio.sleep(0.01)

    # The durable row is gone even though processing raised — no poison retry.
    assert await store.pending_rows("loop-a") == []
    await dispatcher.shutdown()


@pytest.mark.asyncio
async def test_memory_only_fallback_when_store_unavailable() -> None:
    daemon = _FakeDaemon()
    dispatcher = LoopInputDispatcher(daemon, max_queue_size=0)

    await dispatcher.enqueue("loop-a", {"type": "input", "text": "x"})
    for _ in range(100):
        if daemon.processed:
            break
        await asyncio.sleep(0.01)

    assert daemon.processed[0][1]["text"] == "x"
    await dispatcher.shutdown()
