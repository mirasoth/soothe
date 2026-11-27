"""Unit tests for the durable loop-input store (IG-DAEM-004)."""

from __future__ import annotations

from pathlib import Path

import pytest

from soothe_daemon.persistence.loop_input_store import (
    LoopInputStore,
    serialize_durable_payload,
)


@pytest.fixture
async def store(tmp_path: Path) -> LoopInputStore:
    s = LoopInputStore(db_path=str(tmp_path / "loop_runtime.db"))
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_enqueue_and_pending_rows_order(store: LoopInputStore) -> None:
    await store.enqueue("loop-a", "m1", '{"text": "first"}')
    await store.enqueue("loop-a", "m2", '{"text": "second"}')
    await store.enqueue("loop-b", "m3", '{"text": "other loop"}')

    rows = await store.pending_rows("loop-a")
    assert [r["id"] for r in rows] == ["m1", "m2"]
    assert all(r["loop_id"] == "loop-a" for r in rows)

    all_rows = await store.pending_rows()
    assert [r["id"] for r in all_rows] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_delete_removes_single_row(store: LoopInputStore) -> None:
    await store.enqueue("loop-a", "m1", '{"text": "x"}')
    await store.delete("m1")
    assert await store.pending_rows("loop-a") == []


@pytest.mark.asyncio
async def test_delete_loop_clears_rows(store: LoopInputStore) -> None:
    await store.enqueue("loop-a", "m1", '{"text": "x"}')
    await store.enqueue("loop-a", "m2", '{"text": "y"}')
    await store.enqueue("loop-b", "m3", '{"text": "z"}')

    deleted = await store.delete_loop("loop-a")
    assert deleted == 2
    remaining = await store.pending_rows()
    assert [r["id"] for r in remaining] == ["m3"]


@pytest.mark.asyncio
async def test_meta_roundtrip_and_upsert(store: LoopInputStore) -> None:
    assert await store.get_meta("event_seq:loop-a") is None
    await store.set_meta("event_seq:loop-a", "12")
    await store.set_meta("event_seq:loop-a", "20")
    assert await store.get_meta("event_seq:loop-a") == "20"


def test_serialize_durable_payload_rejects_oversized_and_unserializable() -> None:
    import unittest.mock

    assert serialize_durable_payload({"text": "ok"}) is not None
    assert serialize_durable_payload({"bad": object()}) is None  # non-serializable object
    with unittest.mock.patch(
        "soothe_daemon.persistence.loop_input_store.MAX_DURABLE_PAYLOAD_BYTES", 4
    ):
        assert serialize_durable_payload({"text": "too big"}) is None
