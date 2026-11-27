"""PostgreSQL persistence for the thread audit trace (`audit` database)."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from soothe.logging.audit_store import now_ts

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (thread_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_audit_records_ts ON audit_records(ts);
"""


class PostgresAuditStore:
    """Append-only PostgreSQL audit store (`thread_id, seq` keyed JSONB rows)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._lock = threading.Lock()
        self._pool: Any | None = None
        self._schema_ready = False

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def db_path(self) -> None:
        """SQLite path compatibility; always `None` for PostgreSQL."""
        return None

    def _ensure_pool(self) -> Any:
        with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                msg = "psycopg_pool is required for the PostgreSQL audit store"
                raise ImportError(msg) from exc

            pool = ConnectionPool(
                conninfo=self._dsn,
                min_size=1,
                max_size=4,
                open=True,
                kwargs={"autocommit": False},
            )
            self._pool = pool
            self._ensure_schema_unlocked(pool)
            return pool

    def _ensure_schema_unlocked(self, pool: Any) -> None:
        if self._schema_ready:
            return
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for statement in _split_statements(_SCHEMA):
                    cur.execute(statement)
            conn.commit()
        self._schema_ready = True

    def append(self, thread_id: str, records: list[dict[str, Any]]) -> None:
        """Assign monotonic `seq` per thread (advisory-locked) and insert rows."""
        if not records:
            return
        pool = self._ensure_pool()
        with self._lock:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (thread_id,))
                    cur.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM audit_records WHERE thread_id = %s",
                        (thread_id,),
                    )
                    row = cur.fetchone()
                    base_seq = int(row[0]) if row else 0
                    cur.executemany(
                        """
                        INSERT INTO audit_records (thread_id, seq, ts, kind, payload)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        """,
                        [
                            (
                                thread_id,
                                base_seq + index + 1,
                                str(record.get("timestamp") or now_ts()),
                                str(record.get("kind") or "event"),
                                json.dumps(record, default=str),
                            )
                            for index, record in enumerate(records)
                        ],
                    )
                conn.commit()

    def fetch_recent(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent records in chronological order."""
        pool = self._ensure_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload FROM audit_records
                    WHERE thread_id = %s
                    ORDER BY seq DESC
                    LIMIT %s
                    """,
                    (thread_id, limit),
                )
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for (payload,) in reversed(rows):
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            if isinstance(payload, dict):
                out.append(payload)
        return out

    def run_retention(self, retention_days: int, max_size_mb: int) -> int:
        """Delete expired records, then whole oldest threads over the size budget."""
        pool = self._ensure_pool()
        with self._lock:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cutoff = (
                        datetime.now(UTC) - timedelta(days=max(0, retention_days))
                    ).isoformat()
                    cur.execute("DELETE FROM audit_records WHERE ts < %s", (cutoff,))
                    budget_bytes = max(0, max_size_mb) * 1024 * 1024
                    purged = 0
                    if budget_bytes > 0:
                        cur.execute(
                            """
                            SELECT thread_id, SUM(LENGTH(payload::text))
                            FROM audit_records
                            GROUP BY thread_id
                            ORDER BY MIN(ts) ASC
                            """
                        )
                        rows = cur.fetchall()
                        total = sum(int(size or 0) for _, size in rows)
                        for thread_id, size in rows:
                            if total <= budget_bytes:
                                break
                            cur.execute(
                                "DELETE FROM audit_records WHERE thread_id = %s",
                                (thread_id,),
                            )
                            total -= int(size or 0)
                            purged += 1
                conn.commit()
        return purged

    def close(self) -> None:
        with self._lock:
            pool = self._pool
            self._pool = None
            self._schema_ready = False
        if pool is not None:
            try:
                pool.close()
            except Exception:
                logger.debug("Error closing PostgreSQL audit pool", exc_info=True)


def _split_statements(sql: str) -> list[str]:
    return [part.strip() for part in sql.split(";") if part.strip()]


__all__ = ["PostgresAuditStore"]
