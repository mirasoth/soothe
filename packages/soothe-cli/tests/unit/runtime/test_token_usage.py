"""Tests for stream token usage extraction and estimation."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from soothe_cli.runtime.token_usage import (
    TokenUsage,
    coerce_total_tokens_used,
    extract_stream_message_token_usage,
    message_has_token_usage_metadata,
)


def test_coerce_total_tokens_used_parses_and_clamps() -> None:
    assert coerce_total_tokens_used("42") == 42
    assert coerce_total_tokens_used(-5) == 0
    assert coerce_total_tokens_used("bad") == 0


def test_extract_usage_metadata_split_counts() -> None:
    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
    )
    assert extract_stream_message_token_usage(message) == TokenUsage(100, 25, 0, 0, 125)


def test_extract_response_metadata_openai_shape() -> None:
    message = AIMessage(
        content="hi",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 512,
                "completion_tokens": 64,
                "total_tokens": 576,
            }
        },
    )
    assert extract_stream_message_token_usage(message) == TokenUsage(512, 64, 0, 0, 576)


def test_extract_wire_dict_response_metadata_enveloped() -> None:
    wire = {
        "type": "ai",
        "data": {
            "type": "ai",
            "content": "",
            "response_metadata": {
                "token_usage": {"prompt_tokens": 200, "completion_tokens": 10, "total_tokens": 210}
            },
        },
    }
    assert extract_stream_message_token_usage(wire) == TokenUsage(200, 10, 0, 0, 210)


def test_extract_cache_tokens_langchain_details() -> None:
    message = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 80, "cache_creation": 5},
        },
    )
    usage = extract_stream_message_token_usage(message)
    assert usage.cache_read_tokens == 80
    assert usage.cache_creation_tokens == 5
    assert usage.total_tokens == 120  # cache stays a subset of input, not re-added


def test_extract_cache_tokens_openai_cached_tokens() -> None:
    wire = {
        "type": "ai",
        "response_metadata": {
            "token_usage": {
                "prompt_tokens": 50,
                "completion_tokens": 4,
                "total_tokens": 54,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        },
    }
    usage = extract_stream_message_token_usage(wire)
    assert usage.cache_read_tokens == 40
    assert usage.total_tokens == 54


def test_total_only_usage_keeps_total_not_input() -> None:
    wire = {"type": "ai", "usage_metadata": {"total_tokens": 77}}
    usage = extract_stream_message_token_usage(wire)
    assert usage == TokenUsage(0, 0, 0, 0, 77)
    assert usage.has_any()


def test_token_usage_has_any() -> None:
    assert TokenUsage(10, 2, 5, 1, 12).has_any()
    assert not TokenUsage().has_any()


def test_message_has_token_usage_metadata() -> None:
    assert message_has_token_usage_metadata({"type": "ai", "content": ""}) is False
    assert (
        message_has_token_usage_metadata(
            {
                "type": "ai",
                "usage_metadata": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }
        )
        is True
    )
    assert (
        message_has_token_usage_metadata(
            {"type": "ai", "usage_metadata": {"input_token_details": {"cache_read": 9}}}
        )
        is True
    )
