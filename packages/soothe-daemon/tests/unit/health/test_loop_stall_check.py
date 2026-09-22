"""Unit tests for the daemon loop stall scanner health check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from soothe_daemon.health.checks.loop_stall_check import (
    LOOP_STALL_DETECTED,
    _is_process_alive,
    _loop_has_active_runner,
    _parse_updated_at,
    _stall_timeout_minutes,
    check_loop_stall,
)
from soothe_daemon.health.models import CheckStatus


def _make_daemon_config(timeout_minutes: int = 30) -> SimpleNamespace:
    """Build a minimal daemon config with the stall timeout field."""
    return SimpleNamespace(
        loop_status_reconciliation=SimpleNamespace(
            stale_running_seconds=180,
            stall_timeout_minutes=timeout_minutes,
        ),
    )


class _StubPersistence:
    """Minimal persistence manager stub for loop row queries."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.closed = False

    async def list_loops(
        self,
        status_filter: str | None = None,
        limit: int = 100,
        **_kw: object,
    ) -> list[dict]:
        assert status_filter == "running"
        return self._rows[:limit]

    async def close(self) -> None:
        self.closed = True


class _StubEventBus:
    """Captures published events for assertions."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, event: dict) -> None:
        self.published.append((topic, event))


def test_parse_updated_at_valid_iso() -> None:
    result = _parse_updated_at("2025-01-15T10:30:00Z")
    assert result is not None
    assert result.tzinfo is not None
    assert result.year == 2025


def test_parse_updated_at_with_offset() -> None:
    result = _parse_updated_at("2025-01-15T10:30:00+08:00")
    assert result is not None
    assert result.tzinfo is not None


def test_parse_updated_at_naive_assumes_utc() -> None:
    result = _parse_updated_at("2025-01-15T10:30:00")
    assert result is not None
    assert result.tzinfo is UTC


def test_parse_updated_at_invalid_returns_none() -> None:
    assert _parse_updated_at("not-a-date") is None
    assert _parse_updated_at("") is None
    assert _parse_updated_at(None) is None
    assert _parse_updated_at(12345) is None  # type: ignore[arg-type]


def test_is_process_alive_self() -> None:
    """The current process is always alive."""
    import os

    assert _is_process_alive(os.getpid()) is True


def test_is_process_alive_nonexistent() -> None:
    assert _is_process_alive(999_999) is False


def test_stall_timeout_minutes_from_config() -> None:
    cfg = _make_daemon_config(timeout_minutes=45)
    assert _stall_timeout_minutes(cfg) == 45.0


def test_stall_timeout_minutes_default_when_none() -> None:
    assert _stall_timeout_minutes(None) == 30.0


def test_stall_timeout_minutes_falls_back_when_missing_field() -> None:
    cfg = SimpleNamespace(loop_status_reconciliation=SimpleNamespace())
    assert _stall_timeout_minutes(cfg) == 30.0


def test_stall_timeout_minutes_falls_back_when_no_recon() -> None:
    cfg = SimpleNamespace()
    assert _stall_timeout_minutes(cfg) == 30.0


def test_loop_has_active_runner_in_stream_set() -> None:
    daemon = SimpleNamespace(
        _active_stream_loop_ids={"loop-1"},
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    assert _loop_has_active_runner(daemon, "loop-1") is True
    assert _loop_has_active_runner(daemon, "loop-2") is False


def test_loop_has_active_runner_in_query_set() -> None:
    daemon = SimpleNamespace(
        _active_stream_loop_ids=set(),
        _loops_with_active_query={"loop-q"},
        _query_engine=None,
    )
    assert _loop_has_active_runner(daemon, "loop-q") is True


def test_loop_has_active_runner_in_engine_runners() -> None:
    qe = SimpleNamespace(
        _active_runners={"loop-e"},
        _loops_turn_starting=set(),
    )
    daemon = SimpleNamespace(
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=qe,
    )
    assert _loop_has_active_runner(daemon, "loop-e") is True


def test_loop_has_active_runner_in_turn_starting() -> None:
    qe = SimpleNamespace(
        _active_runners={},
        _loops_turn_starting={"loop-s"},
    )
    daemon = SimpleNamespace(
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=qe,
    )
    assert _loop_has_active_runner(daemon, "loop-s") is True


@pytest.mark.asyncio
async def test_no_running_loops_returns_ok() -> None:
    persistence = _StubPersistence([])
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(),
        daemon=daemon,
    )
    assert result.category == "loop_stall"
    assert result.status == CheckStatus.OK
    assert len(result.checks) == 1
    assert result.checks[0].name == "loop_stall_scan"
    assert result.checks[0].details["scanned_count"] == 0


@pytest.mark.asyncio
async def test_fresh_running_loop_not_stalled() -> None:
    now = datetime.now(UTC)
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-fresh",
                "status": "running",
                "updated_at": now.isoformat(),
                "current_thread_id": "thread-1",
            }
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.OK
    assert result.checks[0].details["stalled_count"] == 0


@pytest.mark.asyncio
async def test_stalled_loop_emits_warning_and_event() -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=45)).isoformat()
    bus = _StubEventBus()
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-stalled",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "thread-1",
            }
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=bus,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.WARNING
    assert result.message == "1 stalled loop(s) detected"
    stall_check = next(c for c in result.checks if c.name == "loop_stall:loop-stalled")
    assert stall_check.status == CheckStatus.WARNING
    assert stall_check.details["loop_id"] == "loop-stalled"
    assert stall_check.details["age_minutes"] == 45.0
    assert stall_check.details["timeout_minutes"] == 30.0
    assert len(bus.published) == 1
    topic, event = bus.published[0]
    assert topic == "loop:loop-stalled"
    assert event["type"] == LOOP_STALL_DETECTED
    assert event["loop_id"] == "loop-stalled"
    assert event["age_minutes"] == 45.0


@pytest.mark.asyncio
async def test_active_runner_prevents_stall_flag() -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=45)).isoformat()
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-active",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "thread-1",
            }
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=_StubEventBus(),
        _active_stream_loop_ids={"loop-active"},
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.OK
    assert result.checks[0].details["stalled_count"] == 0


@pytest.mark.asyncio
async def test_multiple_stalled_loops() -> None:
    now = datetime.now(UTC)
    old1 = (now - timedelta(minutes=35)).isoformat()
    old2 = (now - timedelta(minutes=60)).isoformat()
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-1",
                "status": "running",
                "updated_at": old1,
                "current_thread_id": "t1",
            },
            {
                "loop_id": "loop-2",
                "status": "running",
                "updated_at": old2,
                "current_thread_id": "t2",
            },
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=_StubEventBus(),
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.WARNING
    assert result.checks[0].details["stalled_count"] == 2
    assert result.checks[0].details["scanned_count"] == 2
    stall_names = {c.name for c in result.checks if c.name.startswith("loop_stall:")}
    assert stall_names == {"loop_stall:loop-1", "loop_stall:loop-2"}


@pytest.mark.asyncio
async def test_mixed_stalled_and_fresh() -> None:
    now = datetime.now(UTC)
    fresh = now.isoformat()
    old = (now - timedelta(minutes=50)).isoformat()
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-fresh",
                "status": "running",
                "updated_at": fresh,
                "current_thread_id": "t1",
            },
            {
                "loop_id": "loop-old",
                "status": "running",
                "updated_at": old,
                "current_thread_id": "t2",
            },
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=_StubEventBus(),
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.WARNING
    assert result.checks[0].details["scanned_count"] == 2
    assert result.checks[0].details["stalled_count"] == 1
    stall_checks = [c for c in result.checks if c.name.startswith("loop_stall:")]
    assert len(stall_checks) == 1
    assert stall_checks[0].details["loop_id"] == "loop-old"


@pytest.mark.asyncio
async def test_unparseable_updated_at_skipped() -> None:
    now = datetime.now(UTC)
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-bad-ts",
                "status": "running",
                "updated_at": "not-a-timestamp",
                "current_thread_id": "t1",
            },
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.OK
    assert result.checks[0].details["stalled_count"] == 0


@pytest.mark.asyncio
async def test_no_daemon_uses_standalone_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=45)).isoformat()
    stub = _StubPersistence(
        [
            {
                "loop_id": "loop-standalone",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "t1",
            }
        ]
    )

    async def _fake_init(*_a, **_kw):
        return stub

    monkeypatch.setattr(
        "soothe.sloop.checkpoints.manager.StrangeLoopCheckpointPersistenceManager",
        lambda *_a, **_kw: stub,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=None,
        now=now,
    )
    assert result.status == CheckStatus.WARNING
    assert result.checks[0].details["stalled_count"] == 1
    assert stub.closed is True


@pytest.mark.asyncio
async def test_custom_timeout_threshold() -> None:
    now = datetime.now(UTC)
    border = (now - timedelta(minutes=15)).isoformat()
    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-border",
                "status": "running",
                "updated_at": border,
                "current_thread_id": "t1",
            },
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result_10 = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=10),
        daemon=daemon,
        now=now,
    )
    assert result_10.status == CheckStatus.WARNING

    stub2 = _StubPersistence(persistence._rows)
    daemon2 = SimpleNamespace(
        _persistence_manager=stub2,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result_20 = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=20),
        daemon=daemon2,
        now=now,
    )
    assert result_20.status == CheckStatus.OK


@pytest.mark.asyncio
async def test_event_emission_failure_does_not_break_check() -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=45)).isoformat()

    class _FailingBus:
        async def publish(self, topic: str, event: dict) -> None:
            raise RuntimeError("bus down")

    persistence = _StubPersistence(
        [
            {
                "loop_id": "loop-bus-fail",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "t1",
            }
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=_FailingBus(),
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.status == CheckStatus.WARNING
    assert result.checks[0].details["stalled_count"] == 1


@pytest.mark.asyncio
async def test_empty_loop_id_skipped() -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=45)).isoformat()
    persistence = _StubPersistence(
        [
            {
                "loop_id": "",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "t1",
            },
            {
                "loop_id": "loop-real",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "t2",
            },
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.checks[0].details["scanned_count"] == 1
    assert result.checks[0].details["stalled_count"] == 1


@pytest.mark.asyncio
async def test_non_dict_row_skipped() -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=45)).isoformat()
    persistence = _StubPersistence(
        [
            "not-a-dict",  # type: ignore[list-item]
            {
                "loop_id": "loop-ok",
                "status": "running",
                "updated_at": old_ts,
                "current_thread_id": "t1",
            },
        ]
    )
    daemon = SimpleNamespace(
        _persistence_manager=persistence,
        _event_bus=None,
        _active_stream_loop_ids=set(),
        _loops_with_active_query=set(),
        _query_engine=None,
    )
    result = await check_loop_stall(
        config=SimpleNamespace(),
        daemon_config=_make_daemon_config(timeout_minutes=30),
        daemon=daemon,
        now=now,
    )
    assert result.checks[0].details["scanned_count"] == 1
    assert result.checks[0].details["stalled_count"] == 1


@pytest.mark.asyncio
async def test_check_via_health_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify loop_stall is wired into the HealthChecker dispatch."""
    from soothe_daemon.health.checker import VITAL_CATEGORIES, HealthChecker

    assert "loop_stall" in VITAL_CATEGORIES

    checker = HealthChecker()

    async def _fake_check_loop_stall() -> object:
        from soothe_daemon.health.models import CategoryResult

        return CategoryResult(
            category="loop_stall",
            status=CheckStatus.OK,
            checks=[],
            message="stub",
        )

    monkeypatch.setattr(checker, "check_loop_stall", _fake_check_loop_stall)

    async def fake_nano(_config=None, **kwargs):
        cats = kwargs.get("categories") or []
        return [{"category": c, "status": "ok", "checks": [], "message": None} for c in cats]

    async def fake_host(_config=None, **kwargs):
        cats = kwargs.get("categories") or []
        return [{"category": c, "status": "ok", "checks": [], "message": None} for c in cats]

    monkeypatch.setattr("soothe_nano.diagnose.diagnose", fake_nano)
    monkeypatch.setattr("soothe.diagnose.diagnose", fake_host)

    for method_name in ("check_config", "check_daemon", "check_persistence", "check_external_apis"):

        async def _ok(*_a, _n=method_name, **_k):
            from soothe_daemon.health.models import CategoryResult

            return CategoryResult(category=_n, status=CheckStatus.OK, checks=[])

        monkeypatch.setattr(checker, method_name, _ok)

    report = await checker.run_all_checks()
    categories = [c.category for c in report.categories]
    assert "loop_stall" in categories
