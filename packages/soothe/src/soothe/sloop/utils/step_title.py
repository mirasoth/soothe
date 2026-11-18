"""Short, single-line titles for execution steps."""

from __future__ import annotations

ROOT_STEP_TITLE = "Execute user goal"

STEP_TITLE_PREVIEW_MAX_CHARS = 120
"""Upper bound for step titles carried by progress events and TUI cards."""


def preview_step_title(text: str, *, max_len: int = STEP_TITLE_PREVIEW_MAX_CHARS) -> str:
    """Collapse whitespace and clip a step description for one TUI row."""
    single = " ".join((text or "").split())
    if len(single) <= max_len:
        return single
    return single[: max_len - 1].rstrip() + "…"


__all__ = [
    "ROOT_STEP_TITLE",
    "STEP_TITLE_PREVIEW_MAX_CHARS",
    "preview_step_title",
]
