"""Pure step-card activity rendering and row classification."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from soothe_client.ux.task_namespace import (
    _step_id_from_unified_fragment,
    is_inner_subgraph_task_tool_id,
    is_step_level_task_tool_id,
    normalize_step_task_tool_call_id,
    parse_unified_tool_call_id,
)
from textual.content import Content

from soothe_cli.commands.subagent_routing import get_subagent_display_name
from soothe_cli.display import theme
from soothe_cli.display.preview_limits import (
    STEP_CARD_FILE_EDIT_PREVIEW_COUNT,
    STEP_CARD_PANE_LEAD_RULE_CHARS,
    STEP_CARD_PANE_ROWS_TODO,
    STEP_CARD_REASONING_LINE_MAX_CHARS,
    STEP_CARD_REASONING_PREVIEW_LINES,
    STEP_CARD_TOOL_ACTIVITY_PREVIEW_COUNT,
    TASK_DELEGATION_DESC_MAX_CHARS,
)
from soothe_cli.display.tool_display import (
    abbreviate_tool_error_message,
    compact_arg_text,
    display_width,
    format_step_tool_activity_command,
    format_step_tool_activity_line,
    truncate_to_width,
)
from soothe_cli.runtime.presentation.duration_format import (
    format_duration_ms,
    format_running_elapsed_compact,
)

# Todo item status → phase_icon input.
_TODO_STATUS_TO_PHASE: dict[str, str] = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "success",
    "done": "success",
    "cancelled": "skipped",
    "canceled": "skipped",
    "failed": "error",
    "error": "error",
}


@dataclass
class StepToolRow:
    """One tool invocation row on the step card.

    Task delegation rows use `is_task_row=True` as flat markers. Subgraph tools
    (type `t`) stay on the step card for per-task counts; they are not rendered
    as nested activity lines (task call line shows the running tool total).
    """

    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    phase: str  # pending | running | success | error | rejected | skipped
    output: str = ""
    duration_ms: int = 0
    started_at: float | None = None
    parent_tool_call_id: str | None = None
    is_task_row: bool = False


@dataclass
class StepRowIndex:
    """Single-pass classification of tool rows for one step card.

    Step cards show flat tool rows and task delegation markers. Subgraph tools
    are counted under `children_by_task` for the running task-line suffix and
    included in `total_tool_count`; they are not shown as nested preview lines.
    """

    task_delegations: list[StepToolRow] = field(default_factory=list)
    main_tools: list[StepToolRow] = field(default_factory=list)
    children_by_task: dict[str, list[StepToolRow]] = field(default_factory=dict)
    file_edit_rows: list[StepToolRow] = field(default_factory=list)
    total_tool_count: int = 0
    main_tool_count: int = 0
    task_delegation_count: int = 0
    file_edit_count: int = 0
    file_edit_added: int = 0
    file_edit_removed: int = 0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_tool_count_label(count: int, *, singular: str, plural: str) -> str:
    """Human-readable count label, e.g. `3 tools`."""
    if count <= 0:
        return ""
    word = singular if count == 1 else plural
    return f"{count} {word}"


def count_distinct_tool_call_ids(rows: list[StepToolRow]) -> int:
    """Distinct non-empty tool_call_id values in `rows`."""
    ids: set[str] = set()
    for row in rows:
        tcid = str(row.tool_call_id).strip()
        if tcid:
            ids.add(tcid)
    return len(ids)


def latest_preview_rows(
    rows: list[StepToolRow],
    limit: int = STEP_CARD_TOOL_ACTIVITY_PREVIEW_COUNT,
) -> list[StepToolRow]:
    """Return the most recently appended rows, capped at `limit`."""
    if not rows:
        return []
    if len(rows) <= limit:
        return list(rows)
    return rows[-limit:]


def stats_title_suffix(index: StepRowIndex) -> str:
    """Step status suffix: total tool count plus task delegation count."""
    parts: list[str] = []
    if index.total_tool_count:
        parts.append(
            format_tool_count_label(index.total_tool_count, singular="tool", plural="tools")
        )
    if index.task_delegation_count:
        parts.append(
            format_tool_count_label(index.task_delegation_count, singular="task", plural="tasks")
        )
    if not parts:
        return ""
    return f" · {', '.join(parts)}"


def compact_step_title_meta(
    *,
    elapsed_secs: int | None,
    tool_count: int,
    task_count: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    retry_attempt: int = 0,
    max_retry_attempts: int = 0,
    format_token: Any = None,
) -> str:
    """Compact middot meta for the step title.

    Forms: ` · 45s · 12/1 · ↑8.1K ↓2.0K · ↻1/3`. Description is not truncated;
    callers append this string after the full step brief.
    """
    parts: list[str] = []
    if elapsed_secs is not None:
        parts.append(format_running_elapsed_compact(float(elapsed_secs)))
    if tool_count > 0 or task_count > 0:
        parts.append(f"{max(0, int(tool_count))}/{max(0, int(task_count))}")
    if input_tokens or output_tokens:
        fmt = format_token if callable(format_token) else str
        parts.append(f"↑{fmt(int(input_tokens))} ↓{fmt(int(output_tokens))}")
    if retry_attempt > 0 and max_retry_attempts > 0:
        parts.append(f"↻{int(retry_attempt)}/{int(max_retry_attempts)}")
    if not parts:
        return ""
    return " · " + " · ".join(parts)


def normalize_todo_items(todos: list[Any] | None) -> list[dict[str, str]]:
    """Normalize CoreAgent `write_todos` / updates payloads to `{content, status}`."""
    out: list[dict[str, str]] = []
    if not todos:
        return out
    for item in todos:
        if isinstance(item, dict):
            content = str(item.get("content") or item.get("text") or "").strip()
            status = str(item.get("status") or "pending").strip().lower() or "pending"
            if content:
                out.append({"content": content, "status": status})
        else:
            text = str(item or "").strip()
            if text:
                out.append({"content": text, "status": "pending"})
    return out


def coerce_todos_list(raw: object) -> list[Any] | None:
    """Coerce a `todos` field to a list (handles JSON strings from streaming)."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(loaded, list):
            return loaded
    return None


def is_write_todos_tool_name(tool_name: object) -> bool:
    """True for `write_todos` / `WriteTodos` style names."""
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name == "write_todos":
        return True
    # CamelCase / mixed display forms → snake_case.
    snake = "".join(("_" + c.lower()) if c.isupper() else c for c in name).lstrip("_")
    return snake.replace("-", "_").lower() == "write_todos"


def todo_status_phase(status: str) -> str:
    """Map a todo status string to a `phase_icon` phase."""
    return _TODO_STATUS_TO_PHASE.get((status or "pending").strip().lower(), "pending")


# ---------------------------------------------------------------------------
# File-edit branch helpers
# ---------------------------------------------------------------------------

_FILE_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "edit_lines",
        "insert_lines",
        "delete_lines",
        "apply_diff",
        "delete_file",
    }
)


def is_file_write_tool_name(tool_name: object) -> bool:
    """True for filesystem tools that mutate files (write/edit/insert/delete)."""
    name = str(tool_name or "").strip().lower()
    return name in _FILE_WRITE_TOOL_NAMES


def file_edit_action_label(tool_name: str) -> str:
    """Short action verb for a file-write tool row (Created/Edited/Deleted)."""
    name = str(tool_name or "").strip().lower()
    if name == "write_file":
        return "Created"
    if name in ("edit_file", "edit_lines", "apply_diff"):
        return "Edited"
    if name in ("insert_lines",):
        return "Inserted"
    if name in ("delete_lines", "delete_file"):
        return "Deleted"
    return "Edited"


def _file_path_from_args(args: dict[str, Any]) -> str:
    """Extract the display path from a file-write tool row's args."""
    path_str = str(args.get("file_path") or args.get("path") or "").strip()
    if not path_str:
        return "(unknown)"
    try:
        from pathlib import Path

        p = Path(path_str)
        if p.is_absolute():
            return p.name or str(p)
        return str(p)
    except (OSError, ValueError):
        return str(path_str)


def _line_count(text: str) -> int:
    """Count lines in `text` (trailing newline does not add an empty line)."""
    if not text:
        return 0
    count = text.count("\n")
    if not text.endswith("\n"):
        count += 1
    return max(0, count)


def compute_file_edit_line_changes(
    tool_name: str,
    args: dict[str, Any],
) -> tuple[int, int]:
    """Best-effort added/removed line deltas for a file-write tool row.

    Returns:
        `(added, removed)` line counts. Returns `(0, 0)` when the tool args do
        not carry enough information to estimate the change.
    """
    name = str(tool_name or "").strip().lower()
    if name == "write_file":
        content = str(args.get("content") or "")
        return (_line_count(content), 0)
    if name == "edit_file":
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        added = _line_count(new)
        removed = _line_count(old)
        return (added, removed)
    if name == "insert_lines":
        content = str(args.get("content") or "")
        return (_line_count(content), 0)
    if name == "delete_lines":
        start = args.get("start_line")
        end = args.get("end_line")
        if isinstance(start, bool) or isinstance(end, bool):
            return (0, 0)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            removed = max(0, int(end) - int(start) + 1)
            return (0, removed)
        return (0, 0)
    if name == "edit_lines":
        edits = args.get("edits")
        added = 0
        removed = 0
        if isinstance(edits, list):
            for edit in edits:
                if not isinstance(edit, dict):
                    continue
                old = str(edit.get("old_string") or edit.get("old") or "")
                new = str(edit.get("new_string") or edit.get("new") or "")
                added += _line_count(new)
                removed += _line_count(old)
        return (added, removed)
    if name == "apply_diff":
        diff = str(args.get("diff") or args.get("content") or "")
        added = 0
        removed = 0
        for ln in diff.splitlines():
            if ln.startswith("+") and not ln.startswith("+++"):
                added += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                removed += 1
        return (added, removed)
    if name == "delete_file":
        return (0, 0)
    return (0, 0)


def format_file_edit_line_suffix(added: int, removed: int) -> str:
    """Format `+added -removed` line-change suffix for a file-edit row."""
    parts: list[str] = []
    if added:
        parts.append(f"+{added}")
    if removed:
        parts.append(f"-{removed}")
    return " ".join(parts)


def format_file_edit_stats_label(count: int, added: int, removed: int) -> str:
    """Aggregate file-edit label for the step footer, e.g. `2 files +10 -2`.

    Returns the count word (`N files`) plus a `+added -removed` line delta
    (via :func:`format_file_edit_line_suffix`) when any lines changed. Returns
    an empty string when ``count`` is zero so callers can omit the segment.
    """
    if count <= 0:
        return ""
    word = "file" if count == 1 else "files"
    line_suffix = format_file_edit_line_suffix(added, removed)
    if line_suffix:
        return f"{count} {word} {line_suffix}"
    return f"{count} {word}"


def has_task_activity_body(
    index: StepRowIndex,
    subagent_notes: list[str],
    subagent_notes_by_task: dict[str, list[str]],
    todos: list[dict[str, str]] | None = None,
    reasoning: str | None = None,
) -> bool:
    """True when the step card should show the task-activity tree panel."""
    if reasoning and reasoning.strip():
        return True
    if todos:
        return True
    if subagent_notes or subagent_notes_by_task:
        return True
    if index.task_delegations:
        return True
    return bool(index.main_tools)


def branched_prose_body(
    body: str,
    *,
    gutter: str,
    circle_empty: str,
    style: str,
) -> Content:
    """Streamed execute-phase prose: tree gutter per line."""
    text = (body or "").rstrip()
    if not text:
        return Content("")
    line_gutter = f"{gutter}{circle_empty} "
    parts: list[object] = []
    for i, ln in enumerate(text.splitlines()):
        if i:
            parts.append("\n")
        parts.append(Content.styled(f"{line_gutter}{ln}", style))
    return Content.assemble(*parts)


def finalize_tool_rows_on_step_end(
    rows: list[StepToolRow],
    task_delegations: list[StepToolRow],
    *,
    success: bool,
) -> None:
    """Finalize open tool rows when the step card completes.

    On successful steps, pending/running/skipped subgraph tools are marked
    `success` so task branches show Done instead of Skipped/Pending when
    the subagent finished without per-tool ToolMessage events.
    """
    terminal = "success" if success else "skipped"
    open_phases = ("pending", "running") if not success else ("pending", "running", "skipped")
    for row in rows:
        if row.phase in open_phases:
            row.phase = terminal
            row.started_at = None
    if success:
        for task_row in task_delegations:
            if (task_row.phase or "pending").strip().lower() in (
                "pending",
                "running",
                "skipped",
            ):
                task_row.phase = "success"
                task_row.started_at = None


# ---------------------------------------------------------------------------
# Row classification helpers
# ---------------------------------------------------------------------------


def row_belongs_to_step(row: StepToolRow, step_id: str) -> bool:
    """True when `row` belongs to this step card (unified id encodes step)."""
    parsed_sid, _, _, _ = parse_unified_tool_call_id(row.tool_call_id)
    if parsed_sid:
        canonical_step_id = _step_id_from_unified_fragment(step_id)
        return parsed_sid == canonical_step_id
    return True


def is_task_metadata_only_tool_row(row: StepToolRow) -> bool:
    """True when a nested row is task metadata and should stay hidden in branch activity."""
    if is_inner_subgraph_task_tool_id(row.tool_call_id):
        return True
    if (row.tool_name or "").strip() == "task":
        return True
    args = row.args if isinstance(row.args, dict) else {}
    subagent_type = str(args.get("subagent_type") or "").strip()
    desc = str(args.get("description") or args.get("prompt") or "").strip()
    return bool(subagent_type and desc)


def task_delegation_dedupe_key(row: StepToolRow, step_id: str) -> str:
    """Stable key for one main-graph task delegation (aliases share one branch)."""
    tcid = str(row.tool_call_id).strip()
    if not tcid:
        return ""
    if row.is_task_row or is_step_level_task_tool_id(tcid):
        parsed_sid, _, _, _ = parse_unified_tool_call_id(tcid)
        if parsed_sid and parsed_sid != step_id:
            return tcid
        return normalize_step_task_tool_call_id(step_id, tcid)
    return tcid


def prefer_task_delegation_row(candidate: StepToolRow, incumbent: StepToolRow) -> bool:
    """True when `candidate` should replace `incumbent` for the same task key."""
    if candidate.is_task_row and not incumbent.is_task_row:
        return True
    if incumbent.is_task_row and not candidate.is_task_row:
        return False
    return len(candidate.args or {}) >= len(incumbent.args or {})


def row_counts_for_main_tools(row: StepToolRow, step_id: str) -> bool:
    """True for main-agent tools on this step (excludes task rows and subgraph tools)."""
    if row.is_task_row:
        return False
    if not row_belongs_to_step(row, step_id):
        return False
    if is_task_metadata_only_tool_row(row):
        return False
    tcid = str(row.tool_call_id).strip()
    if not tcid:
        return False
    if is_step_level_task_tool_id(tcid):
        return False
    _, type_code, _, _ = parse_unified_tool_call_id(tcid)
    if type_code == "t":
        return False
    return True


def row_counts_for_step_tool_total(row: StepToolRow, step_id: str) -> bool:
    """True for rows that belong in the step-card footer tool total.

    Includes main-graph tools (type `s`) and subgraph tools (type `t`).
    Task delegation rows and task-metadata-only rows are excluded. Legacy opaque
    ids parented under a task are also excluded.
    """
    if row.is_task_row:
        return False
    if is_task_metadata_only_tool_row(row):
        return False
    if not row_belongs_to_step(row, step_id):
        return False
    tcid = str(row.tool_call_id).strip()
    if not tcid:
        return False
    _, type_code, _, _ = parse_unified_tool_call_id(tcid)
    if type_code in ("s", "t"):
        return True
    return not bool(row.parent_tool_call_id)


def _task_idx_from_task_row(row: StepToolRow) -> int | None:
    """Parse task index from a step-level `…:s:task:N` row id."""
    tcid = str(row.tool_call_id).strip()
    if not tcid:
        return None
    _, type_code, _, tool_info = parse_unified_tool_call_id(tcid)
    if type_code != "s":
        return None
    head = (tool_info or "").split(":")[0]
    if head != "task":
        return None
    tail = (tool_info or "").split(":")[-1]
    if tail.isdigit():
        return int(tail)
    return None


def build_children_by_task(
    step_id: str,
    rows: list[StepToolRow],
    task_delegations: list[StepToolRow],
) -> dict[str, list[StepToolRow]]:
    """Group countable subgraph tools under their parent task delegation key."""
    idx_to_key: dict[int, str] = {}
    for task_row in task_delegations:
        key = task_delegation_dedupe_key(task_row, step_id)
        idx = _task_idx_from_task_row(task_row)
        if key and idx is not None:
            idx_to_key[idx] = key
    if not idx_to_key:
        return {}
    children: dict[str, list[StepToolRow]] = {k: [] for k in idx_to_key.values()}
    for row in rows:
        if row.is_task_row or is_task_metadata_only_tool_row(row):
            continue
        if not row_belongs_to_step(row, step_id):
            continue
        tcid = str(row.tool_call_id).strip()
        if not tcid:
            continue
        _, type_code, idx, _ = parse_unified_tool_call_id(tcid)
        if type_code != "t":
            continue
        key = idx_to_key.get(idx)
        if key:
            children[key].append(row)
    return {k: v for k, v in children.items() if v}


def normalized_task_note_key(step_id: str, task_tool_call_id: str) -> str:
    """Normalize task tool_call_id for subagent note lookup."""
    tcid = str(task_tool_call_id).strip()
    if not tcid:
        return ""
    if is_step_level_task_tool_id(tcid):
        return normalize_step_task_tool_call_id(step_id, tcid)
    return tcid


class StepRowClassifier:
    """Builds a :class:`StepRowIndex` from raw tool rows."""

    @staticmethod
    def build(step_id: str, rows: list[StepToolRow]) -> StepRowIndex:
        """Classify rows into task delegations, main tools, and per-task children."""
        task_delegations = StepRowClassifier._iter_task_delegation_rows(step_id, rows)
        main_tools = [r for r in rows if row_counts_for_main_tools(r, step_id)]
        children_by_task = build_children_by_task(step_id, rows, task_delegations)
        countable = [r for r in rows if row_counts_for_step_tool_total(r, step_id)]
        file_edit_rows = [
            r for r in main_tools if is_file_write_tool_name(r.tool_name) and not r.is_task_row
        ]
        file_added = 0
        file_removed = 0
        for r in file_edit_rows:
            a, d = compute_file_edit_line_changes(r.tool_name, r.args or {})
            file_added += a
            file_removed += d
        return StepRowIndex(
            task_delegations=task_delegations,
            main_tools=main_tools,
            children_by_task=children_by_task,
            file_edit_rows=file_edit_rows,
            total_tool_count=count_distinct_tool_call_ids(countable),
            main_tool_count=count_distinct_tool_call_ids(main_tools),
            task_delegation_count=len(task_delegations),
            file_edit_count=len(file_edit_rows),
            file_edit_added=file_added,
            file_edit_removed=file_removed,
        )

    @staticmethod
    def build_orphan(rows: list[StepToolRow], *, task_idx: int = 0) -> StepRowIndex:
        """Classify rows for an intake-only orphan SubAgent card.

        Host stamps wired-subagent tools as `{step}:s:{id}` (type `s`).
        Nested task subgraphs use type `t` filtered by `task_idx`. Both
        belong on the orphan card as primary activity (no task-delegation branch).
        """
        filtered_rows: list[StepToolRow] = []
        for row in rows:
            tcid = str(row.tool_call_id).strip()
            if not tcid:
                continue
            if is_task_metadata_only_tool_row(row):
                continue
            _, type_code, idx, _ = parse_unified_tool_call_id(tcid)
            if type_code == "t":
                if idx == task_idx:
                    filtered_rows.append(row)
                continue
            # Type ``s`` (intake wire stamp) or opaque ids → show on orphan card.
            filtered_rows.append(row)

        orphan_file_edit_rows = [r for r in filtered_rows if is_file_write_tool_name(r.tool_name)]
        orphan_file_added = 0
        orphan_file_removed = 0
        for r in orphan_file_edit_rows:
            a, d = compute_file_edit_line_changes(r.tool_name, r.args or {})
            orphan_file_added += a
            orphan_file_removed += d
        return StepRowIndex(
            task_delegations=[],
            main_tools=filtered_rows,
            children_by_task={},
            file_edit_rows=orphan_file_edit_rows,
            total_tool_count=count_distinct_tool_call_ids(filtered_rows),
            main_tool_count=len(filtered_rows),
            task_delegation_count=0,
            file_edit_count=len(orphan_file_edit_rows),
            file_edit_added=orphan_file_added,
            file_edit_removed=orphan_file_removed,
        )

    @staticmethod
    def _iter_task_delegation_rows(step_id: str, rows: list[StepToolRow]) -> list[StepToolRow]:
        """Task delegation rows on this step (unified `{step}:s:task:…` ids)."""
        by_key: dict[str, StepToolRow] = {}
        canonical_step_id = _step_id_from_unified_fragment(step_id)
        for row in rows:
            if not row.is_task_row and not is_step_level_task_tool_id(row.tool_call_id):
                continue
            parsed_sid, _, _, _ = parse_unified_tool_call_id(str(row.tool_call_id or ""))
            if parsed_sid and parsed_sid != canonical_step_id:
                continue
            key = task_delegation_dedupe_key(row, step_id)
            if not key:
                continue
            prev = by_key.get(key)
            if prev is None or prefer_task_delegation_row(row, prev):
                by_key[key] = row
        return sorted(by_key.values(), key=lambda r: r.tool_call_id)


# ---------------------------------------------------------------------------
# Phase / tone / label helpers
# ---------------------------------------------------------------------------


def phase_icon(
    phase: str,
    g: Any,
    *,
    spinner_position: int = 0,
    animate_running: bool = False,
) -> str:
    """Lifecycle glyph for a task branch or tool row."""
    p = (phase or "pending").strip().lower()
    if p in ("success", "done"):
        return g.checkmark
    if p in ("error", "rejected", "failed"):
        return g.error
    if p == "running" and animate_running:
        frames = g.spinner_frames
        return frames[spinner_position % len(frames)]
    return g.circle_empty


def task_tool_row_tone_for_phase(phase: str, colors: Any) -> str:
    """Textual style for a tool row or task label from lifecycle phase."""
    p = (phase or "pending").strip().lower()
    if p in ("error", "rejected", "failed"):
        return colors.error
    return theme.SECONDARY_TEXT_STYLE


def task_tool_row_tone(row: StepToolRow, colors: Any) -> str:
    """Textual style for a tool row from its lifecycle phase."""
    return task_tool_row_tone_for_phase(row.phase or "pending", colors)


def preview_task_description(
    description: object,
    *,
    max_chars: int = TASK_DELEGATION_DESC_MAX_CHARS,
) -> str:
    """Collapse whitespace and truncate for a single-line task description preview."""
    if isinstance(description, str):
        text = compact_arg_text(description.strip())
    else:
        text = compact_arg_text(str(description or "").strip())
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def subagent_task_label(subagent_type: object, description: object = "") -> str:
    """Display label `DisplayName(preview)` for a subagent task description."""
    if isinstance(subagent_type, str):
        st = subagent_type.strip()
    else:
        st = str(subagent_type or "").strip()
    name = get_subagent_display_name(st) if st else "Task"
    desc_text = preview_task_description(description)
    if desc_text:
        return f"{name}({desc_text})"
    return name


def task_delegation_label(task_row: StepToolRow) -> str:
    """Display label `SubAgentName(description)` for a task delegation row."""
    args = dict(task_row.args or {})
    return subagent_task_label(
        args.get("subagent_type", ""),
        args.get("description") or args.get("prompt") or "",
    )


def append_tool_activity_lines(
    parts: list[object],
    rows: list[StepToolRow],
    *,
    gutter: str,
    g: Any,
    colors: Any,
    spinner_position: int,
    animate_running: bool,
    max_cols: int | None = None,
) -> None:
    """Append capped per-tool activity lines under a task or step branch.

    `max_cols` is the available terminal width for one tool line. When set,
    the prefix (`gutter + icon + " "`) is subtracted and the remainder is
    passed to :func:`format_step_tool_activity_line` so each row fits on one
    line without wrapping.
    """
    for row in rows:
        if parts:
            parts.append("\n")
        phase = (row.phase or "pending").strip().lower()
        icon = phase_icon(
            row.phase or "pending",
            g,
            spinner_position=spinner_position,
            animate_running=animate_running and phase == "running",
        )
        line_max = None
        if max_cols is not None and max_cols > 0:
            prefix_width = display_width(f"{gutter}{icon} ")
            line_max = max(0, max_cols - prefix_width)
        body = format_step_tool_activity_line(
            row.tool_name,
            row.args or {},
            row.phase or "pending",
            duration_ms=row.duration_ms,
            error=str(row.output or "") if phase == "error" else "",
            max_cols=line_max,
        )
        tone = task_tool_row_tone(row, colors)
        parts.append(Content.styled(f"{gutter}{icon} {body}", tone))


# ---------------------------------------------------------------------------
# Status lines
# ---------------------------------------------------------------------------


class StepCardStatusLine:
    """Pure footer status line builders (pending / completed; no Running)."""

    @staticmethod
    def footer_pending(
        *,
        gutter: str,
        circle_empty: str,
        stats_suffix: str,
        token_suffix: str = "",
        colors: Any,
    ) -> Content:
        """Step card footer for planned steps not yet executing."""
        head = f"{gutter}{circle_empty} Pending..."
        tail = f"{stats_suffix}{token_suffix}"
        parts: list[object] = [Content.styled(head, colors.cognition)]
        if tail:
            parts.append(Content.styled(tail, theme.SECONDARY_TEXT_STYLE))
        return Content.assemble(*parts)

    @staticmethod
    def footer_completed(
        *,
        gutter: str,
        icon: str,
        head: str,
        suffix: str = "",
        success: bool,
        colors: Any,
    ) -> Content:
        """Step card footer after successful or failed completion."""
        head_tone = theme.SECONDARY_TEXT_STYLE if success else colors.card_error
        parts: list[object] = [Content.styled(f"{gutter}{icon} {head}", head_tone)]
        if suffix:
            parts.append(Content.styled(suffix, theme.SECONDARY_TEXT_STYLE))
        return Content.assemble(*parts)


# Activity panes (embedded windows — flat rows, not a nested tree)
# ---------------------------------------------------------------------------
#
# A step card's activity is rendered as side-by-side / stacked *panes*
# (`tools`, `todo`, `files`, `thinking`). Each pane is a bounded viewport:
#
#   ──── tools ────────────────────────────────────────────── 22 · ⚠1 ────
#     ✓ Grep(jev, output_mode=files_with_matches)                    0.6s
#
# The header is a lightweight rule with the label and counters baked in, so it
# carries information instead of just naming a section. Rows are flat (no
# indentation hierarchy) with a right-aligned meta column, which is what makes
# the card read as windows rather than a tree.


@dataclass
class PaneLine:
    """One line inside an activity pane.

    `segs` is the left content (icon + kind badge + body) as `(text, style)`
    pairs; `meta` is right-aligned inside the pane width (duration / `live` /
    failure marker). Rows never nest — the pane header is the only structure.
    """

    segs: list[tuple[str, str]] = field(default_factory=list)
    meta: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ActivityPane:
    """One rendered pane: label, accent color, header counter and body rows."""

    label: str
    color: str
    lines: list[PaneLine] = field(default_factory=list)
    counter: str = ""


def _safe_colors(colors: Any) -> Any:
    """Fallback palette for pure render calls that pass `colors=None`."""
    return colors if colors is not None else theme.DARK_COLORS


def _bold(color: str) -> str:
    """Bold Textual style in `color` (pane headers and counters)."""
    return f"bold {color}"


def _thinking_tone(colors: Any) -> str:
    """Italic muted tone for captured reasoning (draft-grade content)."""
    return f"{colors.muted} italic"


def _pane_header_line(
    label: str,
    counter: str,
    *,
    color: str,
    width: int,
    g: Any,
    colors: Any,
) -> PaneLine:
    """Header line: a rule with the label and right-aligned counters on it.

    `──── tools ──────────────────────────────── 22 · ⚠1 ────`
    """
    colors = _safe_colors(colors)
    rule = g.box_horizontal
    lead = f"{rule * STEP_CARD_PANE_LEAD_RULE_CHARS} "
    counter_w = display_width(counter) if counter else 0
    fill = max(0, width - display_width(lead) - display_width(label) - 1 - counter_w - 1)
    segs = [
        (lead, colors.card_border),
        (f"{label} ", _bold(color)),
        (rule * fill, colors.card_border),
    ]
    meta = [(counter, _bold(color))] if counter else []
    return PaneLine(segs=segs, meta=meta)


def _tool_row_meta(row: StepToolRow) -> str:
    """Right-aligned meta for a tool row (duration / state / failure)."""
    phase = (row.phase or "pending").strip().lower()
    if phase == "success":
        return format_duration_ms(row.duration_ms) if row.duration_ms > 0 else ""
    if phase == "error":
        return abbreviate_tool_error_message(str(row.output or "")) or "failed"
    if phase == "running":
        return "live"
    if phase == "rejected":
        return "rejected"
    if phase == "skipped":
        return "skipped"
    return ""


def _assemble_pane_line(line: PaneLine, width: int | None) -> list[tuple[str, str]]:
    """Flatten one pane line: left segments, gap, right-aligned meta.

    Truncates the left content (never the meta) when the pane is narrower than
    the content, so every row stays on one line.
    """
    segs = [(t, s) for t, s in line.segs if t]
    meta = [(t, s) for t, s in line.meta if t]
    if width and width > 0:
        meta_w = sum(display_width(t) for t, _ in meta)
        avail = max(1, width - meta_w - (1 if meta else 0))
        left_w = sum(display_width(t) for t, _ in segs)
        if left_w > avail and segs:
            text, style = segs[-1]
            room = max(0, display_width(text) - (left_w - avail))
            segs[-1] = (truncate_to_width(text, room), style)
    if not meta:
        return segs
    left_w = sum(display_width(t) for t, _ in segs)
    meta_w = sum(display_width(t) for t, _ in meta)
    gap = max(1, (width or (left_w + meta_w + 1)) - left_w - meta_w)
    return segs + [(" " * gap, theme.SECONDARY_TEXT_STYLE)] + meta


def _render_pane(
    pane: ActivityPane,
    *,
    width: int,
    g: Any,
    colors: Any,
) -> list[list[tuple[str, str]]]:
    """Render one pane (header + rows) into styled line segments."""
    rows: list[list[tuple[str, str]]] = [
        _assemble_pane_line(
            _pane_header_line(
                pane.label,
                pane.counter,
                color=pane.color,
                width=width,
                g=g,
                colors=colors,
            ),
            width,
        )
    ]
    for line in pane.lines:
        rows.append(_assemble_pane_line(line, width))
    return rows


def _pane_content(rows: list[list[tuple[str, str]]]) -> Content:
    """Assemble styled rows into one ``Content`` block."""
    parts: list[object] = []
    for row in rows:
        if parts:
            parts.append("\n")
        for text, style in row:
            if text:
                parts.append(Content.styled(text, style or theme.SECONDARY_TEXT_STYLE))
    return Content.assemble(*parts) if parts else Content("")


# Activity tree renderer
# ---------------------------------------------------------------------------


class StepActivityTree:
    """Pure render: activity panes (tools / todo / files / thinking).

    Each pane is a bounded viewport rendered as flat rows beneath a
    lightweight colored header rule::

        ──── tools ────────────────────────────────────────────── 22 · ⚠1 ────
          ✓ Grep(jev, output_mode=files_with_matches)                    0.6s

    Rows carry no indentation hierarchy — kind comes from the row body and the
    right-aligned meta column — so the card reads as windows rather than a
    nested tree. The header rule doubles as the pane's information bar: it
    carries the label, the row count, and the failure badge, so no line is
    spent on a bare section name.

    Task delegation rows stay flat markers inside the tools pane; while a task
    runs, its line shows that task's subgraph tool count. The files pane shows
    the latest file-write rows with action labels and line deltas (always
    rendered when such rows exist — no config gate). The thinking pane shows
    the latest reasoning lines in an italic muted tone.
    """

    @staticmethod
    def render(
        *,
        step_id: str,
        step_status: str,
        index: StepRowIndex,
        subagent_notes: list[str],
        subagent_notes_by_task: dict[str, list[str]],
        spinner_position: int,
        colors: Any,
        g: Any,
        preview_limit: int = STEP_CARD_TOOL_ACTIVITY_PREVIEW_COUNT,
        todos: list[dict[str, str]] | None = None,
        reasoning: str | None = None,
        max_cols: int | None = None,
        glyph_override: str | None = None,
        file_edit_preview_limit: int = STEP_CARD_FILE_EDIT_PREVIEW_COUNT,
        reasoning_preview_lines: int = STEP_CARD_REASONING_PREVIEW_LINES,
        reasoning_line_max_chars: int = STEP_CARD_REASONING_LINE_MAX_CHARS,
    ) -> Content:
        """Render the step card activity body as one vertical column of panes.

        Panes always stack in reading order (tools → todo → files → thinking)
        at every terminal width; `max_cols` only bounds line width (rows are
        truncated, never wrapped) and falls back to a 100-column budget when
        `None` (pure-render tests / unmounted widgets). A single column keeps
        the reading order stable and avoids re-flowing the card on resize.

        `glyph_override` is accepted for call compatibility; pane rows no
        longer align to a card header glyph because rows are flat.

        The thinking pane shows the *latest* ``reasoning_preview_lines`` lines
        of reasoning (captured thinking tokens from
        ``additional_kwargs["reasoning_content"]`` plus untagged CoreAgent step
        text). Older lines scroll off: reasoning grows while the step runs, so
        the tail is what the user wants to read.
        """
        colors = _safe_colors(colors)
        todo_items = list(todos or [])

        main_source = list(index.main_tools)
        if todo_items:
            # Avoid duplicating write_todos when the todo pane is live.
            main_source = [r for r in main_source if not is_write_todos_tool_name(r.tool_name)]
        if index.file_edit_rows:
            # File-write rows render in the files pane; exclude from tools.
            main_source = [r for r in main_source if not is_file_write_tool_name(r.tool_name)]
        main_preview = latest_preview_rows(main_source, preview_limit)
        has_tools = bool(
            index.task_delegations or main_preview or subagent_notes or subagent_notes_by_task
        )
        file_edit_rows = (
            latest_preview_rows(index.file_edit_rows, file_edit_preview_limit)
            if index.file_edit_rows
            else []
        )
        reasoning_text = (reasoning or "").strip()
        if not reasoning_text and not todo_items and not has_tools and not file_edit_rows:
            return Content("")

        panes: list[ActivityPane] = []

        # --- tools pane ----------------------------------------------------
        tool_lines: list[PaneLine] = []
        animate_tools = step_status == "running"
        failure_phases = ("error", "rejected", "failed")

        for task_row in index.task_delegations:
            task_key = task_delegation_dedupe_key(task_row, step_id)
            task_phase = (task_row.phase or "pending").strip().lower()
            if task_phase == "pending" and animate_tools:
                task_phase = "running"
            icon = phase_icon(
                task_phase,
                g,
                spinner_position=spinner_position,
                animate_running=task_phase == "running",
            )
            label = task_delegation_label(task_row)
            if task_phase == "running":
                child_count = count_distinct_tool_call_ids(index.children_by_task.get(task_key, []))
                count_label = format_tool_count_label(child_count, singular="tool", plural="tools")
                if count_label:
                    label = f"{label} · {count_label}"
            tone = task_tool_row_tone_for_phase(task_phase, colors)
            meta = "live" if task_phase == "running" else ""
            tool_lines.append(
                PaneLine(
                    segs=[("  ", tone), (icon, tone), (" ", tone), (label, tone)],
                    meta=[(meta, tone)] if meta else [],
                )
            )
            for note in subagent_notes_by_task.get(task_key, []):
                text = (note or "").strip()
                if not text:
                    continue
                tool_lines.append(
                    PaneLine(
                        segs=[
                            ("  ", theme.SECONDARY_TEXT_STYLE),
                            (f"· {text}", theme.SECONDARY_TEXT_STYLE),
                        ]
                    )
                )

        # Consecutive identical calls collapse into `×N` so a burst of tool
        # calls costs one row instead of one row per call.
        grouped: list[tuple[StepToolRow, int]] = []
        for row in main_preview:
            command = format_step_tool_activity_command(row.tool_name, row.args or {})
            if grouped:
                prev, count = grouped[-1]
                if format_step_tool_activity_command(
                    prev.tool_name, prev.args or {}
                ) == command and (prev.phase or "") == (row.phase or ""):
                    grouped[-1] = (row, count + 1)
                    continue
            grouped.append((row, 1))
        for row, count in grouped:
            phase = (row.phase or "pending").strip().lower()
            icon = phase_icon(
                row.phase or "pending",
                g,
                spinner_position=spinner_position,
                animate_running=animate_tools and phase == "running",
            )
            tone = task_tool_row_tone(row, colors)
            body = format_step_tool_activity_command(row.tool_name, row.args or {})
            if count > 1:
                body = f"{body} ×{count}"
            meta = _tool_row_meta(row)
            tool_lines.append(
                PaneLine(
                    segs=[("  ", tone), (icon, tone), (" ", tone), (body, tone)],
                    meta=[(meta, theme.SECONDARY_TEXT_STYLE)] if meta else [],
                )
            )

        hidden_tools = len(main_source) - len(main_preview)
        if hidden_tools > 0:
            label = f"· +{hidden_tools} more tool{'s' if hidden_tools != 1 else ''}"
            tool_lines.append(
                PaneLine(
                    segs=[("  ", theme.SECONDARY_TEXT_STYLE), (label, theme.SECONDARY_TEXT_STYLE)]
                )
            )
        for note in subagent_notes:
            text = (note or "").strip()
            if not text:
                continue
            tool_lines.append(
                PaneLine(
                    segs=[
                        ("  ", theme.SECONDARY_TEXT_STYLE),
                        (f"· {text}", theme.SECONDARY_TEXT_STYLE),
                    ]
                )
            )

        if tool_lines:
            tool_total = len(main_source) + len(index.task_delegations)
            tool_failures = sum(
                1
                for r in (*main_source, *index.task_delegations)
                if (r.phase or "").strip().lower() in failure_phases
            )
            counter = f"{tool_total}"
            if tool_failures:
                counter = f"{tool_total} · {g.warning} {tool_failures}"
            panes.append(
                ActivityPane(label="tools", color=colors.accent, lines=tool_lines, counter=counter)
            )

        # --- todo pane -----------------------------------------------------
        if todo_items:
            todo_lines: list[PaneLine] = []
            shown_todos = todo_items[:STEP_CARD_PANE_ROWS_TODO]
            for item in shown_todos:
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                phase = todo_status_phase(str(item.get("status") or "pending"))
                icon = phase_icon(
                    phase,
                    g,
                    spinner_position=spinner_position,
                    animate_running=phase == "running" and animate_tools,
                )
                tone = task_tool_row_tone_for_phase(phase, colors)
                meta = "live" if phase == "running" else ""
                todo_lines.append(
                    PaneLine(
                        segs=[("  ", tone), (icon, tone), (" ", tone), (content, tone)],
                        meta=[(meta, tone)] if meta else [],
                    )
                )
            hidden_todos = len(todo_items) - len(shown_todos)
            if hidden_todos > 0:
                todo_lines.append(
                    PaneLine(
                        segs=[
                            ("  ", theme.SECONDARY_TEXT_STYLE),
                            (f"· +{hidden_todos} more", theme.SECONDARY_TEXT_STYLE),
                        ]
                    )
                )
            if todo_lines:
                done = sum(
                    1
                    for item in todo_items
                    if todo_status_phase(str(item.get("status") or "pending")) == "success"
                )
                panes.append(
                    ActivityPane(
                        label="todo",
                        color=colors.cognition,
                        lines=todo_lines,
                        counter=f"{done}/{len(todo_items)}",
                    )
                )

        # --- files pane ----------------------------------------------------
        files_lines: list[PaneLine] = []
        for row in file_edit_rows:
            phase = (row.phase or "pending").strip().lower()
            icon = phase_icon(
                row.phase or "pending",
                g,
                spinner_position=spinner_position,
                animate_running=animate_tools and phase == "running",
            )
            action = file_edit_action_label(row.tool_name)
            file_path = _file_path_from_args(row.args or {})
            added, removed = compute_file_edit_line_changes(row.tool_name, row.args or {})
            suffix = format_file_edit_line_suffix(added, removed)
            body = f"{action} {file_path}"
            tone = task_tool_row_tone(row, colors)
            files_lines.append(
                PaneLine(
                    segs=[("  ", tone), (icon, tone), (" ", tone), (body, tone)],
                    meta=[(suffix, theme.SECONDARY_TEXT_STYLE)] if suffix else [],
                )
            )
        hidden_edits = len(index.file_edit_rows) - len(file_edit_rows)
        if hidden_edits > 0:
            label = f"· +{hidden_edits} more edit{'s' if hidden_edits != 1 else ''}"
            files_lines.append(
                PaneLine(
                    segs=[("  ", theme.SECONDARY_TEXT_STYLE), (label, theme.SECONDARY_TEXT_STYLE)]
                )
            )
        if files_lines:
            stats = format_file_edit_stats_label(
                len(index.file_edit_rows), index.file_edit_added, index.file_edit_removed
            )
            panes.append(
                ActivityPane(
                    label="files",
                    color=colors.warning,
                    lines=files_lines,
                    counter=stats,
                )
            )

        # --- thinking pane -------------------------------------------------
        thinking_lines: list[PaneLine] = []
        if reasoning_text:
            char_limit = reasoning_line_max_chars
            if max_cols and max_cols > 0:
                char_limit = min(char_limit, max_cols)
            shown_lines: list[str] = []
            for ln in reasoning_text.splitlines():
                stripped = ln.strip()
                if not stripped:
                    continue
                if len(stripped) > char_limit:
                    shown_lines.append(stripped[: char_limit - 1] + "…")
                else:
                    shown_lines.append(stripped)
            tail_lines = (
                shown_lines[-reasoning_preview_lines:] if reasoning_preview_lines > 0 else []
            )
            tone = _thinking_tone(colors)
            for ln in tail_lines:
                thinking_lines.append(
                    PaneLine(
                        segs=[
                            ("  ", theme.SECONDARY_TEXT_STYLE),
                            (f"{g.bullet} ", tone),
                            (ln, tone),
                        ]
                    )
                )
        if thinking_lines:
            panes.append(
                ActivityPane(
                    label="thinking",
                    color=colors.muted,
                    lines=thinking_lines,
                )
            )

        if not panes:
            return Content("")

        # --- layout: one vertical column of panes --------------------------
        width = max_cols or 100
        rows: list[list[tuple[str, str]]] = []
        for pane in panes:
            rows.extend(_render_pane(pane, width=width, g=g, colors=colors))
        return _pane_content(rows)
