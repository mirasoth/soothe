"""Tests for the per-loop card manager (real-time binding + DB ledger)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from soothe_nano.core.events import (
    CARD_CREATED,
    CARD_REPLAY_BEGIN,
    CARD_REPLAY_END,
    CARD_UPDATED,
)

from soothe_daemon.display.loop_card_manager import (
    LoopCardManager,
    _BindingBuffers,
)


async def _seed_messages(
    manager: LoopCardManager,
    loop_id: str,
    messages: list,
    *,
    log_events: list[dict] | None = None,
) -> None:
    state = manager._buffers[loop_id]  # noqa: SLF001
    state.messages = list(messages)
    state.log_events = list(log_events or [])
    await manager._flush_buffers_to_ledger(loop_id, state)  # noqa: SLF001


@pytest.mark.asyncio
async def test_record_user_prompt_persists_user_card(isolated_display_db) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    await manager.record_user_prompt("loop_a", "hello")
    ledger = await manager.ensure_for_loop("loop_a")
    assert [card.content for card in ledger.snapshot()] == ["hello"]
    assert isolated_display_db.peek_user_prompt("loop_a") == "hello"


@pytest.mark.asyncio
async def test_peek_latest_assistant_response_returns_latest_card(isolated_display_db) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(
        manager,
        "loop_latest_ai",
        [
            HumanMessage(content="question"),
            AIMessage(content="first answer"),
            AIMessage(content="latest answer"),
        ],
    )
    assert isolated_display_db.peek_latest_assistant_response("loop_latest_ai") == "latest answer"


@pytest.mark.asyncio
async def test_bind_messages_from_checkpoint_messages() -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(
        manager,
        "loop_b",
        [HumanMessage(content="from log"), AIMessage(content="log reply")],
    )
    ledger = await manager.ensure_for_loop("loop_b")
    assert [card.content for card in ledger.snapshot()] == ["from log", "log reply"]


@pytest.mark.asyncio
async def test_bind_messages_from_activity_log() -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    activity = [
        {
            "kind": "conversation",
            "role": "user",
            "content": "from log",
            "timestamp": "2026-06-04T10:00:00+00:00",
        },
        {
            "kind": "conversation",
            "role": "assistant",
            "content": "log reply",
            "timestamp": "2026-06-04T10:00:01+00:00",
        },
    ]
    await _seed_messages(manager, "loop_c", [], log_events=activity)
    ledger = await manager.ensure_for_loop("loop_c")
    assert [card.content for card in ledger.snapshot()] == ["from log", "log reply"]


@pytest.mark.asyncio
async def test_ensure_for_loop_returns_empty_when_no_data(isolated_display_db) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    ledger = await manager.ensure_for_loop("loop_empty")
    assert ledger.card_count() == 0
    assert isolated_display_db.list_mutations("loop_empty")[0].op == "header"


@pytest.mark.asyncio
async def test_replay_to_client_empty_loop() -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    sent: list[dict] = []

    async def collect(frame: dict) -> None:
        sent.append(frame)

    total = await manager.replay_to_client("loop_fast_replay", collect)
    assert total == 0
    assert sent[0]["type"] == CARD_REPLAY_BEGIN
    assert sent[-1]["type"] == CARD_REPLAY_END
    assert sent[0]["total_cards"] == 0


@pytest.mark.asyncio
async def test_replay_to_client_emits_begin_created_end() -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    messages = [
        HumanMessage(content="q"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "read_file", "args": {"path": "/x"}}],
        ),
        ToolMessage(
            content="contents",
            tool_call_id="tc1",
            name="read_file",
            status="success",
        ),
        AIMessage(content="here you go"),
    ]
    await _seed_messages(manager, "loop_d", messages)

    sent: list[dict] = []

    async def collect(frame: dict) -> None:
        sent.append(frame)

    total = await manager.replay_to_client("loop_d", collect)
    types = [frame["type"] for frame in sent]
    assert types[0] == CARD_REPLAY_BEGIN
    assert types[-1] == CARD_REPLAY_END
    middle = types[1:-1]
    assert all(item == CARD_CREATED for item in middle)
    assert len(middle) == total


@pytest.mark.asyncio
async def test_replay_to_client_reads_persisted_cards() -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(
        manager,
        "loop_refresh",
        [HumanMessage(content="turn1"), AIMessage(content="final goal completion response")],
    )

    sent: list[dict] = []

    async def collect(frame: dict) -> None:
        sent.append(frame)

    await manager.replay_to_client("loop_refresh", collect)
    replay_texts = [
        str(frame.get("data", {}).get("content", ""))
        for frame in sent
        if frame.get("type") == CARD_CREATED
    ]
    assert any("final goal completion response" in text for text in replay_texts)


@pytest.mark.asyncio
async def test_live_cognition_custom_events_bind_to_ledger(isolated_display_db) -> None:
    """Flat live ``soothe.cognition.*`` customs must project intent/plan cards."""
    manager = LoopCardManager(
        SimpleNamespace(_runner=MagicMock()),
        flush_debounce_ms=0,
    )
    await manager.ingest_stream_tuple(
        "loop_cognition",
        (),
        "custom",
        {
            "type": "soothe.cognition.intent.classified",
            "reasoning": "I'll map the repository layout first.",
            "intent_type": "agentic",
        },
    )
    await manager.ingest_stream_tuple(
        "loop_cognition",
        (),
        "custom",
        {
            "type": "soothe.cognition.strange_loop.reasoned",
            "status": "continue",
            "iteration": 1,
            "plan_action": "new",
            "assessment_reasoning": "Need structure discovery.",
            "plan_reasoning": "Scan packages and config, then summarize architecture.",
        },
    )
    await asyncio.sleep(0.05)
    ledger = await manager.ensure_for_loop("loop_cognition")
    cards = ledger.snapshot()
    reason_cards = [c for c in cards if c.type.value == "cognition_reason"]
    assert len(reason_cards) >= 2
    texts = " ".join(
        f"{c.cognition_plan_strategy or ''} {c.cognition_plan_assessment or ''}"
        for c in reason_cards
    )
    assert "map the repository" in texts
    assert "Scan packages" in texts or "structure discovery" in texts
    await manager.stop_for_loop("loop_cognition")


@pytest.mark.asyncio
async def test_debounced_flush_coalesces_rapid_ingests(monkeypatch) -> None:
    """Multiple stream frames within the debounce window produce one bind pass."""
    manager = LoopCardManager(
        SimpleNamespace(_runner=MagicMock()),
        flush_debounce_ms=100,
    )
    flush_count = 0
    original_flush = manager._flush_buffers_to_ledger

    async def counting_flush(loop_id: str, state: _BindingBuffers) -> None:
        nonlocal flush_count
        flush_count += 1
        await original_flush(loop_id, state)

    monkeypatch.setattr(manager, "_flush_buffers_to_ledger", counting_flush)

    custom = {"kind": "conversation", "role": "assistant", "content": "a"}
    for _ in range(8):
        await manager.ingest_stream_tuple("loop_debounce", (), "custom", dict(custom))
    await asyncio.sleep(0.05)
    assert flush_count == 0
    await asyncio.sleep(0.15)
    assert flush_count == 1
    await manager.stop_for_loop("loop_debounce")


@pytest.mark.asyncio
async def test_ingest_stream_tuple_returns_before_flush_completes(monkeypatch) -> None:
    """Stream ingest must not block on ledger flush."""
    manager = LoopCardManager(
        SimpleNamespace(_runner=MagicMock()),
        flush_debounce_ms=50,
    )
    flush_started = asyncio.Event()

    original_flush = manager._flush_buffers_to_ledger

    async def slow_flush(loop_id: str, state: _BindingBuffers) -> None:
        flush_started.set()
        await asyncio.sleep(0.15)
        await original_flush(loop_id, state)

    monkeypatch.setattr(manager, "_flush_buffers_to_ledger", slow_flush)

    wire = {"type": "ai", "content": "hello", "chunk_position": "last"}
    await manager.ingest_stream_tuple("loop_async", (), "messages", (wire, {}))
    assert not flush_started.is_set()
    await asyncio.wait_for(flush_started.wait(), timeout=1.0)
    # Wait deterministically for the real flush (thread-pool bind + persist)
    # to land the card in the ledger, instead of a fixed sleep that flakes
    # under CI runner load.
    ledger = await manager.ensure_for_loop("loop_async")
    for _ in range(100):
        if ledger.card_count() >= 1:
            break
        await asyncio.sleep(0.02)
    assert ledger.card_count() >= 1
    await manager.stop_for_loop("loop_async")


@pytest.mark.asyncio
async def test_stop_for_loop_releases_in_memory_state(isolated_display_db) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(manager, "loop_e", [HumanMessage(content="x")])
    assert "loop_e" in manager._ledgers  # noqa: SLF001

    await manager.stop_for_loop("loop_e")
    assert "loop_e" not in manager._ledgers  # noqa: SLF001
    assert isolated_display_db.list_mutations("loop_e")


@pytest.mark.asyncio
async def test_ingest_overflow_preserves_frames(monkeypatch) -> None:
    """Overflow deque must not drop frames when the bounded queue is full."""
    manager = LoopCardManager(
        SimpleNamespace(_runner=MagicMock()),
        ingest_queue_maxsize=2,
        flush_debounce_ms=0,
    )
    processed: list[Any] = []
    original = manager._ingest_stream_tuple_now

    async def record(loop_id: str, namespace, mode, data) -> None:
        processed.append(data)
        await original(loop_id, namespace, mode, data)

    monkeypatch.setattr(manager, "_ingest_stream_tuple_now", record)

    custom = {"kind": "conversation", "role": "assistant", "content": "x"}
    for i in range(5):
        await manager.ingest_stream_tuple(
            "loop_overflow",
            (),
            "custom",
            {**custom, "content": f"msg-{i}"},
        )
    await asyncio.sleep(0.3)
    assert len(processed) == 5
    worker = manager._ingest_workers.get("loop_overflow")  # noqa: SLF001
    assert worker is not None
    assert len(worker.overflow) == 0
    await manager.stop_for_loop("loop_overflow")


@pytest.mark.asyncio
async def test_overflow_emits_stream_degraded(monkeypatch) -> None:
    """First overflow frame notifies clients via stream_degraded (RFC-450)."""
    from soothe_daemon.display.loop_card_manager import (
        reset_card_ingest_overflow_metrics,
    )

    reset_card_ingest_overflow_metrics()
    broadcasts: list[dict] = []

    async def capture(msg: dict) -> None:
        broadcasts.append(msg)

    manager = LoopCardManager(
        SimpleNamespace(_broadcast=capture),
        ingest_queue_maxsize=1,
        flush_debounce_ms=0,
    )
    custom = {"kind": "conversation", "role": "assistant", "content": "a"}
    await manager.ingest_stream_tuple("loop_sd", (), "custom", dict(custom))
    await manager.ingest_stream_tuple("loop_sd", (), "custom", {**custom, "content": "b"})
    await asyncio.sleep(0.05)
    assert any(
        m.get("mode") == "custom" and (m.get("data") or {}).get("type") == "stream_degraded"
        for m in broadcasts
    )
    await manager.stop_for_loop("loop_sd")
    reset_card_ingest_overflow_metrics()


@pytest.mark.asyncio
async def test_stop_for_loop_cancels_ingest_worker() -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await manager.ingest_stream_tuple(
        "loop_worker",
        (),
        "custom",
        {"kind": "conversation", "role": "assistant", "content": "hi"},
    )
    assert "loop_worker" in manager._ingest_workers  # noqa: SLF001
    await manager.stop_for_loop("loop_worker")
    assert "loop_worker" not in manager._ingest_workers  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_for_loop_without_runner_still_opens_ledger(isolated_display_db) -> None:
    manager = LoopCardManager(SimpleNamespace(_runner=None))
    ledger = await manager.ensure_for_loop("loop_no_runner")
    assert ledger.card_count() == 0
    assert isolated_display_db.list_mutations("loop_no_runner")


@pytest.mark.asyncio
async def test_commit_goal_boundary_preserves_report_across_reload(
    isolated_display_db,
) -> None:
    """The boundary marker carries the report; a fresh manager replays it."""
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(
        manager,
        "loop_boundary",
        [
            HumanMessage(content="count files"),
            AIMessage(content="partial report", phase="goal_completion"),
        ],
    )
    await manager.commit_goal_boundary(
        "loop_boundary",
        goal_text="count files",
        goal_completion="**Total file count in packages: 3632**",
    )

    # Fresh manager = cold read from the persisted mutation log.
    resumed = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    payload = await resumed.fetch_loop_history("loop_boundary")

    [goal] = payload["goals"]
    assert goal["goal_text"] == "count files"
    assert goal["status"] == "completed"
    assert goal["goal_completion"] == "**Total file count in packages: 3632**"
    cards = goal["display_cards"]
    last = cards[-1]
    assert str(last["type"]) == "assistant"
    assert last["content"] == "**Total file count in packages: 3632**"
    assert last["loop_output_phase"] == "goal_completion"
    # A truncated streaming report card is replaced, not supplemented.
    assert "partial report" not in [str(c.get("content")) for c in cards]


@pytest.mark.asyncio
async def test_fetch_loop_history_segments_goals_and_keeps_live_tail(
    isolated_display_db,
) -> None:
    """Multiple boundaries split the transcript; the live tail stays separate."""
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(
        manager,
        "loop_multi",
        [
            HumanMessage(content="how are u"),
            AIMessage(content="Hey there!"),
        ],
    )
    await manager.commit_goal_boundary(
        "loop_multi",
        goal_text="how are u",
        goal_completion="Hey there!",
    )
    await _seed_messages(
        manager,
        "loop_multi",
        [
            HumanMessage(content="shanghai weather"),
            AIMessage(content="It is rainy."),
        ],
    )

    payload = await manager.fetch_loop_history("loop_multi")

    goals = payload["goals"]
    assert len(goals) == 1
    goal_texts = " ".join(str(c.get("content")) for c in goals[0]["display_cards"])
    assert "Hey there!" in goal_texts
    assert "rainy" not in goal_texts
    live_texts = " ".join(str(c.get("content")) for c in payload["live_cards"])
    assert "shanghai weather" in live_texts
    assert "It is rainy." in live_texts

    # Second goal completes → two segments, live tail empty.
    await manager.commit_goal_boundary(
        "loop_multi",
        goal_text="shanghai weather",
        goal_completion="It is rainy.",
    )
    payload = await manager.fetch_loop_history("loop_multi")
    assert len(payload["goals"]) == 2
    assert payload["goals"][1]["goal_completion"] == "It is rainy."
    assert payload["live_cards"] == []


@pytest.mark.asyncio
async def test_commit_goal_boundary_survives_ingest_race(
    isolated_display_db,
    monkeypatch,
) -> None:
    """Report invariant holds even when the final chunks never bind to cards."""
    manager = LoopCardManager(
        SimpleNamespace(_runner=MagicMock()),
        flush_debounce_ms=10_000,  # debounce far beyond the test window
    )

    # Simulate the ingest pipeline stalling past the boundary: drain is a
    # no-op so nothing reaches the ledger before the marker commits.
    async def _no_drain(loop_id: str, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_drain_ingest", _no_drain)
    wire = {"type": "human", "content": "deploy chirpy"}
    await manager.ingest_stream_tuple("loop_race", (), "messages", (wire, {}))
    await manager.commit_goal_boundary(
        "loop_race",
        goal_text="deploy chirpy",
        goal_completion="I deployed and verified the Chirpy Helm stack.",
    )

    payload = await manager.fetch_loop_history("loop_race")

    [goal] = payload["goals"]
    cards = goal["display_cards"]
    assert str(cards[0]["type"]) == "user"
    assert cards[0]["content"] == "deploy chirpy"
    last = cards[-1]
    assert str(last["type"]) == "assistant"
    assert last["content"] == "I deployed and verified the Chirpy Helm stack."
    await manager.stop_for_loop("loop_race")


@pytest.mark.asyncio
async def test_cancelled_goal_boundary_replays_without_report_card(
    isolated_display_db,
) -> None:
    """Cancelled boundaries carry no completion body; no card is synthesized."""
    manager = LoopCardManager(SimpleNamespace(_runner=MagicMock()))
    await _seed_messages(
        manager,
        "loop_cancelled",
        [
            HumanMessage(content="stop this"),
            AIMessage(content="working on it"),
        ],
    )
    await manager.commit_goal_boundary(
        "loop_cancelled",
        goal_text="stop this",
        goal_completion="",
        status="cancelled",
    )

    payload = await manager.fetch_loop_history("loop_cancelled")

    [goal] = payload["goals"]
    assert goal["status"] == "cancelled"
    assert goal["goal_completion"] == ""
    contents = [str(c.get("content")) for c in goal["display_cards"]]
    assert "stop this" in contents
    assert "working on it" in contents


@pytest.mark.asyncio
async def test_second_flush_appends_update_and_broadcasts_card_frames(
    isolated_display_db,
) -> None:
    """subsequent binds append updates instead of replace_with wipe."""
    broadcasted: list[dict[str, Any]] = []

    async def _broadcast(msg: dict[str, Any]) -> None:
        broadcasted.append(msg)

    daemon = SimpleNamespace(_runner=MagicMock(), _broadcast=_broadcast)
    manager = LoopCardManager(daemon)

    await _seed_messages(
        manager,
        "loop_append",
        [HumanMessage(content="q"), AIMessage(content="hel")],
    )
    first_count = len(isolated_display_db.list_mutations("loop_append"))
    assert first_count >= 2  # header + creates

    # Grow assistant content — stable asst ordinal key should emit update.
    await _seed_messages(
        manager,
        "loop_append",
        [HumanMessage(content="q"), AIMessage(content="hello world")],
    )
    mutations = isolated_display_db.list_mutations("loop_append")
    ops = [m.op for m in mutations]
    assert "update" in ops
    assert ops.count("header") == 1

    card_frames = [
        f
        for f in broadcasted
        if f.get("type") == "event"
        and isinstance(f.get("data"), dict)
        and str(f["data"].get("type", "")).startswith("soothe.card.")
    ]
    assert card_frames
    assert any(f["data"]["type"] == CARD_CREATED for f in card_frames)
    assert any(f["data"]["type"] == CARD_UPDATED for f in card_frames)

    ledger = await manager.ensure_for_loop("loop_append")
    texts = [c.content for c in ledger.snapshot() if c.type.value == "assistant"]
    assert texts and texts[-1] == "hello world"


if __name__ == "__main__":  # pragma: no cover - convenience
    sys.exit(pytest.main([__file__, "-v"]))
