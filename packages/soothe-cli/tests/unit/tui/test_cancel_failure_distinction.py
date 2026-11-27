"""Cancel vs step-failure status distinction (wording, glyphs, tones, colors).

User-initiated cancels and stream stops render in the lavender "cancelled"
family (``colors.secondary`` + the stopped glyph), while genuine step
failures render in the violet "failure" family (``colors.card_error`` +
the error glyph). The two must never share wording, glyph, or color.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from soothe_cli.display import theme
from soothe_cli.settings import get_glyphs
from soothe_cli.tui.widgets.messages import CognitionStepMessage
from soothe_cli.tui.widgets.messages.cognition_goal_tree import (
    CognitionGoalTreeMessage,
)
from soothe_cli.tui.widgets.messages.cognition_step_activity import (
    StepCardStatusLine,
)


def _running_goal_tree(goal_id: str) -> CognitionGoalTreeMessage:
    tree = CognitionGoalTreeMessage(goal="analyze logs", id=goal_id)
    tree.mark_loop_started()
    tree.sync_plan_steps([{"id": "S-01", "description": "scan soothe.log"}])
    tree.set_step_phase("S-01", "running")
    return tree


def _span_styles(content: object) -> list[str]:
    return [str(span.style) for span in content.spans]  # type: ignore[attr-defined]


def test_goal_tree_cancel_footer_uses_cancelled_family() -> None:
    tree = _running_goal_tree("gt-cancel")
    tree.set_interrupted("Cancelled by user")

    assert tree._footer_tone == "cancelled"
    assert tree._goal_tree_status() == "interrupted"

    content = tree._goal_footer_styled_content()
    g = get_glyphs()
    assert g.interrupted in content.plain
    assert g.error not in content.plain
    assert "Cancelled by user" in content.plain
    assert theme.DARK_COLORS.secondary in _span_styles(content)
    assert theme.DARK_COLORS.card_error not in _span_styles(content)


def test_goal_tree_failure_footer_uses_failure_family() -> None:
    tree = CognitionGoalTreeMessage(goal="analyze logs", id="gt-fail")
    tree.set_loop_finished(
        status="failed",
        goal_progress="low",
        completion_summary="boom",
        total_steps=1,
    )

    assert tree._footer_tone == "error"
    assert tree._goal_tree_status() == "error"

    content = tree._goal_footer_styled_content()
    g = get_glyphs()
    assert g.error in content.plain
    assert g.interrupted not in content.plain
    assert theme.DARK_COLORS.card_error in _span_styles(content)
    assert theme.DARK_COLORS.secondary not in _span_styles(content)


def test_step_card_interrupt_line_uses_stopped_glyph_and_lavender() -> None:
    step = CognitionStepMessage(step_id="s1", description="Run", id="stp-cancel")
    step._status_widget = MagicMock()

    step.set_interrupted("Cancelled by user")

    content = step._status_widget.update.call_args.args[0]
    g = get_glyphs()
    assert g.interrupted in content.plain
    assert g.error not in content.plain
    assert "Cancelled by user" in content.plain
    assert theme.DARK_COLORS.secondary in _span_styles(content)


def test_step_card_failure_detail_uses_violet_not_lavender() -> None:
    step = CognitionStepMessage(step_id="s2", description="Run", id="stp-fail")
    step._status_widget = MagicMock()
    step._detail_widget = MagicMock()

    step.set_complete(False, 1200, 0, "TypeError: boom")

    content = step._detail_widget.update.call_args.args[0]
    g = get_glyphs()
    assert g.error in content.plain
    assert g.interrupted not in content.plain
    assert theme.DARK_COLORS.card_error in _span_styles(content)
    assert theme.DARK_COLORS.secondary not in _span_styles(content)


def test_step_failure_footer_includes_clipped_reason() -> None:
    colors = theme.DARK_COLORS
    g = get_glyphs()
    content = StepCardStatusLine.footer_completed(
        gutter="",
        icon=g.error,
        head="Failed · 1.2s",
        suffix=" · TypeError: boom",
        success=False,
        colors=colors,
    )
    assert content.plain.startswith(f"{g.error} Failed · 1.2s")
    assert "TypeError: boom" in content.plain
    assert colors.card_error in _span_styles(content)
    assert colors.secondary not in _span_styles(content)


def test_failure_reason_suffix_clips_and_suppresses_generic_text() -> None:
    step = CognitionStepMessage(step_id="s3", description="Run", id="stp-suffix")

    step._last_summary = "TypeError: boom\n  stack line"
    assert step._failure_reason_suffix() == " · TypeError: boom"

    step._last_summary = ""
    assert step._failure_reason_suffix() == ""

    step._last_summary = "Step failed"
    assert step._failure_reason_suffix() == ""

    long_reason = "x" * 100
    suffix = step._failure_reason_suffix(long_reason)
    assert suffix.startswith(" · ")
    assert len(suffix) <= len(" · ") + 64 + 1  # clip + possible ellipsis
