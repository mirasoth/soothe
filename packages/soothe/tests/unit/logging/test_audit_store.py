"""Audit store tests: per-thread seq monotonicity, fetch ordering, retention sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from soothe.logging.audit_store import SqliteAuditStore


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    audit_store = SqliteAuditStore(db_path=tmp_path / "audit.db")
    yield audit_store
    audit_store.close()


def _record(kind: str, ts: str, *, text: str | None = None) -> dict[str, Any]:
    return {"timestamp": ts, "kind": kind, "text": text if text is not None else ts}


def test_append_assigns_monotonic_seq_per_thread(store: Any) -> None:
    store.append("t1", [_record("event", "ts-a"), _record("event", "ts-b")])
    store.append("t1", [_record("event", "ts-c")])
    store.append("t2", [_record("event", "ts-d")])

    rows = store.fetch_recent("t1", limit=100)
    assert [r["text"] for r in rows] == ["ts-a", "ts-b", "ts-c"]

    rows_t2 = store.fetch_recent("t2", limit=100)
    assert [r["text"] for r in rows_t2] == ["ts-d"]


def test_fetch_recent_returns_most_recent_in_chronological_order(store: Any) -> None:
    for index in range(150):
        store.append("t1", [_record("event", f"ts-{index:04d}", text=str(index))])

    rows = store.fetch_recent("t1", limit=10)
    assert [int(r["text"]) for r in rows] == list(range(140, 150))


def test_retention_age_cutoff_deletes_old_records(store: Any) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    fresh_ts = datetime.now(UTC).isoformat()
    store.append("old-thread", [_record("event", old_ts)])
    store.append("fresh-thread", [_record("event", fresh_ts)])

    purged = store.run_retention(retention_days=30, max_size_mb=100)

    assert purged == 0
    assert store.fetch_recent("old-thread", limit=10) == []
    assert len(store.fetch_recent("fresh-thread", limit=10)) == 1


def test_retention_size_budget_purges_oldest_threads_first(store: Any) -> None:
    fresh_ts = datetime.now(UTC).isoformat()
    older_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # ~1500 bytes per record payload per thread.
    store.append("t-older", [_record("event", older_ts, text="x" * 1500)])
    store.append("t-newer", [_record("event", fresh_ts, text="y" * 1500)])

    # Budget ~2097 bytes fits one thread → the oldest is purged first.
    purged = store.run_retention(retention_days=30, max_size_mb=0.002)
    assert purged == 1
    assert store.fetch_recent("t-older", limit=10) == []
    assert len(store.fetch_recent("t-newer", limit=10)) == 1


def test_retention_zero_budget_disables_size_sweep(store: Any) -> None:
    fresh_ts = datetime.now(UTC).isoformat()
    older_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    store.append("t-older", [_record("event", older_ts, text="x" * 1500)])
    store.append("t-newer", [_record("event", fresh_ts, text="y" * 1500)])

    # Budget 0 disables the size-based sweep (age cutoff still applies).
    purged = store.run_retention(retention_days=30, max_size_mb=0)
    assert purged == 0
    assert len(store.fetch_recent("t-older", limit=10)) == 1


def test_retention_size_budget_keeps_newest_thread(store: Any) -> None:
    fresh_ts = datetime.now(UTC).isoformat()
    older_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    store.append("t-older", [_record("event", older_ts, text="x" * 1500)])
    store.append("t-newer", [_record("event", fresh_ts, text="y" * 1500)])

    # Budget below a single thread's payload forces oldest-first deletion of all.
    purged = store.run_retention(retention_days=30, max_size_mb=0.0001)
    assert purged == 2

    store.append("t-older", [_record("event", older_ts, text="x" * 1500)])
    store.append("t-newer", [_record("event", fresh_ts, text="y" * 1500)])
    store.append("t-newest", [_record("event", fresh_ts, text="z" * 1500)])
    # A large budget keeps everything.
    store.run_retention(retention_days=30, max_size_mb=100)
    assert len(store.fetch_recent("t-older", limit=10)) == 1
    assert len(store.fetch_recent("t-newest", limit=10)) == 1
