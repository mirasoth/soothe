"""Tests for daemon restart detection helpers (IG-DAEM-004)."""

from __future__ import annotations

from soothe_cli.runtime.state.daemon_restart import daemon_epoch, restart_detected


def test_epoch_from_daemon_id() -> None:
    info = {"daemon_id": "123:2026-11-27T00:00:00+00:00", "pid": 123}
    assert daemon_epoch(info) == "123:2026-11-27T00:00:00+00:00"


def test_epoch_falls_back_to_pid_and_start_time() -> None:
    info = {"pid": 7, "started_at": "2026-11-27T01:00:00+00:00"}
    assert daemon_epoch(info) == "7:2026-11-27T01:00:00+00:00"


def test_epoch_none_for_unusable_payloads() -> None:
    assert daemon_epoch(None) is None
    assert daemon_epoch("nope") is None
    assert daemon_epoch({}) is None
    assert daemon_epoch({"daemon_id": "  "}) is None
    assert daemon_epoch({"pid": 7}) is None


def test_restart_detected_only_between_known_epochs() -> None:
    assert restart_detected("a", "b") is True
    assert restart_detected("a", "a") is False
    assert restart_detected(None, "b") is False
    assert restart_detected("a", None) is False
    assert restart_detected(None, None) is False


def test_epoch_change_marks_restart_like_real_daemon_info() -> None:
    first = daemon_epoch({"daemon_id": "100:t0", "started_at": "t0"})
    second = daemon_epoch({"daemon_id": "100:t1", "started_at": "t1"})
    assert restart_detected(first, second) is True
