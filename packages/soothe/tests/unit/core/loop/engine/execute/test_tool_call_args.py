"""Unit tests for tool-call kwargs collection during Act streaming."""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from soothe.sloop.engine.execute.tool_call_args import (
    ToolCallArgsCollector,
    enrich_wire_updates_with_collector,
    format_args_for_log,
    format_todos_for_log,
)


def test_format_args_for_log_truncates_long_payload() -> None:
    args = {"command": "x" * 600}
    preview = format_args_for_log(args, max_chars=80)
    assert preview.endswith("...")
    assert len(preview) <= 83


def test_format_todos_for_log_renders_status_and_content() -> None:
    todos = [
        {"content": "Survey docs", "status": "in_progress"},
        {"content": "Fix errors", "status": "pending"},
    ]
    text = format_todos_for_log(todos)
    assert "1. [in_progress] Survey docs" in text
    assert "2. [pending] Fix errors" in text


def test_format_todos_for_log_empty_list() -> None:
    assert format_todos_for_log([]) == "(empty)"


def test_record_ai_pair_backfills_from_chunks() -> None:
    msg = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "grep", "args": {}}],
        tool_call_chunks=[
            {
                "id": "call_1",
                "name": "grep",
                "args": '{"pattern": "foo", "path": "."}',
            }
        ],
    )
    collector = ToolCallArgsCollector()
    collector.record_ai_pair(msg, msg)
    assert collector.lookup("call_1") == {"pattern": "foo", "path": "."}


def test_record_ai_pair_maps_index_only_chunk_to_unified_id() -> None:
    """Index-only streaming chunks must be keyed by unified id for ToolMessage lookup."""
    msg = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "index": 7,
                "name": "edit_file",
                "args": '{"file_path": "README.md", "old_string": "a", "new_string": "b"}',
            },
        ],
    )
    collector = ToolCallArgsCollector()
    collector.record_ai_pair(msg, msg, step_id="YJH-01")
    assert collector.lookup("YJH_01:s:edit_file:7") == {
        "file_path": "README.md",
        "old_string": "a",
        "new_string": "b",
    }


def test_enrich_wire_updates_hydrates_from_collector() -> None:
    collector = ToolCallArgsCollector()
    collector.by_id["EMV_01:s:wizsearch_search:0"] = {"query": "world cup teams"}
    updates = [
        {
            "type": "soothe.stream.tool_call.update",
            "tool_call_id": "EMV_01:s:wizsearch_search:0",
            "name": "wizsearch_search",
            "args": {},
        }
    ]
    enriched = enrich_wire_updates_with_collector(updates, collector)
    assert enriched[0]["args"] == {"query": "world cup teams"}


# ---------------------------------------------------------------------------
# Partial-arg wire streaming (ToolCallWireStreamTracker)
# ---------------------------------------------------------------------------


def test_recover_partial_tool_call_args_extracts_closed_pairs() -> None:
    from soothe.sloop.engine.execute.tool_call_args import recover_partial_tool_call_args

    assert recover_partial_tool_call_args("") == {}
    assert recover_partial_tool_call_args('{"file_path": "a.txt",') == {"file_path": "a.txt"}
    # A closed pair followed by an open value: the closed one plus the
    # still-growing value (live display mirrors the stream).
    assert recover_partial_tool_call_args('{"path": "/x", "content": "he') == {
        "path": "/x",
        "content": "he",
    }
    # Escaped quotes inside a closed value survive.
    assert recover_partial_tool_call_args('{"q": "a\\"b",') == {"q": 'a"b'}


def test_tracker_emits_partial_updates_only_when_recoverable() -> None:
    from soothe.sloop.engine.execute.tool_call_args import ToolCallWireStreamTracker

    tracker = ToolCallWireStreamTracker()
    first = AIMessageChunk(
        content="",
        tool_call_chunks=[{"id": "c1", "name": "write_file", "args": ""}],
    )
    # Name-only first chunk: nothing recoverable yet.
    assert tracker.partial_updates_from_message(first) == []

    growing = AIMessageChunk(
        content="",
        tool_call_chunks=[{"id": "c1", "name": "", "args": '{"file_path": "a.txt",'}],
    )
    out = tracker.partial_updates_from_message(growing)
    assert len(out) == 1
    assert out[0]["type"] == "soothe.stream.tool_call.update"
    assert out[0]["partial"] is True
    assert out[0]["tool_call_id"] == "c1"
    assert out[0]["name"] == "write_file"
    assert out[0]["args"] == {"file_path": "a.txt"}


def test_tracker_partial_updates_are_rate_limited_per_call(monkeypatch) -> None:
    from soothe.sloop.engine.execute import tool_call_args as tca
    from soothe.sloop.engine.execute.tool_call_args import ToolCallWireStreamTracker

    monkeypatch.setattr(tca, "WIRE_TOOL_UPDATE_MIN_INTERVAL_S", 0.0)
    tracker = ToolCallWireStreamTracker()
    tracker.partial_updates_from_message(
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"id": "c1", "name": "write_file", "args": '{"a": "1",'}],
        )
    )
    # Immediately re-emitted with richer args: interval 0 → allowed.
    out = tracker.partial_updates_from_message(
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"id": "c1", "name": "", "args": '"b": "2"'}],
        )
    )
    assert len(out) == 1
    assert out[0]["args"] == {"a": "1", "b": "2"}


def test_tracker_partial_updates_throttled_within_interval() -> None:
    from soothe.sloop.engine.execute.tool_call_args import ToolCallWireStreamTracker

    tracker = ToolCallWireStreamTracker()
    tracker.partial_updates_from_message(
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"id": "c1", "name": "write_file", "args": '{"a": "1",'}],
        )
    )
    # Growth within one interval window is suppressed (token-rate flood guard).
    out = tracker.partial_updates_from_message(
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"id": "c1", "name": "", "args": '"b": "2"'}],
        )
    )
    assert out == []


def test_tracker_skips_dict_args_complete_json() -> None:
    """Complete-JSON entries (dict-args providers) skip the partial path."""
    from soothe.sloop.engine.execute.tool_call_args import ToolCallWireStreamTracker

    tracker = ToolCallWireStreamTracker()
    # Dict-args providers mark the entry complete inside accumulate; seed it
    # directly (AIMessageChunk validation only allows string chunk args).
    tracker.pending["c1"] = {
        "name": "grep",
        "args_str": '{"pattern": "x"}',
        "is_complete_json": True,
        "emitted": False,
        "is_main": True,
    }
    out = tracker.partial_updates_from_message(
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"id": "c1", "name": "", "args": ""}],
        )
    )
    assert out == []


def test_tracker_ignores_plain_text_messages() -> None:
    from soothe.sloop.engine.execute.tool_call_args import ToolCallWireStreamTracker

    tracker = ToolCallWireStreamTracker()
    assert tracker.partial_updates_from_message(AIMessage(content="hello")) == []
