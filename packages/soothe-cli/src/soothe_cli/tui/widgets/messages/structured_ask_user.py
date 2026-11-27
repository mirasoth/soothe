"""Structured multi-question option-picker widget for `ask_user` and HITL gates.

Unifies the generic ``ask_user`` (execute origin) and the HITL plan-review /
tool-approval origins into one widget. HITL origins differ only in that their
options are system-prefilled (Approve / Reject / Refine), the ``Other``
free-text row is suppressed, and the Refine option shows a comment input.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import Any

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Click, DescendantFocus
from textual.message import Message
from textual.widgets import Button, Input, Static

from soothe_cli.display import theme
from soothe_cli.display.card import (
    _assemble_card_header,
    _card_body_gutter,
)
from soothe_cli.display.markdown_theme import (
    ThemedMarkdownRenderer,
    resolve_markdown_theme_parts,
)
from soothe_cli.settings import get_glyphs
from soothe_cli.tui.focus import FocusPriority
from soothe_cli.tui.widgets.clipboard import screen_has_text_selection

logger = logging.getLogger(__name__)

# Wire origin constants (mirror host ORIGIN_* constants). CLI must not import
# soothe host packages; keep the wire strings local.
_ORIGIN_PLAN_MODE_REVIEW = "plan_mode_review"
_ORIGIN_TOOL_APPROVAL = "tool_approval"

_HITL_ORIGINS: frozenset[str] = frozenset({_ORIGIN_PLAN_MODE_REVIEW, _ORIGIN_TOOL_APPROVAL})

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)

# Max chars of the tool arg (e.g. shell command) shown in the approval card
# title. Long commands are truncated to a brief preview with an ellipsis so
# the card header stays readable. The CLI truncates defensively — it cannot
# assume the host's upstream truncation has already shortened the value.
_TOOL_ARG_PREVIEW_MAX = 80


def _truncate_tool_arg(body: str) -> str:
    """Truncate the parenthesized arg portion of a tool-approval body.

    The body is shaped ``"<name> (<arg>)"`` (the leading ``"Approve "`` and
    trailing ``"?"`` already stripped). Only the ``<arg>`` inside the final
    parenthesized group is shortened; the tool name and the wrapping parens
    are preserved. Bodies without a parenthesized arg pass through unchanged.
    """
    if not body:
        return body
    open_idx = body.rfind("(")
    if open_idx < 0 or not body.endswith(")"):
        return body
    arg = body[open_idx + 1 : -1]
    if len(arg) <= _TOOL_ARG_PREVIEW_MAX:
        return body
    preview = arg[: _TOOL_ARG_PREVIEW_MAX - 1].rstrip() + "…"
    return f"{body[: open_idx + 1]}{preview})"


def _strip_plan_frontmatter(markdown: str) -> str:
    """Remove YAML frontmatter from a plan artifact for display."""
    raw = (markdown or "").strip()
    if not raw.startswith("---"):
        return raw
    return _FRONTMATTER_RE.sub("", raw, count=1).strip()


def tool_label_from_header(header: str) -> str:
    """Short tool label from an approval question header.

    ``"Approve run_command (command=git status)?"`` → ``"run_command"``.
    Falls back to the (stripped) header when it does not match the shape.
    """
    body = (header or "").strip()
    if body.startswith("Approve "):
        body = body[len("Approve ") :]
    paren = body.find(" (")
    if paren >= 0:
        body = body[:paren]
    if body.endswith("?"):
        body = body[:-1]
    return body.strip() or (header or "").strip()


class StructuredAskUserWidget(Vertical):
    """Unified multi-question option-picker for ``ask_user`` and HITL gates.

    Three modes:
    - **Structured** (default): tabs + options + inline descriptions + footer.
      Used for generic ``ask_user`` (execute origin) with LLM-emitted options.
    - **HITL**: same structured layout but ``allow_custom=False`` (no "Other"
      row), origin-aware title, optional plan body rendering, and a comment
      field tied to the Refine/Edit option. Used for ``plan_mode_review``
      and ``tool_approval`` origins.
    - **Degraded**: plain free-text ``Input`` per question + Submit button.
      Used for in-flight plain-string questions from before the schema upgrade.
    """

    can_focus = True

    BINDINGS = [
        Binding("left", "prev_question", "Prev question", show=False),
        Binding("right", "next_question", "Next question", show=False),
        Binding("up", "prev_option", "Prev option", show=False),
        Binding("down", "next_option", "Next option", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("tab", "focus_footer", "Footer", show=False),
        Binding("escape", "abandon", "Abandon", show=False),
    ]

    DEFAULT_CSS = """
    StructuredAskUserWidget {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        background: transparent;
    }

    StructuredAskUserWidget .saq-title {
        height: auto;
        color: $warning;
        text-style: bold;
        padding: 0;
        margin: 0;
    }

    StructuredAskUserWidget .saq-tab-bar {
        height: auto;
        margin: 0 0 1 0;
    }

    /* height: auto is required — Vertical defaults to 1fr, which collapses
    content when the widget is mounted in a stream/auto-height container
    (e.g. #messages), crushing the description and option rows to zero. */
    StructuredAskUserWidget .saq-question-body {
        height: auto;
    }

    StructuredAskUserWidget .saq-option-list {
        height: auto;
    }

    StructuredAskUserWidget .saq-tab {
        height: 1;
        min-width: 0;
        width: auto;
        padding: 0 1;
        margin: 0 1 0 0;
        color: $text-muted;
    }

    StructuredAskUserWidget .saq-tab.is-active {
        color: $primary;
        text-style: bold;
    }

    StructuredAskUserWidget .saq-tab.is-answered {
        color: $success;
    }

    StructuredAskUserWidget .saq-tab.is-active.is-answered {
        color: $success;
        text-style: bold;
    }

    StructuredAskUserWidget .saq-question-title {
        height: auto;
        color: $text;
        text-style: bold;
        padding: 0;
        margin: 0 0 0 0;
    }

    StructuredAskUserWidget .saq-description {
        height: auto;
        color: $text;
        padding: 0;
        margin: 0 0 0 0;
    }

    StructuredAskUserWidget .saq-option-row {
        height: auto;
        width: 1fr;
        padding: 0;
        margin: 0;
        color: $text;
    }

    StructuredAskUserWidget .saq-option-row.is-highlighted {
        color: $text;
        text-style: bold;
    }

    StructuredAskUserWidget .saq-option-row.is-selected {
        color: $success;
        text-style: bold;
    }

    /* ── Option list ─────────────────────────────────────────────── */

    /* Long description rendered directly below each option label.
    Tight (zero) bottom margin packs options tightly — no blank lines
    between option rows, matching the Claude Code AskUserQuestion style. */
    StructuredAskUserWidget .saq-option-desc {
        height: auto;
        width: 1fr;
        padding: 0;
        margin: 0 0 0 0;
        color: $text-muted;
    }

    /* Hide stale option/desc rows when switching to a question with
    fewer options than the initially rendered one. */
    StructuredAskUserWidget .saq-option-row.is-hidden,
    StructuredAskUserWidget .saq-option-desc.is-hidden {
        display: none;
    }

    /* ── Inline option inputs (shared by custom + comment) ────────── */

    /* `!important` beats Textual's default Input `:focus` border + height. */
    StructuredAskUserWidget .saq-inline-input {
        margin: 0;
        width: 1fr;
        height: auto !important;
        padding: 0 !important;
        border: none !important;
        background: transparent !important;
        color: $foreground !important;
    }

    StructuredAskUserWidget .saq-inline-input:disabled {
        color: $text-muted;
    }

    StructuredAskUserWidget .saq-custom-hint {
        display: none;
        height: auto;
        padding: 0;
        margin: 0 0 0 2;
        color: $warning 80%;
        text-style: italic;
    }

    StructuredAskUserWidget .saq-custom-hint.is-visible {
        display: block;
    }

    StructuredAskUserWidget .saq-footer {
        height: 1;
        margin: 0;
    }

    StructuredAskUserWidget .saq-count {
        height: 1;
        padding: 0;
        margin: 0 1 0 0;
        color: $text-muted;
    }

    /* ── Footer ──────────────────────────────────────────────────── */

    /* Keyboard hint shown next to the count once every question is answered. */
    StructuredAskUserWidget .saq-enter-hint {
        display: none;
        height: 1;
        padding: 0;
        margin: 0 1 0 0;
        color: $success;
    }

    StructuredAskUserWidget .saq-enter-hint.is-visible {
        display: block;
    }

    StructuredAskUserWidget.is-reviewing .saq-enter-hint {
        color: $accent;
    }

    StructuredAskUserWidget .saq-footer Button {
        margin: 0;
        min-width: 0;
        width: auto;
        height: 1;
        padding: 0 1;
        border: none;
        background: transparent;
        color: $text-muted;
    }

    StructuredAskUserWidget .saq-footer Button:focus {
        color: $primary;
        text-style: bold;
    }

    StructuredAskUserWidget .saq-footer Button.is-disabled {
        opacity: 0.4;
    }

    StructuredAskUserWidget .saq-recap {
        display: none;
        height: auto;
        padding: 0 0 0 2;
        margin: 1 0 0 0;
        color: $text;
    }

    StructuredAskUserWidget .saq-recap.is-visible {
        display: block;
    }

    /* Review phase (two-phase submit): while the recap is open the card
    shifts into a distinct "confirm" look — accent-tinted recap block with
    a left accent rule — so the user can tell at a glance that Enter now
    commits instead of editing. */
    StructuredAskUserWidget.is-reviewing .saq-recap.is-visible {
        background: $accent 15%;
        border-left: thick $accent;
        padding: 0 0 0 1;
    }

    StructuredAskUserWidget .saq-recap-title {
        text-style: bold;
        color: $text-muted;
        margin: 0 0 0 0;
    }

    StructuredAskUserWidget.is-reviewing .saq-recap-title {
        color: $accent;
    }

    StructuredAskUserWidget .saq-recap-row {
        height: auto;
        padding: 0;
        margin: 0;
        color: $text;
    }

    /* ── Separator ──────────────────────────────────────────────── */

    /* Horizontal rule separating the QA widget from surrounding transcript.
    Zero margins keep the card compact so the full interactive card (options
    + footer) fits the approval dock's height budget on small terminals. */
    StructuredAskUserWidget .saq-separator {
        height: 1;
        color: $text-muted 30%;
        padding: 0;
        margin: 0;
    }

    StructuredAskUserWidget .saq-hint {
        height: 1;
        margin: 0 0 1 0;
        color: $text-muted;
    }

    /* Degraded mode */
    StructuredAskUserWidget .saq-degraded-input {
        margin: 0;
        width: 1fr;
        height: 1;
        padding: 0 1;
        background: $surface;
        border: none;
    }

    /* Submitted (collapsed) view */
    StructuredAskUserWidget .saq-answered-box {
        display: none;
        height: auto;
        padding: 0;
        margin: 0 0 1 0;
    }

    StructuredAskUserWidget.is-submitted .saq-tab-bar,
    StructuredAskUserWidget.is-submitted .saq-question-body,
    StructuredAskUserWidget.is-submitted .saq-footer,
    StructuredAskUserWidget.is-submitted .saq-hint,
    StructuredAskUserWidget.is-submitted .saq-recap,
    StructuredAskUserWidget.is-submitted .saq-separator,
    StructuredAskUserWidget.is-submitted .saq-body-path {
        display: none;
    }

    StructuredAskUserWidget.is-submitted .saq-answered-box {
        display: block;
    }

    StructuredAskUserWidget.is-submitted .saq-title {
        margin: 0;
    }

    StructuredAskUserWidget .saq-answered-row {
        height: auto;
        color: $text;
        padding: 0;
        margin: 0;
    }

    /* ── HITL: plan body rendering ─────────────────────────────────── */

    StructuredAskUserWidget .saq-body-box {
        height: auto;
        /* Bounded, scrollable plan preview. Without a cap the card's natural
        height grows with the plan body, pushing the option rows below the
        approval dock's visible region (options become unreachable) and
        inflating the docked bottom container. The cap uses absolute rows —
        vh/% do not resolve during the parent's auto-height measurement. */
        max-height: 10;
        padding: 0 1;
        margin: 0 0 1 0;
        border: solid $primary 40%;
        background: $surface;
        overflow-y: auto;
        overflow-x: hidden;
        scrollbar-visibility: visible;
        scrollbar-size-vertical: 1;
    }

    StructuredAskUserWidget .saq-body {
        height: auto;
        width: 1fr;
        padding: 0;
        margin: 0;
        color: $text;
    }

    StructuredAskUserWidget .saq-body-path {
        height: auto;
        padding: 0;
        margin: 0;
        color: $text-muted;
        text-style: italic;
    }

    /* ── HITL: inline comment input for Refine/Edit ─────────────────── */

    /* Horizontal wrapper: option label on the left, comment input on the
    right.  The input sits beside the option, not below all options. */
    StructuredAskUserWidget .saq-option-with-comment {
        height: auto;
        width: 1fr;
        padding: 0;
        margin: 0;
    }

    /* The option label inside the comment row takes only the space it needs. */
    StructuredAskUserWidget .saq-option-with-comment .saq-option-row {
        width: auto;
        min-width: 0;
    }

    /* `❯` prefix before the comment input — same line height as the row.
    Hidden unless the Refine/Edit option is highlighted. */
    StructuredAskUserWidget .saq-comment-prefix {
        width: auto;
        height: 1;
        padding: 0;
        margin: 0 1 0 1;
        color: $primary;
        visibility: hidden;
    }

    StructuredAskUserWidget .saq-comment-prefix.is-visible {
        visibility: visible;
    }

    /* ── HITL: expand/collapse plan body in answered view ──────────── */

    StructuredAskUserWidget .saq-expand-toggle {
        height: auto;
        color: $primary;
        text-style: italic;
        padding: 0;
        margin: 0;
    }

    /* After submit, hide the plan body preview unless expanded via toggle. */
    StructuredAskUserWidget.is-submitted .saq-body-box {
        display: none;
    }

    StructuredAskUserWidget.is-submitted .saq-body-box.is-expanded {
        display: block;
    }
    """

    class Submitted(Message):
        """Bubbles when the user submits answers or abandons."""

        def __init__(
            self,
            *,
            step_id: str,
            questions: list,
            answers: list[str],
            widget_id: str,
            origin_node: str = "",
        ) -> None:
            super().__init__()
            self.step_id = step_id
            self.questions = questions
            self.answers = answers
            self.widget_id = widget_id
            self.origin_node = origin_node

    def __init__(
        self,
        *,
        step_id: str,
        questions: list,
        widget_id: str | None = None,
        origin_node: str | None = None,
        degraded: bool = False,
        body_markdown: str | None = None,
        body_path: str | None = None,
        allow_custom: bool = True,
        comment_option_index: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._step_id = step_id
        self._questions = questions
        self._degraded = degraded
        self._origin_node = (origin_node or "").strip()
        self._widget_id = widget_id or self.id or ""
        self._current_q = 0
        self._selected: dict[int, int] = {}  # q_idx → option_idx (0–2) or 3 (custom)
        self._custom_texts: dict[int, str] = {}
        self._highlighted = 0  # 0–3 within current question (3 = custom)
        self._rendered_opt_count = 0  # option rows created at compose time
        self._submit_review_open = False
        self._submitted = False
        self._answers: list[str] = []
        self._degraded_inputs: list[Input] = []
        self._custom_input: Input | None = None
        self._submit_btn: Button | None = None
        self._abandon_btn: Button | None = None
        self._footer_focused = False  # False = question area, True = footer
        # HITL extensions
        self._body_markdown = (body_markdown or "").strip()
        self._body_path = (body_path or "").strip()
        self._allow_custom = allow_custom
        self._comment_option_index = comment_option_index  # option idx that shows comment
        self._comment_input: Input | None = None
        self._body_expanded = False
        self._plan_body_widget: Static | None = None
        self._plan_body_text: str = ""

    # ── Properties ───────────────────────────────────────────────────

    @property
    def _is_hitl(self) -> bool:
        """True for plan-review and tool-approval origins."""
        return self._origin_node in _HITL_ORIGINS

    @property
    def _is_plan_review(self) -> bool:
        return self._origin_node == _ORIGIN_PLAN_MODE_REVIEW

    @property
    def _is_tool_approval(self) -> bool:
        return self._origin_node == _ORIGIN_TOOL_APPROVAL

    @property
    def _num_questions(self) -> int:
        return len(self._questions)

    @property
    def _all_answered(self) -> bool:
        return len(self._selected) >= self._num_questions

    def _question_header(self, idx: int) -> str:
        q = self._questions[idx]
        if isinstance(q, dict):
            return q.get("header", f"Q{idx + 1}")
        return str(q)

    def _question_question(self, idx: int) -> str:
        q = self._questions[idx]
        if isinstance(q, dict):
            return q.get("question", "")
        return str(q)

    def _question_options(self, idx: int) -> list[dict[str, str]]:
        """Return list of option dicts with 'label' and 'description' keys."""
        q = self._questions[idx]
        if isinstance(q, dict):
            return q.get("options", [])
        return []

    def _answer_text(self, q_idx: int) -> str:
        """The text to send on resume for the selected answer."""
        sel = self._selected.get(q_idx)
        if sel is None:
            return ""
        if not self._allow_custom and sel == 3:
            # HITL custom shouldn't happen, but guard.
            return ""
        if sel == 3:
            return self._custom_texts.get(q_idx, "")
        options = self._question_options(q_idx)
        if sel < len(options):
            return options[sel].get("label", "")
        return ""

    def _hitl_comment_label(self) -> str:
        """The comment text from the HITL comment input (if any)."""
        if self._comment_input is not None:
            return str(self._comment_input.value or "").strip()
        return ""

    def _answers_collected(self) -> list[str]:
        """All answer texts in question order (empty string if unanswered).

        For HITL origins, appends the comment text as a second slot when
        the Refine/Edit option is selected — the wire shape is
        ``[action, comment]`` for the host decoder.
        """
        answers = [self._answer_text(i) for i in range(self._num_questions)]
        if self._is_hitl and self._comment_option_index is not None:
            sel = self._selected.get(0)
            if sel == self._comment_option_index and self._hitl_comment_label():
                # Append the comment as a trailing slot for the host decoder.
                # Never overwrite answers[1]: with multiple pending tool calls
                # that slot carries the second tool's decision, and clobbering
                # it would flip that call to the fail-safe reject.
                answers.append(self._hitl_comment_label())
        return answers

    # ── Title content ───────────────────────────────────────────────

    def _title_content(self) -> Content:
        if self._is_plan_review:
            title = "Review this plan"
        elif self._is_tool_approval:
            title = self._tool_approval_title()
        elif self._submitted and self._answers:
            title = "Answered (from human)"
        elif self._submit_review_open:
            title = "Review your answers"
        else:
            title = "Awaiting your answer"
        if self._submitted:
            return _assemble_card_header(self, title, status="success")
        colors = theme.get_theme_colors(self)
        if self._submit_review_open:
            # Phase 2 of the two-phase submit: recap open, awaiting the
            # final commit. A distinct accent dot separates this "confirm"
            # state from the answering (warning) state above.
            return _assemble_card_header(self, title, status="review", accent=colors.accent)
        return _assemble_card_header(self, title, status="pending", accent=colors.warning)

    def _tool_approval_title(self) -> str:
        """Build the tool-approval card title from the first question.

        Each HITL question for tool_approval is shaped as a ``QuestionSpec``
        with ``header`` = ``"Approve <name> (<arg>)"``. The card title
        re-shapes it to ``"Approve tool: <name> (<arg>)"``. Long command
        args are truncated to a brief preview (see ``_truncate_tool_arg``)
        so the header stays readable. Multiple pending tool calls collapse
        into one title.
        """
        if not self._questions:
            return "Approve tool"
        q0 = self._questions[0]
        if isinstance(q0, dict):
            body = q0.get("header", "")
        else:
            body = str(q0)
        if body.startswith("Approve "):
            body = body[len("Approve ") :]
        if body.endswith("?"):
            body = body[:-1]
        body = _truncate_tool_arg(body)
        more = len(self._questions) - 1
        if more > 0:
            return f"Approve {more + 1} tools: {body} (+{more} more)"
        return f"Approve tool: {body}"

    def _tool_label(self, idx: int) -> str:
        """Short tool label for a tool-approval question (tab rows / history)."""
        return tool_label_from_header(self._question_header(idx))

    def _path_footer_text(self) -> str:
        if self._body_path:
            return f"Plan saved to: {self._body_path}"
        return "Plan held in memory only"

    def _refresh_title(self) -> None:
        try:
            title_w = self.query_one(".saq-title", Static)
            title_w.update(self._title_content())
        except Exception:  # noqa: BLE001
            pass

    # ── Compose ─────────────────────────────────────────────────────

    def compose(self) -> Any:
        yield Static(self._title_content(), classes="saq-title", markup=False)
        # Answered summary — visible only when is-submitted (CSS toggles).
        yield from self._compose_answered_summary()
        if self._degraded:
            yield from self._compose_degraded()
        else:
            yield from self._compose_structured()

    def _compose_answered_summary(self) -> Any:
        gutter = _card_body_gutter()
        with Vertical(classes="saq-answered-box"):
            if self._is_hitl:
                if self._is_tool_approval and self._num_questions > 1:
                    # Multi-tool gate: one history row per pending tool call —
                    # a single "[Approved]" line would hide which of the
                    # batched calls got which decision.
                    for i in range(self._num_questions):
                        action = self._answers[i] if i < len(self._answers) else ""
                        if action == "Approve":
                            display = "Approved"
                        elif action == "Approve & remember":
                            display = "Approved (remember)"
                        elif action == "Reject":
                            display = "Rejected"
                        else:
                            display = action or "—"
                        yield Static(
                            f"{gutter}[{self._tool_label(i)} → {display}]",
                            classes="saq-answered-row",
                            markup=False,
                        )
                    return
                # HITL answered view: action label + optional comments + body toggle
                answer = self._answers[0] if self._answers else ""
                comments = self._answers[1] if len(self._answers) > 1 else ""
                display_action = answer
                # Tool-approval "Approve" reads as past-tense "Approved";
                # "Approve & remember" marks the loop-scoped grant.
                if self._is_tool_approval and answer == "Approve":
                    display_action = "Approved"
                elif self._is_tool_approval and answer == "Approve & remember":
                    display_action = "Approved (remember)"
                shows_comments = comments and answer in ("Refine", "Edit")
                summary = f"{display_action}: {comments}" if shows_comments else display_action
                yield Static(
                    f"{gutter}[{summary}]",
                    classes="saq-answered-row",
                    markup=False,
                )
                if self._is_plan_review and self._body_markdown.strip():
                    g = get_glyphs()
                    yield Static(
                        f"{g.expand} Plan body — click or press Enter to expand",
                        classes="saq-expand-toggle",
                        markup=True,
                    )
            else:
                for i in range(self._num_questions):
                    yield Static(
                        f"{gutter}[{self._question_header(i)} → {self._answer_text(i) or '—'}]",
                        classes="saq-answered-row",
                        markup=False,
                    )

    def _compose_structured(self) -> Any:
        # Top visual separator — clearly distinguishes the QA widget from
        # surrounding messages in the transcript.
        yield Static("─" * 60, classes="saq-separator", markup=False)
        # HITL plan review: render the plan body above the options.
        if self._is_plan_review and self._body_markdown.strip():
            body = _strip_plan_frontmatter(self._body_markdown)
            with Vertical(classes="saq-body-box"):
                body_widget = Static("", classes="saq-body", markup=False)
                yield body_widget
                self._plan_body_widget = body_widget
                self._plan_body_text = body
            if self._body_path:
                yield Static(
                    self._path_footer_text(),
                    classes="saq-body-path",
                    markup=False,
                )
        # Tab bar (hidden for single question — CSS can't conditionally
        # hide, so we just don't render it when there's only one question).
        if self._num_questions > 1:
            with Horizontal(classes="saq-tab-bar"):
                for i in range(self._num_questions):
                    yield Static(
                        self._tab_label(i),
                        id=f"saq-tab-{i}",
                        classes="saq-tab",
                        markup=False,
                    )
        with Vertical(classes="saq-question-body"):
            header_text = self._question_header(self._current_q)
            question_text = self._question_question(self._current_q)
            yield Static(
                header_text,
                classes="saq-question-title",
                id="saq-qtitle",
                markup=False,
            )
            # Suppress the description body when it duplicates the header
            # (tool-approval QuestionSpecs set header == question) so the
            # prompt text is not rendered twice.
            if question_text and question_text != header_text:
                yield Static(
                    question_text,
                    classes="saq-description",
                    id="saq-desc",
                    markup=False,
                )
            with Vertical(classes="saq-option-list"):
                options = self._question_options(self._current_q)
                # Guard: if options are missing/malformed, fall back to a
                # free-text row so the user can still answer.  This covers
                # LLM outputs that pass the ``is_structured`` key-check but
                # carry empty or non-list options.
                _has_renderable_opts = isinstance(options, list) and len(options) > 0
                if not _has_renderable_opts:
                    logger.warning(
                        "StructuredAskUserWidget: question %r has no renderable "
                        "options (got %r). Falling back to free-text input.",
                        self._question_header(self._current_q),
                        options,
                    )
                # Rows rendered for the initial question; tab switches update
                # these in place (and hide extras when a question has fewer
                # options — see _update_question_display).
                self._rendered_opt_count = len(options) if _has_renderable_opts else 0
                for j, opt in enumerate(options if _has_renderable_opts else []):
                    label = opt.get("label", "") if isinstance(opt, dict) else ""
                    # HITL: the Refine/Edit option renders its comment input
                    # inline to the right of the label (inside a Horizontal),
                    # not below all options.
                    if (
                        self._is_hitl
                        and self._comment_option_index is not None
                        and j == self._comment_option_index
                    ):
                        with Horizontal(classes="saq-option-with-comment"):
                            yield Static(
                                f"  {j + 1}. {label}",
                                id=f"saq-opt-{j}",
                                classes="saq-option-row",
                                markup=False,
                            )
                            yield Static(
                                "❯",
                                id="saq-comment-prefix",
                                classes="saq-comment-prefix",
                                markup=False,
                            )
                            comment_placeholder = (
                                "Revised args…" if self._is_tool_approval else "Comments…"
                            )
                            self._comment_input = Input(
                                placeholder=comment_placeholder,
                                id="saq-comment-input",
                                classes="saq-inline-input",
                                disabled=True,
                            )
                            yield self._comment_input
                    else:
                        yield Static(
                            f"  {j + 1}. {label}",
                            id=f"saq-opt-{j}",
                            classes="saq-option-row",
                            markup=False,
                        )
                    # Long description directly below the label (muted).
                    # Skip the row entirely when empty — a blank Static still
                    # occupies a line, producing gaps between options.
                    desc = opt.get("description", "") if isinstance(opt, dict) else ""
                    if desc.strip():
                        yield Static(
                            f"     {desc}",
                            id=f"saq-optdesc-{j}",
                            classes="saq-option-desc",
                            markup=False,
                        )
                # "Other" custom row (implicit — auto-added, always last).
                # Suppressed for HITL origins (allow_custom=False).
                if self._allow_custom:
                    yield Static(
                        "  Other:",
                        id="saq-opt-custom",
                        classes="saq-option-row",
                        markup=False,
                    )
                    self._custom_input = Input(
                        placeholder="Type a custom answer…",
                        id="saq-custom-input",
                        classes="saq-inline-input",
                        disabled=_has_renderable_opts,
                    )
                    yield self._custom_input
                    # Auto-highlight the custom row when there are no options
                    # so the user can start typing immediately.
                    if not _has_renderable_opts:
                        self._highlighted = 0  # point at "Other"
            # Inline hint shown when custom row is highlighted but text is empty.
            if self._allow_custom:
                yield Static(
                    "Enter a custom answer or pick an option",
                    classes="saq-custom-hint",
                    id="saq-custom-hint",
                    markup=False,
                )
        # Recap (hidden until submit review opens)
        with Vertical(classes="saq-recap", id="saq-recap-box"):
            yield Static("Review:", classes="saq-recap-title", markup=False)
        # Footer
        with Horizontal(classes="saq-footer"):
            yield Static(
                f"{len(self._selected)}/{self._num_questions} answered",
                classes="saq-count",
                id="saq-count",
                markup=False,
            )
            yield Static(
                "← Enter to submit",
                classes="saq-enter-hint",
                id="saq-enter-hint",
                markup=False,
            )
            self._submit_btn = Button(
                "Submit",
                id="saq-submit",
                variant="default",
                compact=True,
            )
            yield self._submit_btn
            self._abandon_btn = Button(
                "Abandon",
                id="saq-abandon",
                variant="default",
                compact=True,
            )
            yield self._abandon_btn
        # Bottom visual separator — mirrors the top rule.
        yield Static("─" * 60, classes="saq-separator", markup=False)

    def _compose_degraded(self) -> Any:
        for i, q in enumerate(self._questions):
            text = q if isinstance(q, str) else self._question_header(i)
            yield Static(
                f"Q{i + 1}: {text}",
                classes="saq-question-title",
                markup=False,
            )
            inp = Input(
                placeholder=f"Your answer for Q{i + 1}…",
                id=f"saq-degraded-input-{i}",
                classes="saq-degraded-input",
            )
            self._degraded_inputs.append(inp)
            yield inp
        yield Static(
            "Enter to submit · Esc to abandon",
            classes="saq-hint",
            markup=False,
        )
        with Horizontal(classes="saq-footer"):
            yield Static(
                f"{len(self._selected)}/{self._num_questions} answered",
                classes="saq-count",
                id="saq-count",
                markup=False,
            )
            self._submit_btn = Button(
                "Submit",
                id="saq-submit",
                variant="default",
                compact=True,
            )
            yield self._submit_btn
            self._abandon_btn = Button(
                "Abandon",
                id="saq-abandon",
                variant="default",
                compact=True,
            )
            yield self._abandon_btn

    def _tab_label(self, idx: int) -> str:
        """Label for a tab: ✓ if answered, ▸ if active, plus title."""
        g = get_glyphs()
        prefix = "▸ " if idx == self._current_q else "  "
        answered = idx in self._selected
        if answered:
            prefix = f"{g.checkmark} " if idx == self._current_q else f"{g.checkmark} "
        # Tool-approval tabs show the short tool name — the full
        # "Approve <name> (<arg>)?" header would blow out the tab bar when
        # several tool calls are batched into one gate.
        title = self._tool_label(idx) if self._is_tool_approval else self._question_header(idx)
        return f"{prefix}{title}"

    # ── on_mount ────────────────────────────────────────────────────

    def on_mount(self) -> None:
        if self._submitted:
            self._refresh_answered_summary()
            return
        # Render plan body markdown for HITL plan review.
        if self._is_plan_review:
            self._render_plan_body()
        self._update_option_highlight()
        self._update_tab_highlight()
        self._update_submit_state()
        # IG-CLNT-005: no focus steal, ever. Cards live in the approval dock
        # so the chat input stays typable while approvals pend; focus arrives
        # via Ctrl+R (action_focus_approvals) or a click.

    def _render_plan_body(self) -> None:
        """Render the plan body markdown via the themed renderer."""
        body_widget = self._plan_body_widget
        text = self._plan_body_text
        if body_widget is None:
            with suppress(Exception):
                body_widget = self.query_one(".saq-body", Static)
        if body_widget is None:
            return
        if not text:
            body_widget.update("(No plan content)")
            return
        try:
            entry, colors, code_theme = resolve_markdown_theme_parts(self)
            body_widget.update(
                ThemedMarkdownRenderer(
                    text,
                    entry=entry,
                    colors=colors,
                    code_theme=code_theme,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("Plan review markdown render failed; using plain text", exc_info=True)
            body_widget.update(text)

    def _refocus_self_no_scroll(self) -> None:
        """Focus the container without scrolling (prevents plan-header jump)."""
        try:
            self.app.set_focus(self, scroll_visible=False)
        except Exception:  # noqa: BLE001
            pass

    # ── HITL: plan body expand/collapse + comment focus guard ──────

    def _toggle_body_expanded(self) -> None:
        """Expand or collapse the plan body in the answered view."""
        self._body_expanded = not self._body_expanded
        try:
            body_box = self.query_one(".saq-body-box", Vertical)
            if self._body_expanded:
                body_box.add_class("is-expanded")
            else:
                body_box.remove_class("is-expanded")
        except Exception:  # noqa: BLE001
            pass
        self._refresh_answered_summary()

    def on_click(self, event: Click) -> None:
        """Toggle plan body expand/collapse in the answered (submitted) view."""
        if not self._is_plan_review or not self._submitted:
            return
        if screen_has_text_selection(self.screen):
            return
        event.stop()
        self._toggle_body_expanded()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        """An inline input holds focus only while its option is highlighted."""
        allowed = self._inline_input_for_highlight()
        if allowed is not None and event.widget is allowed:
            return
        if event.widget in self._inline_inputs():
            self.screen.set_focus(self)

    # ── Rendering updates ──────────────────────────────────────────

    def _update_question_display(self) -> None:
        """Re-render the question body when switching tabs."""
        try:
            qtitle = self.query_one("#saq-qtitle", Static)
            qtitle.update(self._question_header(self._current_q))
        except Exception:  # noqa: BLE001
            pass
        try:
            desc = self.query_one("#saq-desc", Static)
            desc.update(self._question_question(self._current_q))
        except Exception:  # noqa: BLE001
            pass
        # Re-render option rows (labels + inline descriptions) in place.
        # Questions may have different option counts: extra rows from the
        # initially rendered question are hidden via the is-hidden class.
        options = self._question_options(self._current_q)
        num_opts = len(options)
        for j in range(self._rendered_opt_count):
            try:
                opt_w = self.query_one(f"#saq-opt-{j}", Static)
            except Exception:  # noqa: BLE001
                continue
            try:
                desc_w = self.query_one(f"#saq-optdesc-{j}", Static)
            except Exception:  # noqa: BLE001
                desc_w = None
            if j < num_opts:
                # Use _option_label so the ❯ marker is applied consistently.
                opt_w.update(
                    self._option_label(self._current_q, j, highlighted=(j == self._highlighted))
                )
                opt_w.remove_class("is-hidden")
                if desc_w is not None:
                    opt_desc = (
                        options[j].get("description", "") if isinstance(options[j], dict) else ""
                    )
                    if opt_desc.strip():
                        desc_w.update(f"     {opt_desc}")
                        desc_w.remove_class("is-hidden")
                    else:
                        desc_w.add_class("is-hidden")
            else:
                opt_w.update("")
                opt_w.add_class("is-hidden")
                if desc_w is not None:
                    desc_w.update("")
                    desc_w.add_class("is-hidden")
        # Restore selection highlight for this question
        self._highlighted = self._selected.get(self._current_q, 0)
        self._update_option_highlight()
        self._update_tab_highlight()

    def _option_label(self, q_idx: int, opt_idx: int, *, highlighted: bool = False) -> str:
        """Build the display text for an option row.

        When *highlighted* is true, prefix with ``❯`` to match the
        Claude Code AskUserQuestion style; otherwise use a plain indent.
        """
        options = self._question_options(q_idx)
        if opt_idx < len(options):
            label = options[opt_idx].get("label", "") if isinstance(options[opt_idx], dict) else ""
            marker = "❯ " if highlighted else "  "
            return f"{marker}{opt_idx + 1}. {label}"
        return ""

    def _update_option_highlight(self) -> None:
        """Update which option row is highlighted.

        Applies CSS classes (``is-highlighted`` / ``is-selected``) and
        refreshes each option row's text.  The highlighted row gets a ``❯``
        marker; selected (but not highlighted) rows keep their plain prefix.
        No inline "Enter to submit" suffix — the footer hint is the single
        source of truth.
        """
        num_opts = len(self._question_options(self._current_q))
        custom_idx = num_opts  # Custom row is always at index == num_opts
        for j in range(num_opts):
            try:
                opt_w = self.query_one(f"#saq-opt-{j}", Static)
                if j == self._highlighted:
                    opt_w.add_class("is-highlighted")
                    opt_w.remove_class("is-selected")
                else:
                    opt_w.remove_class("is-highlighted")
                    sel = self._selected.get(self._current_q)
                    if sel is not None and sel == j:
                        opt_w.add_class("is-selected")
                    else:
                        opt_w.remove_class("is-selected")
                # Refresh the label text (with ❯ marker when highlighted).
                opt_w.update(
                    self._option_label(
                        self._current_q,
                        j,
                        highlighted=(j == self._highlighted),
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        # Custom row (only when allow_custom)
        if self._allow_custom:
            self._update_custom_row_highlight(custom_idx)
        # Enable custom input only when custom row is highlighted
        if self._custom_input is not None:
            self._custom_input.disabled = self._highlighted != custom_idx
        # Show the custom-empty hint when the custom row is highlighted but
        # the input has no text.
        if self._allow_custom:
            self._update_custom_hint(custom_idx)
        # HITL: enable/disable comment input based on the selected option.
        if self._comment_input is not None and self._comment_option_index is not None:
            self._update_comment_input_state()
            # Show the `❯` prefix only when the comment option is highlighted.
            try:
                prefix = self.query_one("#saq-comment-prefix", Static)
                if self._highlighted == self._comment_option_index:
                    prefix.add_class("is-visible")
                else:
                    prefix.remove_class("is-visible")
            except Exception:  # noqa: BLE001
                pass

    def _update_custom_row_highlight(self, custom_idx: int) -> None:
        """Apply highlight/selected classes and refresh the custom-row text."""
        try:
            custom_w = self.query_one("#saq-opt-custom", Static)
            if self._highlighted == custom_idx:
                custom_w.add_class("is-highlighted")
                custom_w.remove_class("is-selected")
            else:
                custom_w.remove_class("is-highlighted")
                sel = self._selected.get(self._current_q)
                if sel is not None and sel == custom_idx:
                    custom_w.add_class("is-selected")
                else:
                    custom_w.remove_class("is-selected")
            # Build the custom-row text: "❯ Other: <text>" or "  Other:"
            sel = self._selected.get(self._current_q)
            custom_text = (
                str(self._custom_input.value or "").strip()
                if self._custom_input is not None
                else ""
            )
            marker = "❯ " if self._highlighted == custom_idx else "  "
            if custom_text:
                custom_w.update(f"{marker}Other: {custom_text}")
            else:
                custom_w.update(f"{marker}Other:")
        except Exception:  # noqa: BLE001
            pass

    def _update_custom_hint(self, custom_idx: int) -> None:
        """Show/hide the custom-empty hint."""
        try:
            hint = self.query_one("#saq-custom-hint", Static)
            custom_text = (
                str(self._custom_input.value or "").strip()
                if self._custom_input is not None
                else ""
            )
            if self._highlighted == custom_idx and not custom_text:
                hint.add_class("is-visible")
            else:
                hint.remove_class("is-visible")
        except Exception:  # noqa: BLE001
            pass

    def _update_comment_input_state(self) -> None:
        """Enable the comment input when Refine/Edit is highlighted."""
        if self._comment_input is None or self._comment_option_index is None:
            return
        self._comment_input.disabled = self._highlighted != self._comment_option_index

    # ── Inline-input helpers (shared by custom + comment) ───────────

    def _inline_inputs(self) -> tuple[Input, ...]:
        """Existing inline `Input`s attached to option rows."""
        return tuple(inp for inp in (self._custom_input, self._comment_input) if inp is not None)

    def _inline_input_for_highlight(self) -> Input | None:
        """The inline input whose option is highlighted, else None."""
        if (
            self._comment_option_index is not None
            and self._highlighted == self._comment_option_index
        ):
            return self._comment_input
        if self._allow_custom:
            custom_idx = len(self._question_options(self._current_q))
            if self._highlighted == custom_idx:
                return self._custom_input
        return None

    def _focus_inline_input_or_container(self) -> None:
        """Focus the highlighted option's inline input, or the container.

        When leaving an inline input (Refine→Approve/Reject), the input is
        disabled by `_update_comment_input_state`, which makes Textual drop
        focus to None.  Proactively re-focus the container without scrolling
        so the 200 ms guard doesn't recapture and scroll to the plan header.
        """
        target = self._inline_input_for_highlight()
        if target is not None and not target.disabled:
            try:
                self.app.set_focus(target)
                return
            except Exception:  # noqa: BLE001
                pass
        # No inline input — keep focus on the container without scrolling.
        self._refocus_self_no_scroll()

    def _update_tab_highlight(self) -> None:
        """Update tab labels to reflect current question and answered state."""
        if self._num_questions <= 1:
            return
        for i in range(self._num_questions):
            try:
                tab = self.query_one(f"#saq-tab-{i}", Static)
                tab.update(self._tab_label(i))
                if i == self._current_q:
                    tab.add_class("is-active")
                else:
                    tab.remove_class("is-active")
                if i in self._selected:
                    tab.add_class("is-answered")
                else:
                    tab.remove_class("is-answered")
            except Exception:  # noqa: BLE001
                pass

    def _update_submit_state(self) -> None:
        """Enable/disable Submit based on whether all questions are answered."""
        if self._submit_btn is not None:
            if self._all_answered:
                self._submit_btn.remove_class("is-disabled")
            else:
                self._submit_btn.add_class("is-disabled")
        # Update count
        try:
            count = self.query_one("#saq-count", Static)
            count.update(f"{len(self._selected)}/{self._num_questions} answered")
        except Exception:  # noqa: BLE001
            pass
        # Show the "Enter to submit" hint once every question is answered.
        try:
            hint = self.query_one("#saq-enter-hint", Static)
            if self._all_answered:
                hint.add_class("is-visible")
            else:
                hint.remove_class("is-visible")
        except Exception:  # noqa: BLE001
            pass

    def _refresh_answered_summary(self) -> None:
        """Update the collapsed answered view after submit."""
        try:
            box = self.query_one(".saq-answered-box", Vertical)
            # Clear and re-populate
            for child in list(box.children):
                child.remove()
            gutter = _card_body_gutter()
            if self._is_hitl:
                if self._is_tool_approval and self._num_questions > 1:
                    # Multi-tool gate: mirror the per-tool history rows.
                    for i in range(self._num_questions):
                        action = self._answers[i] if i < len(self._answers) else ""
                        if action == "Approve":
                            display = "Approved"
                        elif action == "Approve & remember":
                            display = "Approved (remember)"
                        elif action == "Reject":
                            display = "Rejected"
                        else:
                            display = action or "—"
                        box.mount(
                            Static(
                                f"{gutter}[{self._tool_label(i)} → {display}]",
                                classes="saq-answered-row",
                                markup=False,
                            )
                        )
                    return
                answers = self._answers if hasattr(self, "_answers") else self._answers_collected()
                action = answers[0] if answers else ""
                comments = answers[1] if len(answers) > 1 else ""
                display_action = action
                if self._is_tool_approval and action == "Approve":
                    display_action = "Approved"
                elif self._is_tool_approval and action == "Approve & remember":
                    display_action = "Approved (remember)"
                shows_comments = comments and action in ("Refine", "Edit")
                summary = f"{display_action}: {comments}" if shows_comments else display_action
                box.mount(
                    Static(
                        f"{gutter}[{summary}]",
                        classes="saq-answered-row",
                        markup=False,
                    )
                )
                if self._is_plan_review and self._body_markdown.strip():
                    g = get_glyphs()
                    toggle = Static(
                        f"{g.expand} Plan body — click or press Enter to expand"
                        if not self._body_expanded
                        else f"{g.collapse} Collapse plan body",
                        classes="saq-expand-toggle",
                        markup=True,
                    )
                    box.mount(toggle)
            else:
                for i in range(self._num_questions):
                    answer = self._answer_text(i)
                    label = answer if answer else "—"
                    box.mount(
                        Static(
                            f"{gutter}[{self._question_header(i)} → {label}]",
                            classes="saq-answered-row",
                            markup=False,
                        )
                    )
        except Exception:  # noqa: BLE001
            pass

    # ── Submit recap ───────────────────────────────────────────────

    def _open_submit_review(self) -> None:
        """Render the recap block above the footer."""
        self._submit_review_open = True
        # Review-phase visual distinction: the whole card shifts into a
        # distinct "confirm" look until the review closes or commits.
        self.add_class("is-reviewing")
        try:
            recap_box = self.query_one("#saq-recap-box", Vertical)
            # Clear title and populate rows
            for child in list(recap_box.children):
                child.remove()
            recap_box.mount(
                Static(
                    "Review — Enter to confirm · Esc to edit",
                    classes="saq-recap-title",
                    markup=False,
                )
            )
            for i in range(self._num_questions):
                answer = self._answer_text(i)
                label = answer if answer else "—"
                recap_box.mount(
                    Static(
                        f"  {self._question_header(i)} → {label}",
                        classes="saq-recap-row",
                        markup=False,
                    )
                )
            recap_box.add_class("is-visible")
        except Exception:  # noqa: BLE001
            pass
        self._refresh_title()
        self._set_enter_hint_text("← Enter to confirm")

    def _close_submit_review(self) -> None:
        self._submit_review_open = False
        self.remove_class("is-reviewing")
        try:
            recap_box = self.query_one("#saq-recap-box", Vertical)
            for child in list(recap_box.children):
                child.remove()
            recap_box.mount(Static("Review:", classes="saq-recap-title", markup=False))
            recap_box.remove_class("is-visible")
        except Exception:  # noqa: BLE001
            pass
        self._refresh_title()
        self._set_enter_hint_text("← Enter to submit")

    def _set_enter_hint_text(self, text: str) -> None:
        try:
            self.query_one("#saq-enter-hint", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    # ── Finalize ──────────────────────────────────────────────────

    def _finalize(self) -> None:
        """Collect answers and post Submitted."""
        if self._submitted:
            return
        self._submitted = True
        self.remove_class("is-reviewing")
        self.add_class("is-submitted")
        self._answers = self._answers_collected()
        self._refresh_title()
        self._refresh_answered_summary()
        self.post_message(
            self.Submitted(
                step_id=self._step_id,
                questions=self._questions,
                answers=self._answers,
                widget_id=self._widget_id,
                origin_node=self._origin_node,
            )
        )

    def _abandon(self) -> None:
        """Post Submitted with empty answers."""
        if self._submitted:
            return
        self._submitted = True
        self.remove_class("is-reviewing")
        self.add_class("is-submitted")
        self._answers = []
        self._refresh_title()
        self._refresh_answered_summary()
        self.post_message(
            self.Submitted(
                step_id=self._step_id,
                questions=self._questions,
                answers=[],
                widget_id=self._widget_id,
                origin_node=self._origin_node,
            )
        )

    # ── Keybinding actions ─────────────────────────────────────────

    def action_prev_question(self) -> None:
        if self._degraded or self._submitted or self._submit_review_open:
            return
        if self._num_questions <= 1:
            return
        self._current_q = (self._current_q - 1) % self._num_questions
        self._update_question_display()

    def action_next_question(self) -> None:
        if self._degraded or self._submitted or self._submit_review_open:
            return
        if self._num_questions <= 1:
            return
        self._current_q = (self._current_q + 1) % self._num_questions
        self._update_question_display()

    def action_prev_option(self) -> None:
        if self._degraded or self._submitted or self._submit_review_open:
            return
        num_opts = len(self._question_options(self._current_q))
        total = num_opts + (1 if self._allow_custom else 0)
        self._highlighted = (self._highlighted - 1) % total
        self._update_option_highlight()
        self._focus_inline_input_or_container()

    def action_next_option(self) -> None:
        if self._degraded or self._submitted or self._submit_review_open:
            return
        num_opts = len(self._question_options(self._current_q))
        total = num_opts + (1 if self._allow_custom else 0)
        self._highlighted = (self._highlighted + 1) % total
        self._update_option_highlight()
        self._focus_inline_input_or_container()

    def action_confirm(self) -> None:
        """Enter: select highlighted option, or finalize submit review."""
        if self._submitted:
            # HITL plan review: toggle body expand after submit.
            if self._is_plan_review:
                self._toggle_body_expanded()
            return
        if self._submit_review_open:
            # Enter on the recap = submit
            self._finalize()
            return
        if self._degraded:
            return
        num_opts = len(self._question_options(self._current_q))
        custom_idx = num_opts  # Custom row is at index == num_opts (if allowed)
        if self._allow_custom and self._highlighted == custom_idx:
            # Custom row — focus the input if not yet selected
            if self._custom_input is not None and not self._custom_input.disabled:
                try:
                    self.app.set_focus(self._custom_input)
                except Exception:  # noqa: BLE001
                    self._schedule_focus(self._custom_input)
            return
        # Select the highlighted option
        self._selected[self._current_q] = self._highlighted
        self._update_option_highlight()
        self._update_tab_highlight()
        self._update_submit_state()
        # HITL: if the selected option has a comment field, focus it.
        if (
            self._comment_option_index is not None
            and self._highlighted == self._comment_option_index
            and self._comment_input is not None
        ):
            try:
                self.app.set_focus(self._comment_input)
            except Exception:  # noqa: BLE001
                self._schedule_focus(self._comment_input)
            return
        # HITL: immediate submit on action selection (Approve/Reject) — but
        # only for the single-question gate. With multiple pending tool calls
        # each question is one tool's decision; finalizing here would send
        # empty answers for the unanswered tools, which the host maps to the
        # fail-safe reject. Route through the auto-advance + review flow so
        # every tool gets an explicit decision before submit.
        if self._is_hitl and self._num_questions == 1:
            self._finalize()
            return
        # Auto-advance to next unanswered question; when every question is
        # answered, open the submit review so Enter finalizes.
        if not self._all_answered:
            for i in range(1, self._num_questions + 1):
                next_q = (self._current_q + i) % self._num_questions
                if next_q not in self._selected:
                    self._current_q = next_q
                    self._update_question_display()
                    break
        else:
            self._open_submit_review()
            self._schedule_focus(self)

    def action_abandon(self) -> None:
        if self._submitted:
            return
        if self._submit_review_open:
            # First Escape closes the review so answers stay editable;
            # a second Escape (outside review) abandons.
            self._close_submit_review()
            return
        self._abandon()

    def action_focus_footer(self) -> None:
        """Tab: cycle focus between Submit → Abandon → back to question area.

        In the submit-review state, Tab stays within the recap's Submit and
        Abandon buttons. In degraded mode, Tab cycles between the Submit
        button and the first input.
        """
        if self._submitted:
            return
        if self._submit_btn is None or self._abandon_btn is None:
            return
        if self._footer_focused:
            # Currently on footer — cycle to the other footer button, or
            # back to the question area if we've visited both.
            if self.app.focused is self._submit_btn:
                self._schedule_focus(self._abandon_btn)
            elif self.app.focused is self._abandon_btn:
                self._footer_focused = False
                self._schedule_focus(self)
            else:
                self._schedule_focus(self._submit_btn)
        else:
            self._footer_focused = True
            self._schedule_focus(self._submit_btn)

    # ── Focus management ───────────────────────────────────────────

    def _schedule_focus(self, widget: Any) -> None:
        """Move focus for intra-card navigation (options, footer, inputs).

        Routes through the app's focus coordinator so this request is
        arbitrated against other automatic focus movers; the coordinator's
        convergence retries also cover widgets that are still mounting.
        Without a coordinator (bare harnesses) falls back to the legacy
        delayed focus.
        """
        coordinator = getattr(self.app, "_focus_coordinator", None)
        if coordinator is not None:
            coordinator.request_focus(
                widget, FocusPriority.CLARIFICATION_INPUT, reason="structured ask navigation"
            )
            return
        app = self.app

        def _focus() -> None:
            try:
                app.set_focus(widget)
            except Exception:  # noqa: BLE001
                try:
                    widget.focus()
                except Exception:  # noqa: BLE001
                    logger.debug("StructuredAskUserWidget: focus failed", exc_info=True)

        try:
            self.call_after_refresh(_focus)
        except Exception:  # noqa: BLE001
            _focus()

    # ── Event handlers ─────────────────────────────────────────────

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        if self._submitted:
            return
        btn_id = event.button.id or ""
        if btn_id == "saq-submit":
            event.stop()
            if self._degraded:
                self._finalize_degraded()
                return
            if self._all_answered:
                if self._submit_review_open:
                    self._finalize()
                else:
                    self._open_submit_review()
        elif btn_id == "saq-abandon":
            event.stop()
            self._abandon()

    @on(Input.Changed)
    def _on_input_changed(self, event: Input.Changed) -> None:
        """Update the custom-empty hint when the custom input text changes."""
        if event.input is self._custom_input:
            try:
                hint = self.query_one("#saq-custom-hint", Static)
                text = str(event.input.value or "").strip()
                if text:
                    hint.remove_class("is-visible")
                elif self._highlighted == len(self._question_options(self._current_q)):
                    hint.add_class("is-visible")
            except Exception:  # noqa: BLE001
                pass

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        if self._submitted:
            event.stop()
            return
        # HITL: comment input — Enter submits with the comment.
        if event.input is self._comment_input:
            event.stop()
            text = str(event.input.value or "").strip()
            if not text:
                return
            # Select Refine/Edit so _answers_collected appends the comment.
            self._selected[self._current_q] = self._comment_option_index
            self._finalize()
            return
        if self._degraded:
            # In degraded mode, Enter on an input finalizes if all filled
            idx = -1
            for i, inp in enumerate(self._degraded_inputs):
                if event.input is inp:
                    idx = i
                    break
            if idx < 0:
                return
            event.stop()
            self._selected[idx] = 0
            self._custom_texts[idx] = str(event.input.value or "").strip()
            self._update_submit_state()
            # Move to next blank or finalize
            next_blank = next(
                (
                    j
                    for j, inp in enumerate(self._degraded_inputs)
                    if j != idx and not str(inp.value or "").strip()
                ),
                None,
            )
            if next_blank is not None:
                self._schedule_focus(self._degraded_inputs[next_blank])
            else:
                self._finalize()
            return
        # Structured mode: custom input
        if event.input is self._custom_input:
            text = str(event.input.value or "").strip()
            if text:
                self._selected[self._current_q] = 3
                self._custom_texts[self._current_q] = text
                self._update_option_highlight()
                self._update_tab_highlight()
                self._update_submit_state()
                # Auto-advance to the next unanswered question; when all
                # are answered, open the submit review so Enter finalizes.
                if not self._all_answered:
                    for i in range(1, self._num_questions + 1):
                        next_q = (self._current_q + i) % self._num_questions
                        if next_q not in self._selected:
                            self._current_q = next_q
                            self._update_question_display()
                            break
                else:
                    self._open_submit_review()
                self._schedule_focus(self)
            event.stop()

    def _finalize_degraded(self) -> None:
        """Collect answers from degraded inputs and finalize."""
        if self._submitted:
            return
        answers = []
        for i, inp in enumerate(self._degraded_inputs):
            val = str(inp.value or "").strip()
            answers.append(val)
        # Broadcast single non-empty answer to all questions
        non_empty = [a for a in answers if a]
        if len(non_empty) == 1 and len(answers) > 1:
            answers = non_empty * len(answers)
        if not any(answers):
            return
        self._submitted = True
        self._selected = {i: 0 for i in range(len(answers))}
        self._custom_texts = {i: answers[i] for i in range(len(answers))}
        self._answers = answers
        self.add_class("is-submitted")
        self._refresh_title()
        self._refresh_answered_summary()
        self.post_message(
            self.Submitted(
                step_id=self._step_id,
                questions=self._questions,
                answers=answers,
                widget_id=self._widget_id,
                origin_node=self._origin_node,
            )
        )
