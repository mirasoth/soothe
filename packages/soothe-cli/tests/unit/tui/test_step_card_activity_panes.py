"""Step card activity panes (embedded windows) — layout and density rules.

Covers:
    - Pane headers replace the old nested section tree and carry counters.
    - Consecutive identical tool calls collapse into `×N`.
    - Panes always stack in one vertical column (tools → todo → files →
      thinking) at every width — no side-by-side grid.
    - Failures are counted in the tools pane header.
    - Rows are flat (no deeper indentation than the pane header) with a
      right-aligned meta column.
"""

from __future__ import annotations

import pytest

from soothe_cli.display import theme
from soothe_cli.settings import get_glyphs
from soothe_cli.tui.widgets.messages.cognition_step_activity import (
    StepActivityTree,
    StepRowClassifier,
    StepToolRow,
)

COLORS = theme.DARK_COLORS
G = get_glyphs()

TOOLS_HEADER = "──── tools"
TODO_HEADER = "──── todo"
FILES_HEADER = "──── files"
THINKING_HEADER = "──── thinking"


def _tool(
    call_id: str,
    name: str,
    args: dict,
    phase: str = "success",
    **kwargs: object,
) -> StepToolRow:
    return StepToolRow(
        tool_call_id=call_id,
        tool_name=name,
        args=args,
        phase=phase,
        **kwargs,  # type: ignore[arg-type]
    )


def _render(rows: list[StepToolRow], **kwargs: object) -> str:
    index = StepRowClassifier.build("STEP-01", rows)
    content = StepActivityTree.render(
        step_id="STEP-01",
        step_status="running",
        index=index,
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=COLORS,
        g=G,
        **kwargs,  # type: ignore[arg-type]
    )
    return content.plain


# ---------------------------------------------------------------------------
# Pane headers
# ---------------------------------------------------------------------------


def test_pane_headers_replace_section_tree() -> None:
    """Panes are labeled windows, not `Tool-use` / `To-do` sections."""
    text = _render(
        [_tool("c0", "grep", {"pattern": "jev"})],
        todos=[{"content": "verify", "status": "pending"}],
        reasoning="thinking out loud",
    )
    assert TOOLS_HEADER in text
    assert TODO_HEADER in text
    assert THINKING_HEADER in text
    assert "Tool-use" not in text
    assert "To-do" not in text


def test_pane_header_carries_the_row_counter() -> None:
    """The header line doubles as the information bar (`──── tools ── 2`)."""
    text = _render([_tool(f"c{i}", "grep", {"pattern": f"p{i}"}) for i in range(2)])
    header = next(ln for ln in text.splitlines() if TOOLS_HEADER in ln)
    assert header.rstrip().endswith("2")


def test_pane_header_counts_failures() -> None:
    """Failures show as a warning badge on the tools pane header."""
    text = _render(
        [
            _tool("c0", "grep", {"pattern": "jev"}),
            _tool("c1", "glob", {"pattern": "**/jev/**"}, phase="error", output="no match"),
        ]
    )
    header = next(ln for ln in text.splitlines() if TOOLS_HEADER in ln)
    assert G.warning in header
    assert "1" in header


# ---------------------------------------------------------------------------
# Density: aggregation and flat rows
# ---------------------------------------------------------------------------


def test_consecutive_identical_calls_collapse_to_xn() -> None:
    """A burst of identical calls costs one row, tagged `×3`."""
    rows = [
        _tool(f"c{i}", "grep", {"pattern": "jev", "output_mode": "files_with_matches"})
        for i in range(3)
    ]
    text = _render(rows)
    assert "×3" in text
    assert text.count("Grep(") == 1


def test_rows_are_flat_with_right_aligned_meta() -> None:
    """Duration sits in the meta column; rows share one indentation level."""
    text = _render([_tool("c0", "grep", {"pattern": "jev"}, duration_ms=600)], max_cols=96)
    row = next(ln for ln in text.splitlines() if "Grep(" in ln)
    assert row.startswith("  ") and not row.startswith("   ")
    assert "600ms" in row


def test_overflow_footer_stays_inside_the_pane() -> None:
    text = _render(
        [_tool(f"c{i}", "grep", {"pattern": f"p{i}"}) for i in range(6)],
        preview_limit=2,
    )
    assert "+4 more tools" in text


# ---------------------------------------------------------------------------
# Layout: two columns vs stacked + chip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cols", [80, 96, 120, 160, None])
def test_panes_stack_in_one_column_at_every_width(cols: int | None) -> None:
    """One vertical column: each pane header owns its own line, in order."""
    text = _render(
        [
            _tool("c0", "grep", {"pattern": "jev"}),
            _tool("c1", "write_file", {"file_path": "a.py", "content": "x\ny"}),
        ],
        todos=[{"content": "verify", "status": "pending"}],
        reasoning="draft thought",
        max_cols=cols,
    )
    lines = text.splitlines()
    header_lines = [
        i
        for i, ln in enumerate(lines)
        if TOOLS_HEADER in ln or TODO_HEADER in ln or FILES_HEADER in ln or THINKING_HEADER in ln
    ]
    # Four panes, four header lines, each on its own row (never side by side).
    assert len(header_lines) == 4
    assert TOOLS_HEADER in lines[header_lines[0]]
    assert TODO_HEADER in lines[header_lines[1]]
    assert FILES_HEADER in lines[header_lines[2]]
    assert THINKING_HEADER in lines[header_lines[3]]


def test_unknown_width_keeps_full_panes() -> None:
    """`max_cols=None` (unmounted / pure render) shows every pane stacked."""
    rows = [
        _tool("c0", "grep", {"pattern": "jev"}),
        _tool("c1", "write_file", {"file_path": "a.py", "content": "x\ny"}),
    ]
    text = _render(rows, reasoning="draft thought")
    assert TOOLS_HEADER in text
    assert FILES_HEADER in text
    assert THINKING_HEADER in text


def test_lines_fit_the_width_budget() -> None:
    text = _render(
        [
            _tool("c0", "run", {"command": "grep -rni 'jev' --include=*.py src/" * 6}),
            _tool("c1", "write_file", {"file_path": "a" * 200, "content": "x"}),
        ],
        reasoning="y" * 300,
        max_cols=100,
    )
    for line in text.splitlines():
        assert len(line) <= 100
