"""Tests for the `stream_resume` RPC handler (IG-DAEM-004 follow-up)."""

from __future__ import annotations

from typing import Any

import pytest

from soothe_daemon.protocol import MessageRouter
from soothe_daemon.query.stream_replay import StreamReplayBuffer


class _FakeEngine:
    def __init__(self) -> None:
        self.buffer = StreamReplayBuffer()
        self.calls: list[tuple[str, int]] = []

    def stream_resume(self, loop_id: str, last_seq: int) -> dict[str, Any]:
        self.calls.append((loop_id, last_seq))
        return self.buffer.resume(loop_id, last_seq)


class _FakeDaemon:
    def __init__(self, engine: _FakeEngine | None) -> None:
        self.sent: list[tuple[Any, dict[str, Any]]] = []
        self._query_engine = engine

    async def _send_client_message(self, client_id: Any, msg: dict[str, Any]) -> None:
        self.sent.append((client_id, msg))


@pytest.mark.asyncio
async def test_stream_resume_replays_frames_after_last_seq() -> None:
    engine = _FakeEngine()
    for seq in (1, 2, 3):
        engine.buffer.record("loop-a", {"type": "event", "seq": seq})
    daemon = _FakeDaemon(engine)
    router = MessageRouter(daemon)

    await router._handle_stream_resume(
        "c1", {"request_id": "r1", "loop_id": "loop-a", "last_seq": 1}
    )

    _, envelope = daemon.sent[0]
    assert envelope["type"] == "response"
    assert envelope["id"] == "r1"
    assert [f["seq"] for f in envelope["result"]["frames"]] == [2, 3]
    assert envelope["result"]["gap"] is False
    assert engine.calls == [("loop-a", 1)]


@pytest.mark.asyncio
async def test_stream_resume_rejects_bad_params() -> None:
    daemon = _FakeDaemon(_FakeEngine())
    router = MessageRouter(daemon)

    bad_params = (
        {"last_seq": 1},
        {"loop_id": "loop-a"},
        {"loop_id": "", "last_seq": 1},
        {"loop_id": "loop-a", "last_seq": -1},
        {"loop_id": "loop-a", "last_seq": "3"},
        {"loop_id": "loop-a", "last_seq": True},
    )
    for bad in bad_params:
        await router._handle_stream_resume("c1", {"request_id": "r", **bad})

    assert len(daemon.sent) == len(bad_params)
    for _, envelope in daemon.sent:
        assert envelope["type"] == "error"


@pytest.mark.asyncio
async def test_stream_resume_when_engine_missing() -> None:
    daemon = _FakeDaemon(None)
    router = MessageRouter(daemon)

    await router._handle_stream_resume(
        "c1", {"request_id": "r", "loop_id": "loop-a", "last_seq": 0}
    )

    _, envelope = daemon.sent[0]
    assert envelope["type"] == "error"
