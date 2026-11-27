"""Structured audit trace store for thread records (replaces conversation.jsonl).

Append-only, derived data: never authoritative for resumption (that is the
LangGraph checkpointer's job). Backends branch on `persistence.default_backend`
via :func:`configure_audit_store` — SQLite mode writes
`$SOOTHE_DATA_DIR/databases/audit.db`, PostgreSQL mode writes the database
registered under `persistence.postgres_databases["audit"]`.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

AUDIT_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS audit_records (
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (thread_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_audit_records_ts ON audit_records(ts);
"""


@runtime_checkable
class AuditStoreProtocol(Protocol):
    """Shared sync API for SQLite and PostgreSQL audit trace stores."""

    def append(self, thread_id: str, records: list[dict[str, Any]]) -> None: ...

    def fetch_recent(self, thread_id: str, limit: int) -> list[dict[str, Any]]: ...

    def run_retention(self, retention_days: int, max_size_mb: int) -> int: ...

    def close(self) -> None: ...


def now_ts() -> str:
    """Return the canonical audit timestamp (UTC ISO-8601)."""
    return datetime.now(UTC).isoformat()


class SqliteAuditStore:
    """Append-only SQLite audit store (`thread_id, seq` keyed payload rows)."""

    def __init__(self, db_path: Path | None = None) -> None:
        from soothe_nano.persistence.sqlite_runtime import SqliteRuntimeRegistry

        self._db_path = db_path or _resolve_audit_db_path()
        self._runtime = SqliteRuntimeRegistry.acquire(self._db_path)
        self._runtime.run_write_sync(lambda conn: conn.executescript(AUDIT_SCHEMA_SQLITE))

    @property
    def db_path(self) -> Path:
        return self._db_path

    def append(self, thread_id: str, records: list[dict[str, Any]]) -> None:
        """Assign monotonic `seq` per thread and insert the record payloads."""

        def _write(conn: Any) -> None:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM audit_records WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            base_seq = int(row[0]) if row else 0
            conn.executemany(
                """
                INSERT INTO audit_records (thread_id, seq, ts, kind, payload)
                VALUES (?, ?, ?, ?, ?)
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

        self._runtime.run_write_sync(_write)

    def fetch_recent(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent records in chronological order."""

        def _read(conn: Any) -> list[dict[str, Any]]:
            cursor = conn.execute(
                """
                SELECT payload FROM audit_records
                WHERE thread_id = ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (thread_id, limit),
            )
            out: list[dict[str, Any]] = []
            for (payload,) in reversed(cursor.fetchall()):
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    out.append(parsed)
            return out

        return self._runtime.run_read_sync(_read)

    def run_retention(self, retention_days: int, max_size_mb: int) -> int:
        """Delete expired records, then whole oldest threads over the size budget.

        Returns the number of threads purged by the size budget.
        """

        def _write(conn: Any) -> int:
            cutoff = (datetime.now(UTC) - timedelta(days=max(0, retention_days))).isoformat()
            conn.execute("DELETE FROM audit_records WHERE ts < ?", (cutoff,))
            budget_bytes = max(0, max_size_mb) * 1024 * 1024
            if budget_bytes <= 0:
                conn.commit()
                return 0
            rows = conn.execute(
                """
                SELECT thread_id, SUM(LENGTH(payload))
                FROM audit_records
                GROUP BY thread_id
                ORDER BY MIN(ts) ASC
                """
            ).fetchall()
            total = sum(int(size or 0) for _, size in rows)
            purged = 0
            for thread_id, size in rows:
                if total <= budget_bytes:
                    break
                conn.execute("DELETE FROM audit_records WHERE thread_id = ?", (thread_id,))
                total -= int(size or 0)
                purged += 1
            conn.commit()
            return purged

        return self._runtime.run_write_sync(_write)

    def close(self) -> None:
        from soothe_nano.persistence.sqlite_runtime import SqliteRuntimeRegistry

        SqliteRuntimeRegistry.release_sync(self._db_path)


def _resolve_audit_db_path() -> Path:
    from soothe_nano.paths import resolve_sqlite_db_path

    return resolve_sqlite_db_path("audit")


_shared_store: AuditStoreProtocol | None = None
_shared_store_lock = threading.Lock()


def _close_shared_store_unlocked() -> None:
    global _shared_store
    if _shared_store is None:
        return
    try:
        _shared_store.close()
    except Exception:
        logger.debug("Error closing audit store", exc_info=True)
    _shared_store = None


def configure_audit_store(config: SootheConfig) -> AuditStoreProtocol:
    """Select SQLite or PostgreSQL audit store from `persistence.default_backend`.

    Call once during process startup after PostgreSQL databases are provisioned.
    PostgreSQL mode requires `persistence.postgres_databases["audit"]` and fails
    loud when the key is missing — never silently mix backends.
    """
    global _shared_store
    with _shared_store_lock:
        _close_shared_store_unlocked()
        if config.persistence.default_backend == "postgresql":
            from soothe.logging.audit_store_postgres import PostgresAuditStore

            dsn = config.resolve_postgres_dsn_for_database("audit")
            store: AuditStoreProtocol = PostgresAuditStore(dsn=dsn)
            logger.info("Audit store backend=postgresql db=audit")
        else:
            store = SqliteAuditStore()
            logger.info("Audit store backend=sqlite path=%s", store.db_path)
        _shared_store = store
        return store


def get_audit_store(db_path: Path | None = None) -> AuditStoreProtocol:
    """Return the process-wide audit store singleton.

    Pass `db_path` only in tests to force an isolated SQLite file.
    """
    global _shared_store
    with _shared_store_lock:
        if db_path is not None:
            current_path = getattr(_shared_store, "db_path", None)
            if _shared_store is None or current_path != db_path:
                _close_shared_store_unlocked()
                _shared_store = SqliteAuditStore(db_path=db_path)
            return _shared_store
        if _shared_store is None:
            _shared_store = SqliteAuditStore()
        return _shared_store


def reset_audit_store_for_tests() -> None:
    """Clear the process-wide singleton (tests only)."""
    with _shared_store_lock:
        _close_shared_store_unlocked()


__all__ = [
    "AUDIT_SCHEMA_SQLITE",
    "AuditStoreProtocol",
    "SqliteAuditStore",
    "configure_audit_store",
    "get_audit_store",
    "now_ts",
    "reset_audit_store_for_tests",
]
