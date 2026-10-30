"""Central numeric limits for TUI preview and truncated displays."""

from __future__ import annotations

from typing import Final

# --- Step cognition cards (`CognitionStepMessage`) ---
# Latest per-tool invocation lines on step cards and orphan SubAgent cards.
STEP_CARD_TOOL_ACTIVITY_PREVIEW_COUNT: Final[int] = 5

# File-edit branch on step cards: latest N file-write tool rows (write_file,
# edit_file, edit_lines, insert_lines, delete_lines, apply_diff, delete_file).
# The File-edit branch is always rendered (no gate); it only shows when there
# are file-write tool rows for the step.
STEP_CARD_FILE_EDIT_PREVIEW_COUNT: Final[int] = 5

# Reasoning branch on step cards: max lines shown in the Reasoning section.
# Reasoning accumulates while a step runs, so the section renders the *latest*
# this many lines and drops older ones (no "+N more lines" footer).
STEP_CARD_REASONING_PREVIEW_LINES: Final[int] = 8

# Reasoning branch: max characters per line before truncation (width-aware
# truncation also applies at render time via max_cols).
STEP_CARD_REASONING_LINE_MAX_CHARS: Final[int] = 120

# --- Step card activity panes (embedded windows, not a nested tree) ---
# Panes always stack vertically in one column (tools → todo → files →
# thinking) at every terminal width: a single column keeps the reading order
# stable and avoids re-flowing the card when the terminal is resized.

# Leading rule characters before a pane label (`──── tools ──── 22 · ⚠1`).
STEP_CARD_PANE_LEAD_RULE_CHARS: Final[int] = 4

# Todo rows kept inside the todo pane; overflow is summarized by an inline
# `· +N more` footnote row inside the pane.
STEP_CARD_PANE_ROWS_TODO: Final[int] = 4

# TUI file-change preview cards (the standalone write/edit/delete diff cards
# mounted in the chat transcript, NOT the step-card File-edit branch).
# When False (default), the standalone file-change cards are suppressed so
# file edits appear only in the step-card File-edit branch. Set True to also
# mount the dedicated diff/content preview cards.
TUI_FILE_CHANGE_CARDS_ENABLED: Final[bool] = False

# Single-line task-description preview on task markers and orphan SubAgent headers.
TASK_DELEGATION_DESC_MAX_CHARS: Final[int] = 80

# --- Skill invocation cards (`SkillMessage` collapsed SKILL.md body) ---
SKILL_CARD_PREVIEW_LINES: Final[int] = 4
SKILL_CARD_PREVIEW_CHARS: Final[int] = 300

# --- Write / edit / delete file change preview widgets (`file_change_preview`) ---
TOOL_APPROVAL_PREVIEW_LINES: Final[int] = 8
TOOL_APPROVAL_BODY_MAX_LINES: Final[int] = 8
TOOL_APPROVAL_DIFF_WIDGET_MAX_LINES: Final[int] = 8

# --- Clipboard copy toast ---
CLIPBOARD_TOAST_PREVIEW_CHARS: Final[int] = 40

# --- Chat input: large paste abbreviation (display only; submit uses full text) ---
CHAT_INPUT_PASTE_ABBREVIATE_LINE_COUNT: Final[int] = 4
CHAT_INPUT_PASTE_ABBREVIATE_CHAR_COUNT: Final[int] = 240

# --- Unified diff snippets in chat (`file_ops`, DiffMessage) ---
APPROVAL_DIFF_MAX_LINES: Final[int] = 15

# --- Plan quick-view panel (Ctrl+T goal tree step rows) ---
PLAN_QUICK_VIEW_STEP_LINE_MAX_CHARS: Final[int] = 76
