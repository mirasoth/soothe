"""Host-side cache-aware token-usage breakdown helpers."""

from __future__ import annotations

from typing import Any

_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_tokens",
)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _empty_usage_breakdown() -> dict[str, int]:
    """Return a zeroed breakdown."""
    return {key: 0 for key in _KEYS}


def _usage_dict(usage: dict[str, Any]) -> dict[str, int]:
    """Normalize a LangChain/OpenAI usage mapping into a full breakdown."""
    input_toks = _as_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
    output_toks = _as_int(usage.get("output_tokens") or usage.get("completion_tokens"))
    total_toks = _as_int(usage.get("total_tokens")) or input_toks + output_toks

    cache_read = 0
    cache_creation = 0
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        cache_read = _as_int(details.get("cache_read"))
        cache_creation = _as_int(details.get("cache_creation"))
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cache_read = cache_read or _as_int(prompt_details.get("cached_tokens"))
    cache_read = cache_read or _as_int(usage.get("cache_read_input_tokens"))
    cache_creation = cache_creation or _as_int(usage.get("cache_creation_input_tokens"))

    return {
        "input_tokens": input_toks,
        "output_tokens": output_toks,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "total_tokens": total_toks,
    }


def extract_usage_breakdown(message: Any) -> dict[str, int] | None:
    """Return a full token breakdown from a LangChain message or wire dict, else None."""
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return _usage_dict(usage)

    response = getattr(message, "response_metadata", None)
    if isinstance(response, dict):
        for key in ("token_usage", "usage"):
            nested = response.get(key)
            if isinstance(nested, dict) and nested:
                return _usage_dict(nested)

    if isinstance(message, dict):
        holders = [message]
        data = message.get("data")
        if isinstance(data, dict):
            holders.append(data)
        for holder in holders:
            direct = holder.get("usage_metadata")
            if isinstance(direct, dict) and direct:
                return _usage_dict(direct)
            resp = holder.get("response_metadata")
            if isinstance(resp, dict):
                for key in ("token_usage", "usage"):
                    nested = resp.get(key)
                    if isinstance(nested, dict) and nested:
                        return _usage_dict(nested)
    return None


def merge_usage_breakdown(a: dict[str, int] | None, b: dict[str, int] | None) -> dict[str, int]:
    """Sum two breakdowns field-wise (cache stays a subset of input)."""
    out = _empty_usage_breakdown()
    for source in (a, b):
        if isinstance(source, dict):
            for key in _KEYS:
                out[key] += _as_int(source.get(key))
    return out


class TokenUsageAccumulator:
    """Per-turn breakdown from forwarded stream chunks, deduped by generation id."""

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, int]] = {}
        self._anonymous: list[dict[str, int]] = []

    def observe(self, chunk: Any) -> None:
        """Fold usage from a `(namespace, mode, data)` stream chunk when present."""
        if not isinstance(chunk, (tuple, list)) or len(chunk) != 3:
            return
        _namespace, mode, data = chunk
        if mode != "messages" or not isinstance(data, (tuple, list)) or not data:
            return
        breakdown = extract_usage_breakdown(data[0])
        if breakdown is None:
            return
        message = data[0]
        msg_id = getattr(message, "id", None)
        if msg_id is None and isinstance(message, dict):
            msg_id = message.get("id")
        if msg_id:
            self._by_id[str(msg_id)] = breakdown
        else:
            self._anonymous.append(breakdown)

    def snapshot(self) -> dict[str, int] | None:
        """Return the summed breakdown, or None when no usage was observed."""
        parts = list(self._by_id.values()) + self._anonymous
        if not parts:
            return None
        total = _empty_usage_breakdown()
        for part in parts:
            total = merge_usage_breakdown(total, part)
        return total if any(total.values()) else None


__all__ = [
    "TokenUsageAccumulator",
    "extract_usage_breakdown",
    "merge_usage_breakdown",
]
