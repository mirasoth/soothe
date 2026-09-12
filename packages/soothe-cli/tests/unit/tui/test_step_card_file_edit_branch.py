"""Step card File-edit branch: ordering, line-change display, always-on.

The step card activity tree renders three sections in order:
  1. To-do (all todo items)
  2. Tool-use (latest N main-agent tools, excluding file-write rows)
  3. File-edit (latest N file-write rows with Action File +line deltas)

The File-edit branch is always rendered when file-write tool rows exist for
the step (no config gate).  Standalone TUI file-change preview cards are
gated separately by ``TUI_FILE_CHANGE_CARDS_ENABLED`` (default False).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from soothe_cli.display import theme
from soothe_cli.tui.widgets.messages.cognition_step_activity import (
    StepActivityTree,
    StepRowIndex,
    StepToolRow,
    compute_file_edit_line_changes,
    file_edit_action_label,
    format_file_edit_line_suffix,
    format_file_edit_stats_label,
    is_file_write_tool_name,
)


def _plain(content: object) -> str:
    if hasattr(content, "plain"):
        return content.plain
    return str(content)


def _mock_theme_colors() -> MagicMock:
    colors = MagicMock()
    colors.warning = "#ff0000"
    colors.cognition = "#00ff00"
    colors.foreground = "#000000"
    colors.muted = "#888888"
    colors.error = "#ff0000"
    colors.primary = "#0000ff"
    colors.success = "#00ff00"
    return colors


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_file_write_tool_name() -> None:
    assert is_file_write_tool_name("write_file")
    assert is_file_write_tool_name("edit_file")
    assert is_file_write_tool_name("edit_lines")
    assert is_file_write_tool_name("insert_lines")
    assert is_file_write_tool_name("delete_lines")
    assert is_file_write_tool_name("delete_file")
    assert is_file_write_tool_name("apply_diff")
    assert not is_file_write_tool_name("grep")
    assert not is_file_write_tool_name("read_file")
    assert not is_file_write_tool_name("")


def test_file_edit_action_label() -> None:
    assert file_edit_action_label("write_file") == "Created"
    assert file_edit_action_label("edit_file") == "Edited"
    assert file_edit_action_label("edit_lines") == "Edited"
    assert file_edit_action_label("apply_diff") == "Edited"
    assert file_edit_action_label("insert_lines") == "Inserted"
    assert file_edit_action_label("delete_lines") == "Deleted"
    assert file_edit_action_label("delete_file") == "Deleted"


def test_compute_file_edit_line_changes_write_file() -> None:
    added, removed = compute_file_edit_line_changes(
        "write_file",
        {"content": "line1\nline2\nline3"},
    )
    assert (added, removed) == (3, 0)


def test_compute_file_edit_line_changes_edit_file() -> None:
    added, removed = compute_file_edit_line_changes(
        "edit_file",
        {"old_string": "a\nb", "new_string": "a\nb\nc\nd"},
    )
    assert (added, removed) == (4, 2)


def test_compute_file_edit_line_changes_insert_lines() -> None:
    added, removed = compute_file_edit_line_changes(
        "insert_lines",
        {"content": "x\ny"},
    )
    assert (added, removed) == (2, 0)


def test_compute_file_edit_line_changes_delete_lines() -> None:
    added, removed = compute_file_edit_line_changes(
        "delete_lines",
        {"start_line": 5, "end_line": 8},
    )
    assert (added, removed) == (0, 4)


def test_compute_file_edit_line_changes_delete_file() -> None:
    added, removed = compute_file_edit_line_changes("delete_file", {})
    assert (added, removed) == (0, 0)


def test_format_file_edit_line_suffix() -> None:
    assert format_file_edit_line_suffix(10, 2) == "+10 -2"
    assert format_file_edit_line_suffix(5, 0) == "+5"
    assert format_file_edit_line_suffix(0, 3) == "-3"
    assert format_file_edit_line_suffix(0, 0) == ""


def test_format_file_edit_stats_label_both_deltas() -> None:
    assert format_file_edit_stats_label(2, 10, 2) == "2 files +10 -2"


def test_format_file_edit_stats_label_added_only() -> None:
    assert format_file_edit_stats_label(1, 5, 0) == "1 file +5"


def test_format_file_edit_stats_label_removed_only() -> None:
    assert format_file_edit_stats_label(3, 0, 8) == "3 files -8"


def test_format_file_edit_stats_label_zero_deltas() -> None:
    """No line delta (e.g. delete_file) still shows the count word."""
    assert format_file_edit_stats_label(2, 0, 0) == "2 files"


def test_format_file_edit_stats_label_zero_count() -> None:
    assert format_file_edit_stats_label(0, 10, 2) == ""


def test_format_file_edit_stats_label_singular_word() -> None:
    assert format_file_edit_stats_label(1, 0, 1) == "1 file -1"


# ---------------------------------------------------------------------------
# Integration: StepActivityTree.render (File-edit always on)
# ---------------------------------------------------------------------------


def _make_index(rows: list[StepToolRow]) -> StepRowIndex:
    """Build a StepRowIndex with explicit rows for render tests."""
    file_edit_rows = [r for r in rows if is_file_write_tool_name(r.tool_name)]
    return StepRowIndex(
        task_delegations=[],
        main_tools=rows,
        children_by_task={},
        file_edit_rows=file_edit_rows,
        total_tool_count=len(rows),
        main_tool_count=len(rows),
        task_delegation_count=0,
        file_edit_count=len(file_edit_rows),
    )


def _render(index: StepRowIndex) -> str:
    g = MagicMock()
    g.checkmark = "✓"
    g.error = "✗"
    g.circle_empty = "○"
    g.spinner_frames = ["⠋", "⠙"]
    g.output_prefix = "⎿"
    g.tool_prefix = "●"
    colors = _mock_theme_colors()
    with patch.object(theme, "get_theme_colors", return_value=colors):
        content = StepActivityTree.render(
            step_id="FE-01",
            step_status="running",
            index=index,
            subagent_notes=[],
            subagent_notes_by_task={},
            spinner_position=0,
            colors=colors,
            g=g,
            todos=[],
        )
    return _plain(content)


def test_file_edit_always_shows_branch_with_action_path_deltas() -> None:
    """File-edit branch is always rendered when file-write rows exist."""
    rows = [
        StepToolRow(
            tool_call_id="FE_01:s:write_file:0",
            tool_name="write_file",
            args={"file_path": "/tmp/foo.md", "content": "hello\nworld"},
            phase="success",
        ),
    ]
    text = _render(_make_index(rows))
    assert "File-edit" in text
    assert "Created" in text
    assert "foo.md" in text
    assert "+2" in text


def test_section_order_todo_tools_fileedit() -> None:
    """Render order: To-do → Tool-use → File-edit."""
    rows = [
        StepToolRow(
            tool_call_id="FE_01:s:grep:0",
            tool_name="grep",
            args={"pattern": "x"},
            phase="success",
        ),
        StepToolRow(
            tool_call_id="FE_01:s:write_file:1",
            tool_name="write_file",
            args={"file_path": "/tmp/out.md", "content": "a\nb\nc"},
            phase="success",
        ),
    ]
    index = _make_index(rows)
    g = MagicMock()
    g.checkmark = "✓"
    g.error = "✗"
    g.circle_empty = "○"
    g.spinner_frames = ["⠋"]
    g.output_prefix = "⎿"
    g.tool_prefix = "●"
    colors = _mock_theme_colors()
    todos = [{"content": "Do thing", "status": "pending"}]
    with patch.object(theme, "get_theme_colors", return_value=colors):
        text = _plain(
            StepActivityTree.render(
                step_id="FE-01",
                step_status="running",
                index=index,
                subagent_notes=[],
                subagent_notes_by_task={},
                spinner_position=0,
                colors=colors,
                g=g,
                todos=todos,
            )
        )
    todo_idx = text.index("To-do")
    tools_idx = text.index("Tool-use")
    file_edit_idx = text.index("File-edit")
    assert todo_idx < tools_idx < file_edit_idx


def test_file_edit_capped_to_latest_five() -> None:
    rows = [
        StepToolRow(
            tool_call_id=f"FE_01:s:write_file:{i}",
            tool_name="write_file",
            args={"file_path": f"/tmp/f{i}.md", "content": "x"},
            phase="success",
        )
        for i in range(7)
    ]
    text = _render(_make_index(rows))
    # Latest 5 visible; oldest 2 hidden behind "+2 more"
    assert "f6.md" in text
    assert "f2.md" in text
    assert "f0.md" not in text
    assert "+2 more edit" in text


def test_file_edit_excludes_file_write_from_tool_use() -> None:
    """File-write rows should not also appear under Tool-use."""
    rows = [
        StepToolRow(
            tool_call_id="FE_01:s:grep:0",
            tool_name="grep",
            args={"pattern": "x"},
            phase="success",
        ),
        StepToolRow(
            tool_call_id="FE_01:s:write_file:1",
            tool_name="write_file",
            args={"file_path": "/tmp/out.md", "content": "a"},
            phase="success",
        ),
    ]
    text = _render(_make_index(rows))
    tools_idx = text.index("Tool-use")
    file_edit_idx = text.index("File-edit")
    tools_section = text[tools_idx:file_edit_idx]
    assert "Grep" in tools_section
    assert "WriteFile" not in tools_section


def test_file_edit_edit_file_shows_added_removed() -> None:
    rows = [
        StepToolRow(
            tool_call_id="FE_01:s:edit_file:0",
            tool_name="edit_file",
            args={
                "file_path": "/tmp/config.yml",
                "old_string": "foo\nbar",
                "new_string": "foo\nbar\nbaz\nqux",
            },
            phase="success",
        ),
    ]
    text = _render(_make_index(rows))
    assert "Edited" in text
    assert "config.yml" in text
    assert "+4 -2" in text


def test_file_edit_branch_absent_without_file_write_rows() -> None:
    """No File-edit header when there are no file-write tool rows."""
    rows = [
        StepToolRow(
            tool_call_id="FE_01:s:grep:0",
            tool_name="grep",
            args={"pattern": "x"},
            phase="success",
        ),
    ]
    text = _render(_make_index(rows))
    assert "File-edit" not in text
    assert "Tool-use" in text
