"""Tests for the Reasoning branch on the step card.

Verifies that captured thinking-token reasoning is rendered in the
Reasoning section of the step card activity tree, with proper ordering
(before To-do), truncation, and line capping.
"""

from __future__ import annotations

from soothe_cli.display.preview_limits import (
    STEP_CARD_REASONING_PREVIEW_LINES,
)
from soothe_cli.settings import get_glyphs
from soothe_cli.tui.widgets.messages.cognition_step_activity import (
    StepActivityTree,
    StepRowIndex,
    has_task_activity_body,
)


def _empty_index() -> StepRowIndex:
    """An empty row index (no tools, no tasks)."""
    return StepRowIndex()


def test_has_task_activity_body_true_with_reasoning() -> None:
    """Reasoning text alone should trigger the activity panel."""
    index = _empty_index()
    assert has_task_activity_body(index, [], {}, reasoning="thinking...")


def test_has_task_activity_body_false_without_reasoning() -> None:
    """No reasoning and no tools means no activity panel."""
    index = _empty_index()
    assert not has_task_activity_body(index, [], {}, reasoning="")


def test_has_task_activity_body_false_with_whitespace_reasoning() -> None:
    """Whitespace-only reasoning should not trigger the panel."""
    index = _empty_index()
    assert not has_task_activity_body(index, [], {}, reasoning="   \n  ")


def test_render_reasoning_section_appears_before_todo() -> None:
    """Reasoning section header appears before To-do header."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="I should check the file first.",
        todos=[{"content": "Read file", "status": "pending"}],
    )
    text = str(content)
    reasoning_pos = text.find("Reasoning")
    todo_pos = text.find("To-do")
    assert reasoning_pos >= 0
    assert todo_pos >= 0
    assert reasoning_pos < todo_pos


def test_render_reasoning_section_content_shown() -> None:
    """Reasoning text appears as item lines under the header."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="Let me analyze the code.",
    )
    text = str(content)
    assert "Reasoning" in text
    assert "Let me analyze the code." in text


def test_render_reasoning_truncates_long_lines() -> None:
    """Long reasoning lines are truncated with an ellipsis."""
    long_line = "x" * 200
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning=long_line,
        reasoning_line_max_chars=50,
    )
    text = str(content)
    assert "…" in text
    # The full 200-char line should not appear
    assert long_line not in text


def test_render_reasoning_caps_line_count() -> None:
    """Reasoning with more lines than the preview limit shows '+N more lines'."""
    lines = [f"line {i}" for i in range(STEP_CARD_REASONING_PREVIEW_LINES + 5)]
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="\n".join(lines),
        reasoning_preview_lines=STEP_CARD_REASONING_PREVIEW_LINES,
    )
    text = str(content)
    assert "+5 more lines" in text


def test_render_reasoning_empty_string_no_section() -> None:
    """Empty reasoning string does not produce a Reasoning section."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="",
    )
    text = str(content)
    assert "Reasoning" not in text


def test_render_reasoning_multiline_content() -> None:
    """Multi-line reasoning shows each non-empty line as an item."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="First thought\nSecond thought\nThird thought",
    )
    text = str(content)
    assert "First thought" in text
    assert "Second thought" in text
    assert "Third thought" in text


def test_render_reasoning_skips_empty_lines() -> None:
    """Empty lines in reasoning are skipped, not rendered as items."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="Real thought\n\n\nAnother thought",
    )
    text = str(content)
    assert "Real thought" in text
    assert "Another thought" in text


def test_render_reasoning_with_tools_section_ordering() -> None:
    """Reasoning appears before Tool-use when both are present."""
    from soothe_cli.tui.widgets.messages.cognition_step_activity import StepToolRow

    index = StepRowIndex(
        main_tools=[
            StepToolRow(
                tool_call_id="s1:s:0",
                tool_name="read_file",
                args={"file_path": "test.py"},
                phase="success",
            )
        ]
    )
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=index,
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="I need to read the file first.",
    )
    text = str(content)
    reasoning_pos = text.find("Reasoning")
    tool_use_pos = text.find("Tool-use")
    assert reasoning_pos >= 0
    assert tool_use_pos >= 0
    assert reasoning_pos < tool_use_pos
