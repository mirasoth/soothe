"""Coalescer token-usage preservation tests."""

from __future__ import annotations

from soothe_daemon.query.stream_delivery import StreamDeliveryCoalescer, _merge_usage_metadata


def _ai_chunk(content: str, usage: dict | None = None, *, last: bool = False) -> tuple:
    body: dict = {"type": "AIMessageChunk", "content": content, "id": "g1"}
    if usage is not None:
        body["usage_metadata"] = usage
    if last:
        body["chunk_position"] = "last"
    return ((), "messages", (body, {}))


def _usage_frames(outputs: list) -> list[dict]:
    frames = []
    for _ns, mode, data in outputs:
        if mode != "messages" or not isinstance(data, (tuple, list)) or not data:
            continue
        body = data[0]
        if isinstance(body, dict) and body.get("usage_metadata"):
            frames.append(body["usage_metadata"])
    return frames


def test_merge_usage_metadata_sums_disjoint_deltas() -> None:
    a = {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}
    b = {"input_tokens": 0, "output_tokens": 4, "total_tokens": 4}
    merged = _merge_usage_metadata(a, b)
    assert merged["input_tokens"] == 10
    assert merged["output_tokens"] == 4
    assert merged["total_tokens"] == 14


def test_merge_usage_metadata_accumulates_cache_details() -> None:
    merged = _merge_usage_metadata(
        {"input_tokens": 5, "input_token_details": {"cache_read": 5, "cache_creation": 1}},
        {"input_tokens": 3, "input_token_details": {"cache_read": 2}},
    )
    assert merged["input_tokens"] == 8
    assert merged["input_token_details"] == {"cache_read": 7, "cache_creation": 1}


def test_text_chunks_sum_usage_instead_of_last_wins() -> None:
    c = StreamDeliveryCoalescer("streaming")
    first = c.ingest(
        *_ai_chunk("Hello ", {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10})
    )
    second = c.ingest(
        *_ai_chunk(
            "world",
            {"input_tokens": 0, "output_tokens": 5, "total_tokens": 5},
            last=True,
        )
    )
    usages = _usage_frames(first + second)
    assert any(
        u["input_tokens"] == 10 and u["output_tokens"] == 5 and u["total_tokens"] == 15
        for u in usages
    ), usages


def test_usage_only_chunk_passes_through_instantly() -> None:
    c = StreamDeliveryCoalescer("streaming")
    out = c.ingest(
        *_ai_chunk(
            "",
            {
                "input_tokens": 7,
                "output_tokens": 2,
                "total_tokens": 9,
                "input_token_details": {"cache_read": 6},
            },
        )
    )
    usages = _usage_frames(out)
    assert len(usages) == 1
    assert usages[0]["input_token_details"]["cache_read"] == 6


def test_tool_invocation_frame_with_usage_is_not_dropped() -> None:
    body = {
        "type": "AIMessageChunk",
        "content": "",
        "id": "g9",
        "tool_call_chunks": [{"name": "read_file", "args": "{}", "id": "call_1", "index": 0}],
        "usage_metadata": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    }
    c = StreamDeliveryCoalescer("streaming")
    out = c.ingest((), "messages", (body, {}))
    assert _usage_frames(out), out
