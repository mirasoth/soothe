"""Single-line text previews shared by TUI display widgets."""

from __future__ import annotations

from soothe_cli.display.tool_display import compact_arg_text, truncate_to_width


def preview_line(
    text: object,
    *,
    max_chars: int | None = None,
    max_cols: int | None = None,
    reserved_cols: int = 0,
    ellipsis: str = "…",
) -> str:
    """Collapse whitespace and fit text into one row.

    Args:
        text: Source text; non-strings are coerced for display.
        max_chars: Character budget, used when no column budget is given.
        max_cols: Terminal column budget; takes precedence over `max_chars`.
        reserved_cols: Columns already occupied by prefixes or status suffixes.
        ellipsis: Truncation marker counted against the active budget.

    Returns:
        A single-line preview that fits the requested budget.
    """
    single = compact_arg_text(str(text or ""))
    if max_cols is not None:
        return truncate_to_width(
            single,
            max(1, max_cols - reserved_cols),
            ellipsis=ellipsis,
        )
    if max_chars is None or len(single) <= max_chars:
        return single
    if max_chars <= len(ellipsis):
        return single[:max_chars]
    return single[: max_chars - len(ellipsis)].rstrip() + ellipsis


__all__ = ["preview_line"]
