"""Durable loop-input queue and daemon runtime metadata store (IG-DAEM-004).

Persists queued `loop_input` payloads before they enter the in-memory
`LoopInputDispatcher` queues so queued-but-unstarted user turns survive a
daemon restart (graceful or crash). Also carries a small key/value table for
daemon-side runtime counters that must not rewind across restarts (e.g. the
per-loop outbound event seq).

Backend selection follows Rule 10 (Unified Persistence Backend): PostgreSQL
when `persistence.default_backend == "postgresql"`, SQLite otherwise — same
factory pattern as the cron job store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from soothe_nano.paths import resolve_sqlite_db_path

logger = logging.getLogger(__name__)

# Payloads above this size are not persisted (memory-only fallback). Keeps a
# pathological attachment payload from wedging the durable queue.
MAX_DURABLE_PAYLOAD_BYTES = 16 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_input_queue (
    id TEXT PRIMARY KEY,
    loop_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_input_queue_loop
    ON loop_input_queue(loop_id, created_at);
CREATE TABLE IF NOT EXISTS loop_runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class LoopInputStore:
    """SQLite-backed durable loop-input queue (async API)."""

    def __init__(self, db_path: str | None = None, reader_pool_size: int = 2) -> None:
        from soothe_nano.config.models import SqliteRuntimeConfig
        from soothe_nano.persistence.sqlite_runtime import SqliteRuntimeRegistry

        self._db_path = db_path or str(resolve_sqlite_db_path("loop_runtime"))
        self._runtime = SqliteRuntimeRegistry.acquire(
            self._db_path,
            SqliteRuntimeConfig(reader_pool_size=reader_pool_size),
        )
        self._runtime.run_write_sync(self._create_schema_sync)
        logger.info("LoopInputStore initialized: path=%s", self._db_path)

    def _create_schema_sync(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    async def enqueue(self, loop_id: str, message_id: str, payload_json: str) -> None:
        """Insert one durable payload row (write-ahead of the memory queue)."""
        await self._runtime.run_write(
            lambda conn: self._enqueue_sync(conn, loop_id, message_id, payload_json)
        )

    def _enqueue_sync(
        self, conn: sqlite3.Connection, loop_id: str, message_id: str, payload_json: str
    ) -> None:
        conn.execute(
            "INSERT INTO loop_input_queue (id, loop_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (message_id, loop_id, payload_json, _now_iso()),
        )

    async def pending_rows(self, loop_id: str | None = None) -> list[dict[str, str]]:
        """Return pending rows ordered by insertion time (per loop or all)."""
        if loop_id:

            def _read_one(conn: sqlite3.Connection) -> list[dict[str, str]]:
                rows = conn.execute(
                    """
                    SELECT id, loop_id, payload FROM loop_input_queue
                    WHERE loop_id = ? ORDER BY created_at, rowid
                    """,
                    (loop_id,),
                ).fetchall()
                return [dict(row) for row in rows]

            return await self._runtime.run_read(_read_one)

        def _read_all(conn: sqlite3.Connection) -> list[dict[str, str]]:
            rows = conn.execute(
                "SELECT id, loop_id, payload FROM loop_input_queue ORDER BY created_at, rowid"
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._runtime.run_read(_read_all)

    async def delete(self, message_id: str) -> None:
        """Remove one row (worker pickup / explicit completion)."""
        await self._runtime.run_write(lambda conn: self._delete_sync(conn, message_id))

    def _delete_sync(self, conn: sqlite3.Connection, message_id: str) -> None:
        conn.execute("DELETE FROM loop_input_queue WHERE id = ?", (message_id,))

    async def delete_loop(self, loop_id: str) -> int:
        """Remove all rows for a loop (loop deletion / GC). Returns row count."""
        return await self._runtime.run_write(lambda conn: self._delete_loop_sync(conn, loop_id))

    def _delete_loop_sync(self, conn: sqlite3.Connection, loop_id: str) -> int:
        conn.execute("DELETE FROM loop_input_queue WHERE loop_id = ?", (loop_id,))
        return conn.execute("SELECT changes()").fetchone()[0]

    async def get_meta(self, key: str) -> str | None:
        """Return the stored value for a runtime meta key, or None."""

        def _read(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT value FROM loop_runtime_meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

        return await self._runtime.run_read(_read)

    async def set_meta(self, key: str, value: str) -> None:
        """Upsert a runtime meta key (e.g. `event_seq:<loop_id>`)."""
        await self._runtime.run_write(lambda conn: self._set_meta_sync(conn, key, value))

    def _set_meta_sync(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO loop_runtime_meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    async def close(self) -> None:
        """Release the process Runtime reference."""
        from soothe_nano.persistence.sqlite_runtime import SqliteRuntimeRegistry

        await SqliteRuntimeRegistry.release(self._db_path)


class PostgresLoopInputStore:
    """PostgreSQL-backed durable loop-input queue (async API matches LoopInputStore)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any | None = None
        self._init_lock = asyncio.Lock()
        self._writer_lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._init_lock:
            if self._pool is not None:
                return self._pool
            await asyncio.to_thread(self._open_pool_sync)
            return self._pool

    def _open_pool_sync(self) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=self._dsn,
            min_size=1,
            max_size=4,
            open=True,
            kwargs={"autocommit": False, "row_factory": dict_row},
        )
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for statement in (s.strip() for s in _SCHEMA.split(";") if s.strip()):
                    cur.execute(statement)
            conn.commit()
        self._pool = pool

    async def enqueue(self, loop_id: str, message_id: str, payload_json: str) -> None:
        async with self._writer_lock:
            pool = await self._ensure_pool()
            await asyncio.to_thread(self._enqueue_sync, pool, loop_id, message_id, payload_json)

    def _enqueue_sync(self, pool: Any, loop_id: str, message_id: str, payload_json: str) -> None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO loop_input_queue (id, loop_id, payload, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (message_id, loop_id, payload_json, _now_iso()),
                )
            conn.commit()

    async def pending_rows(self, loop_id: str | None = None) -> list[dict[str, str]]:
        pool = await self._ensure_pool()
        return await asyncio.to_thread(self._pending_rows_sync, pool, loop_id)

    def _pending_rows_sync(self, pool: Any, loop_id: str | None) -> list[dict[str, str]]:
        query = "SELECT id, loop_id, payload FROM loop_input_queue"
        params: tuple[str, ...] = ()
        if loop_id:
            query += " WHERE loop_id = %s"
            params = (loop_id,)
        query += " ORDER BY created_at"
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return [
                    {"id": row["id"], "loop_id": row["loop_id"], "payload": row["payload"]}
                    for row in cur.fetchall()
                ]

    async def delete(self, message_id: str) -> None:
        async with self._writer_lock:
            pool = await self._ensure_pool()
            await asyncio.to_thread(self._delete_sync, pool, message_id)

    def _delete_sync(self, pool: Any, message_id: str) -> None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM loop_input_queue WHERE id = %s", (message_id,))
            conn.commit()

    async def delete_loop(self, loop_id: str) -> int:
        async with self._writer_lock:
            pool = await self._ensure_pool()
            return await asyncio.to_thread(self._delete_loop_sync, pool, loop_id)

    def _delete_loop_sync(self, pool: Any, loop_id: str) -> int:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM loop_input_queue WHERE loop_id = %s", (loop_id,))
                deleted = cur.rowcount
            conn.commit()
        return max(0, deleted)

    async def get_meta(self, key: str) -> str | None:
        pool = await self._ensure_pool()
        return await asyncio.to_thread(self._get_meta_sync, pool, key)

    def _get_meta_sync(self, pool: Any, key: str) -> str | None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM loop_runtime_meta WHERE key = %s", (key,))
                row = cur.fetchone()
                return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._writer_lock:
            pool = await self._ensure_pool()
            await asyncio.to_thread(self._set_meta_sync, pool, key, value)

    def _set_meta_sync(self, pool: Any, key: str, value: str) -> None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO loop_runtime_meta (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, value),
                )
            conn.commit()

    async def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            await asyncio.to_thread(pool.close)


def create_loop_input_store(config: Any) -> LoopInputStore | PostgresLoopInputStore:
    """Build the durable loop-input store for the configured persistence backend."""
    if config is not None and config.persistence.default_backend == "postgresql":
        dsn = config.resolve_postgres_dsn_for_database("metadata")
        logger.info("Loop input store backend=postgresql db=metadata")
        return PostgresLoopInputStore(dsn=dsn)

    logger.info("Loop input store backend=sqlite")
    return LoopInputStore()


def serialize_durable_payload(message: dict[str, Any]) -> str | None:
    """Serialize a queue payload for durable storage, or None when not persistable."""
    try:
        data = json.dumps(message, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(data.encode("utf-8", "ignore")) > MAX_DURABLE_PAYLOAD_BYTES:
        return None
    return data


__all__ = [
    "LoopInputStore",
    "MAX_DURABLE_PAYLOAD_BYTES",
    "PostgresLoopInputStore",
    "create_loop_input_store",
    "serialize_durable_payload",
]
