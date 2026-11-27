"""Tests for the per-loop stream replay buffer (IG-DAEM-004 follow-up)."""

from __future__ import annotations

from soothe_daemon.query.stream_replay import StreamReplayBuffer


def test_record_ignores_frames_without_int_seq() -> None:
    buf = StreamReplayBuffer()
    buf.record("loop-a", {"type": "event", "seq": "not-int"})
    buf.record("loop-a", {"type": "event"})
    buf.record("", {"type": "event", "seq": 1})
    assert buf.resume("loop-a", 0)["frames"] == []


def test_resume_returns_frames_after_last_seq_in_order() -> None:
    buf = StreamReplayBuffer()
    for seq in (1, 2, 3, 4, 5):
        buf.record("loop-a", {"type": "event", "seq": seq, "i": seq})

    result = buf.resume("loop-a", 3)
    assert [f["seq"] for f in result["frames"]] == [4, 5]
    assert result["oldest_seq"] == 1
    assert result["newest_seq"] == 5
    assert result["gap"] is False


def test_resume_up_to_date_reports_no_gap_and_no_frames() -> None:
    buf = StreamReplayBuffer()
    for seq in (1, 2, 3):
        buf.record("loop-a", {"type": "event", "seq": seq})

    result = buf.resume("loop-a", 3)
    assert result["frames"] == []
    assert result["gap"] is False


def test_resume_reports_gap_when_older_frames_evicted() -> None:
    buf = StreamReplayBuffer(max_frames=3)
    for seq in range(1, 6):
        buf.record("loop-a", {"type": "event", "seq": seq})

    # Client last saw seq 1, but only 3-5 survive in the ring.
    result = buf.resume("loop-a", 1)
    assert [f["seq"] for f in result["frames"]] == [3, 4, 5]
    assert result["oldest_seq"] == 3
    assert result["gap"] is True

    # A client at seq 2 (right before the window) is contiguous — no gap.
    result = buf.resume("loop-a", 2)
    assert result["gap"] is False


def test_resume_unknown_loop_is_empty_without_gap() -> None:
    buf = StreamReplayBuffer()
    result = buf.resume("loop-unknown", 7)
    assert result["frames"] == []
    assert result["gap"] is False
    assert result["oldest_seq"] is None


def test_loop_entries_are_lru_bounded() -> None:
    buf = StreamReplayBuffer(max_loops=3)
    for i in range(5):
        buf.record(f"loop-{i}", {"type": "event", "seq": 1})

    assert buf.resume("loop-0", 0)["frames"] == []
    assert buf.resume("loop-3", 0)["frames"] != []


def test_drop_loop_clears_entry() -> None:
    buf = StreamReplayBuffer()
    buf.record("loop-a", {"type": "event", "seq": 1})
    buf.drop_loop("loop-a")
    assert buf.resume("loop-a", 0)["frames"] == []
