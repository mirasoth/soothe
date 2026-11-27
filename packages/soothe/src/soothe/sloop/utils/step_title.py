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


def step_title_with_goal(
    description: str,
    goal_text: str | None,
    *,
    max_len: int = STEP_TITLE_PREVIEW_MAX_CHARS,
) -> str:
    """Compose a step title for the TUI, appending the goal to the root title.

    The generic root step (`description == ROOT_STEP_TITLE`) reads as
    ``Execute user goal: <goal text>`` so the card shows what is being
    executed; any other description passes through unchanged. The result is
    always a one-line preview clipped to ``max_len`` (the TUI clips it
    further to the live terminal width).
    """
    single = " ".join((description or "").split())
    if single != ROOT_STEP_TITLE:
        return preview_step_title(single, max_len=max_len)
    goal = " ".join((goal_text or "").split())
    if not goal:
        return single
    return preview_step_title(f"{single}: {goal}", max_len=max_len)


__all__ = [
    "ROOT_STEP_TITLE",
    "STEP_TITLE_PREVIEW_MAX_CHARS",
    "preview_step_title",
    "step_title_with_goal",
]
