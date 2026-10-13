"""Tests for early stream chunk filtering (no soothe core dependency)."""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk, ToolMessage

from soothe_cli.runtime.wire.chunk_filter import (
    message_chunk_is_non_actionable,
    should_drop_stream_chunk_early,
    updates_chunk_is_noop,
)


def test_updates_noop_without_interrupt() -> None:
    assert updates_chunk_is_noop({"messages": []}) is True
    assert updates_chunk_is_noop({"__interrupt__": []}) is False


def test_should_drop_empty_ai_chunk() -> None:
    chunk = AIMessageChunk(content="")
    assert should_drop_stream_chunk_early((), "messages", (chunk, {})) is True


def test_should_keep_ai_chunk_with_text() -> None:
    chunk = AIMessageChunk(content="hello")
    assert should_drop_stream_chunk_early((), "messages", (chunk, {})) is False


def test_should_keep_tool_message() -> None:
    chunk = ToolMessage(content="ok", tool_call_id="tc1")
    assert message_chunk_is_non_actionable((chunk, {})) is False


def test_should_keep_ai_with_tool_calls() -> None:
    chunk = AIMessageChunk(content="", tool_calls=[{"name": "edit_file", "id": "tc1", "args": {}}])
    assert message_chunk_is_non_actionable((chunk, {})) is False


def test_should_keep_enveloped_wire_dict_with_text() -> None:
    wire = ({"type": "ai", "data": {"type": "ai", "content": "hello"}}, {})
    assert should_drop_stream_chunk_early((), "messages", wire) is False


def test_should_drop_enveloped_wire_dict_without_text() -> None:
    wire = ({"type": "ai", "data": {"type": "ai", "content": ""}}, {})
    assert should_drop_stream_chunk_early((), "messages", wire) is True


def test_should_keep_usage_only_ai_chunk() -> None:
    chunk = AIMessageChunk(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
    )
    assert should_drop_stream_chunk_early((), "messages", (chunk, {})) is False


def test_should_keep_wire_dict_with_response_metadata_usage() -> None:
    wire = (
        {
            "type": "ai",
            "data": {
                "type": "ai",
                "content": "",
                "response_metadata": {
                    "token_usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 10,
                        "total_tokens": 60,
                    }
                },
            },
        },
        {},
    )
    assert should_drop_stream_chunk_early((), "messages", wire) is False


def test_should_keep_ai_chunk_with_reasoning_content() -> None:
    """Reasoning-only chunks (empty content, no tools) must not be dropped."""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={"reasoning_content": "I should search first."},
    )
    assert should_drop_stream_chunk_early((), "messages", (chunk, {})) is False


def test_should_keep_wire_dict_with_reasoning_content() -> None:
    """Wire dict reasoning-only chunks must not be dropped."""
    wire = (
        {
            "type": "ai",
            "data": {
                "type": "ai",
                "content": "",
                "additional_kwargs": {"reasoning_content": "thinking about the approach"},
            },
        },
        {},
    )
    assert should_drop_stream_chunk_early((), "messages", wire) is False


def test_should_drop_wire_dict_with_empty_reasoning_content() -> None:
    """Empty reasoning_content string should still be dropped."""
    wire = (
        {
            "type": "ai",
            "data": {
                "type": "ai",
                "content": "",
                "additional_kwargs": {"reasoning_content": ""},
            },
        },
        {},
    )
    assert should_drop_stream_chunk_early((), "messages", wire) is True
