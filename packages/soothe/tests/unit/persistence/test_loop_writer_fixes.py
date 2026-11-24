"""Regression tests for the LoopPersistenceWriter lifecycle fixes.

Covers review f994682f: init-lock-across-await deadlock, stale bound loop
after close, released-loop re-attach on durable writes, and the shutdown
drain of coalesced (non-durable) checkpoints.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe.persistence.loop_writer import (
    LoopPersistenceWriter,
    PersistWriteMode,
)
from soothe.sloop.state.checkpoint import StrangeLoopCheckpoint, ThreadHealthMetrics


def _checkpoint(loop_id: str, *, status: str = "running") -> StrangeLoopCheckpoint:
    now = datetime.now(UTC)
    return StrangeLoopCheckpoint(
        loop_id=loop_id,
        current_thread_id=loop_id,
        status=status,
        current_goal_index=0,
        thread_health_metrics=ThreadHealthMetrics(
            thread_id=loop_id,
            last_updated=now,
        ),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _reset_writer_singleton() -> None:
    import soothe.persistence.loop_writer as mod

    mod._writer_singleton = None
    LoopPersistenceWriter._bound_loop = None
    yield
    mod._writer_singleton = None
    LoopPersistenceWriter._bound_loop = None


class _SlowPool:
    """Pool stub whose get_shared_instance sleeps, simulating DB I/O."""

    @classmethod
    async def get_shared_instance(cls, config: Any) -> _SlowPool | None:
        await asyncio.sleep(0.05)
        return cls()


def _pg_config() -> MagicMock:
    cfg = MagicMock()
    cfg.persistence.default_backend = "postgresql"
    cfg.agent.loop.concurrency.checkpoint.flush_interval = 60.0
    cfg.agent.loop.concurrency.checkpoint.close_timeout_seconds = 2.0
    cfg.agent.loop.concurrency.checkpoint.durable_flush_timeout = 5.0
    return cfg


@pytest.mark.asyncio
async def test_concurrent_init_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two coroutines racing `get_shared_instance` on one loop must not deadlock.

    The old implementation awaited pool creation while holding a
    thread-affine `threading.Lock`; the second coroutine blocked the only
    thread able to resume the first (review f994682f, Critical).
    """
    monkeypatch.setattr("soothe.sloop.checkpoints.shared_pool.SharedPostgreSQLPool", _SlowPool)
    cfg = _pg_config()
    results = await asyncio.gather(
        LoopPersistenceWriter.get_shared_instance(cfg),
        LoopPersistenceWriter.get_shared_instance(cfg),
    )
    assert results[0] is results[1]
    assert results[0] is not None


@pytest.mark.asyncio
async def test_durable_enqueue_re_attaches_released_loop() -> None:
    """A durable boundary write clears the release mark for that loop.

    Release marks are per-run; a resumed run's writes must not stay
    filtered forever (review f994682f, Major).
    """
    writer = LoopPersistenceWriter(shared_pool=MagicMock(), flush_interval=60.0)
    writer._flush_loop = AsyncMock()
    writer.flush_durable = AsyncMock()

    cp = _checkpoint("loop-reattach")
    writer._released_loops.add("loop-reattach")

    # Non-durable write is still filtered after release...
    await writer.enqueue_checkpoint("loop-reattach", cp)
    assert "loop-reattach" not in writer._pending

    # ...and a durable write re-attaches the loop.
    await writer.enqueue_checkpoint("loop-reattach", cp, durable=True)
    assert "loop-reattach" not in writer._released_loops


@pytest.mark.asyncio
async def test_shutdown_drains_pending_checkpoints() -> None:
    """`shutdown` flushes coalesced non-durable writes before canceling.

    The old shutdown dropped up to one flush interval of loop state
    (review f994682f, Major).
    """
    writer = LoopPersistenceWriter(shared_pool=MagicMock(), flush_interval=60.0)
    writer._worker_task = None

    flushed: list[str] = []

    async def _fake_flush(loop_id: str, *, force_full: bool) -> None:
        # Mirror the real `_flush_loop`: pop the pending entry when flushed.
        with writer._pending_guard:
            writer._pending.pop(loop_id, None)
        flushed.append(loop_id)

    writer._flush_loop = _fake_flush  # type: ignore[method-assign]

    cp = _checkpoint("loop-drain")
    await writer.enqueue_checkpoint("loop-drain", cp)
    await writer.shutdown()

    assert flushed == ["loop-drain"]
    assert "loop-drain" not in writer._pending


@pytest.mark.asyncio
async def test_close_clears_bound_loop_for_recreated_writer() -> None:
    """Closing the singleton clears `_bound_loop` so a re-created writer rebinds.

    The stale binding made every post-restart `_run_on_main` schedule onto
    the closed loop and leak the coroutine (review f994682f, Major).
    """
    import soothe.persistence.loop_writer as mod

    main_loop = asyncio.get_running_loop()
    LoopPersistenceWriter.bind_main_loop(main_loop)
    writer = LoopPersistenceWriter(shared_pool=MagicMock(), flush_interval=60.0)
    writer._ensure_worker = AsyncMock()
    writer.shutdown = AsyncMock()
    mod._writer_singleton = writer

    await LoopPersistenceWriter.close_shared_instance()

    assert mod._writer_singleton is None
    assert LoopPersistenceWriter._bound_loop is None
    writer.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_on_main_rebinds_after_bound_loop_closes() -> None:
    """`_run_on_main` rebinds to the running loop when the bound loop closed."""
    # Simulate a stale (closed) bound loop.
    closed = asyncio.new_event_loop()
    LoopPersistenceWriter.bind_main_loop(closed)
    closed.close()

    def _run_on_fresh_loop() -> Any:
        async def _inner() -> Any:
            writer = LoopPersistenceWriter(shared_pool=MagicMock(), flush_interval=60.0)

            async def _coro() -> str:
                return "ok"

            # Must rebind instead of raising RuntimeError("Event loop is
            # closed") / leaking the coroutine object.
            running = asyncio.get_running_loop()
            result = await writer._run_on_main(_coro)
            rebound_ok = LoopPersistenceWriter._bound_loop is running and not running.is_closed()
            return result, rebound_ok

        fresh = asyncio.new_event_loop()
        try:
            return fresh.run_until_complete(_inner())
        finally:
            fresh.close()

    result, rebound_ok = await asyncio.to_thread(_run_on_fresh_loop)
    assert result == "ok"
    assert rebound_ok


@pytest.mark.asyncio
async def test_submit_enqueue_survives_released_loop_re_entry() -> None:
    """End-to-end bridge path: release → durable re-attach → enqueue accepted."""
    writer = LoopPersistenceWriter(shared_pool=MagicMock(), flush_interval=60.0)
    writer._flush_loop = AsyncMock()
    writer.flush_durable = AsyncMock()
    writer._ensure_worker = AsyncMock()

    cp = _checkpoint("loop-e2e")
    await writer.submit_enqueue("loop-e2e", cp, write_mode=PersistWriteMode.INDEX_ONLY)
    await writer.submit_release_loop("loop-e2e", timeout=1.0)

    # Resume: a durable goal-boundary write re-attaches the loop.
    await writer.submit_enqueue("loop-e2e", cp, durable=True)
    assert "loop-e2e" not in writer._released_loops
