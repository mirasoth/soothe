"""Tests for TUI stream-resume replay after reconnect (IG-DAEM-004 follow-up)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from soothe_cli.tui.app._execution import _ExecutionMixin


class _FakeSession:
    """Stand-in exposing the client-side stream-resume cursor API."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.resume_calls: list[tuple[str, int]] = []
        self.notified: list[tuple[str, int]] = []
        self.resets = 0

    def last_stream_seq(self, loop_id: str) -> int:
        return 7

    async def resume_stream(self, loop_id: str, last_seq: int) -> Any:
        self.resume_calls.append((loop_id, last_seq))
        return self._result

    def _note_stream_seq(self, loop_id: str, seq: int) -> None:
        self.notified.append((loop_id, seq))

    def reset_stream_seq_tracking(self, loop_id: str | None = None) -> None:
        self.resets += 1


class _FakeApp(_ExecutionMixin):
    def __init__(self, session: Any, loop_id: str | None) -> None:
        self._daemon_session = session
        self._lc_loop_id = loop_id
        self.applied: list[Any] = []
        self.mounted: list[Any] = []

    async def _apply_card_wire_frame(self, data: Any) -> bool:
        self.applied.append(data)
        return True

    async def _mount_message(self, message: Any) -> None:
        self.mounted.append(message)


@pytest.mark.asyncio
async def test_replay_applies_card_frames_and_advances_cursor() -> None:
    session = _FakeSession(
        {
            "frames": [
                {"seq": 8, "mode": "custom", "data": {"kind": "card_created"}},
                {"seq": 9, "mode": "custom", "data": {"kind": "card_updated"}},
                {"seq": 10, "mode": "messages", "data": {"content": "text"}},
            ],
            "gap": False,
        }
    )
    app = _FakeApp(session, "loop-a")

    applied = await app._replay_missed_stream_frames()

    assert applied == 2
    assert session.resume_calls == [("loop-a", 7)]
    # Every seq-stamped frame advances the cursor, not just applied cards.
    assert session.notified == [("loop-a", 8), ("loop-a", 9), ("loop-a", 10)]
    assert app.applied == [
        {"kind": "card_created"},
        {"kind": "card_updated"},
    ]


@pytest.mark.asyncio
async def test_replay_noop_without_loop_or_session() -> None:
    app = _FakeApp(_FakeSession({"frames": []}), None)
    assert await app._replay_missed_stream_frames() == 0

    app2 = _FakeApp(None, "loop-a")
    assert await app2._replay_missed_stream_frames() == 0


@pytest.mark.asyncio
async def test_replay_handles_rpc_failure_and_gap() -> None:
    # RPC failure → None → no crash, nothing applied.
    session = _FakeSession(None)
    app = _FakeApp(session, "loop-a")
    assert await app._replay_missed_stream_frames() == 0

    # Gap is tolerated: buffered frames still applied, no crash.
    session2 = _FakeSession({"frames": [{"seq": 8, "mode": "custom", "data": {}}], "gap": True})
    app2 = _FakeApp(session2, "loop-a")
    assert await app2._replay_missed_stream_frames() == 1


@pytest.mark.asyncio
async def test_refresh_daemon_epoch_resets_cursor_on_restart() -> None:
    session = _FakeSession({"frames": []})
    session.fetch_daemon_info = AsyncMock(return_value={"daemon_id": "pid:t1"})
    app = _FakeApp(session, "loop-a")
    app._daemon_epoch = "pid:t0"

    restart = await app._refresh_daemon_epoch()

    assert restart is True
    assert session.resets == 1

    # Same epoch again → no reset.
    await app._refresh_daemon_epoch()
    assert session.resets == 1
