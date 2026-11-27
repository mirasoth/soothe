"""Integration: PostgresLoopInputStore against a real PostgreSQL (IG-DAEM-004).

Runs only with `--run-integration` and a reachable PostgreSQL. The DSN comes
from `SOOTHE_TEST_POSTGRES_BASE_DSN` (same convention as the shared
checkpointer-pool integration tests); the test uses a throwaway database name
derived from the test run so it never touches real metadata.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgresql]

_DEFAULT_BASE_DSN = "postgresql://postgres:postgres@127.0.0.1:6432"


def _dsn() -> str:
    pytest.importorskip("psycopg_pool")
    base = os.getenv("SOOTHE_TEST_POSTGRES_BASE_DSN", _DEFAULT_BASE_DSN).rstrip("/")
    return f"{base}/soothe_loop_input_test_{uuid.uuid4().hex[:8]}"


def _require_reachable_postgres(dsn: str) -> None:
    """Skip (not fail) when the configured PostgreSQL is unreachable."""
    import psycopg

    try:
        psycopg.connect(dsn, connect_timeout=3).close()
    except Exception as exc:
        pytest.skip(f"PostgreSQL at {dsn} unreachable: {exc}")


@pytest.mark.asyncio
async def test_postgres_loop_input_store_roundtrip() -> None:
    from soothe_daemon.persistence.loop_input_store import PostgresLoopInputStore

    dsn = _dsn()
    _require_reachable_postgres(dsn)
    store = PostgresLoopInputStore(dsn=dsn)
    try:
        await store.enqueue("loop-a", "m1", '{"text": "first"}')
        await store.enqueue("loop-a", "m2", '{"text": "second"}')
        await store.enqueue("loop-b", "m3", '{"text": "other"}')

        rows = await store.pending_rows("loop-a")
        assert [r["id"] for r in rows] == ["m1", "m2"]

        # Complete-before-process deletion.
        await store.delete("m1")
        rows = await store.pending_rows("loop-a")
        assert [r["id"] for r in rows] == ["m2"]

        # Loop cleanup only touches its own rows.
        deleted = await store.delete_loop("loop-a")
        assert deleted == 1
        rows = await store.pending_rows()
        assert [r["id"] for r in rows] == ["m3"]

        # Runtime meta roundtrip + upsert (event-seq persistence path).
        assert await store.get_meta("event_seq:loop-a") is None
        await store.set_meta("event_seq:loop-a", "12")
        await store.set_meta("event_seq:loop-a", "20")
        assert await store.get_meta("event_seq:loop-a") == "20"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_store_survives_reopen() -> None:
    """Rows written before close are still pending after a fresh instance."""
    from soothe_daemon.persistence.loop_input_store import PostgresLoopInputStore

    dsn = _dsn()
    _require_reachable_postgres(dsn)
    store = PostgresLoopInputStore(dsn=dsn)
    await store.enqueue("loop-reopen", "m1", '{"text": "persisted"}')
    await store.close()

    reopened = PostgresLoopInputStore(dsn=dsn)
    try:
        rows = await reopened.pending_rows("loop-reopen")
        assert [r["id"] for r in rows] == ["m1"]
        assert '"persisted"' in rows[0]["payload"]
    finally:
        await reopened.close()
