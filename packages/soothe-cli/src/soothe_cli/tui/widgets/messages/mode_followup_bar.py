"""Inline follow-up options shown after an Ask-mode answer card.

Ask mode is answer-only by design; the bar closes the ask → plan → execute
ladder without asking the user to Shift+Tab manually. Styled like the
plan-review card (``StructuredAskUserWidget``): numbered options with a
``❯`` keyboard highlight instead of buttons.

- **Draft a plan** flips the sticky composer mode to Plan (read-only
  research → plan review);
- **Execute in agent mode** flips it to the default agent mode.

Interaction mirrors the plan-review card: click an option row to apply it
directly, or click the card and drive it from the keyboard (↑/↓ to move the
highlight, Enter or ``1``/``2`` to apply, Esc to dismiss). Selecting only
changes the sticky composer mode (badge + notify) and refocuses the chat
input — the user still types the next turn themselves. The bar removes
itself after a selection so the transcript stays clean.

Like every transcript card, the bar never steals focus from the chat input
on mount (IG-CLNT-005); keyboard mode starts after a click on the card.
"""

from __future__ import annotations

import logging
from typing import Any

from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.events import Click
from textual.widgets import Static

from soothe_cli.display import theme
from soothe_cli.display.card import _assemble_card_header
from soothe_cli.tui.widgets.clipboard import screen_has_text_selection

logger = logging.getLogger(__name__)

_PLAN_ROW_ID = "mode-followup-plan"
_EXEC_ROW_ID = "mode-followup-exec"

# (row id, composer mode, label, description) — order is the keyboard order.
_OPTIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        _PLAN_ROW_ID,
        "plan",
        "Draft a plan",
        "Switch to Plan mode — your next message drafts a plan for review.",
    ),
    (
        _EXEC_ROW_ID,
        "auto",
        "Execute in agent mode",
        "Switch to agent mode — your next message runs with full tools.",
    ),
)

_NOTIFY_BY_MODE: dict[str, str] = {
    "plan": "Composer set to Plan mode — describe the goal to draft a plan.",
    "auto": "Composer set to agent mode — describe the change to execute.",
}


class ModeFollowUpBar(Vertical):
    """Keyboard-selectable mode follow-ups under an Ask-mode answer."""

    can_focus = True

    BINDINGS = [
        Binding("up", "prev_option", "Prev option", show=False),
        Binding("down", "next_option", "Next option", show=False),
        Binding("enter", "apply_highlighted", "Apply", show=False),
        Binding("1", "pick(0)", "Draft a plan", show=False),
        Binding("2", "pick(1)", "Execute in agent mode", show=False),
        Binding("escape", "dismiss", "Dismiss", show=False),
    ]

    DEFAULT_CSS = """
    ModeFollowUpBar {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        background: transparent;
    }

    ModeFollowUpBar .mfb-separator {
        height: 1;
        color: $text-muted 30%;
        padding: 0;
        margin: 0;
    }

    ModeFollowUpBar .mfb-title {
        height: auto;
        color: $warning;
        text-style: bold;
        padding: 0;
        margin: 0;
    }

    ModeFollowUpBar .mfb-option {
        height: auto;
        width: 1fr;
        padding: 0;
        margin: 0;
        color: $text;
    }

    ModeFollowUpBar .mfb-option.is-highlighted {
        color: $text;
        text-style: bold;
    }

    ModeFollowUpBar .mfb-option-desc {
        height: auto;
        width: 1fr;
        padding: 0;
        margin: 0;
        color: $text-muted;
    }

    ModeFollowUpBar .mfb-hint {
        height: 1;
        padding: 0;
        margin: 0;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._highlighted = 0
        self._applied = False

    # ── Compose ──────────────────────────────────────────────────────

    def _title_content(self) -> Content:
        """Card header — same assembly as the plan-review card."""
        try:
            colors = theme.get_theme_colors(self)
        except Exception:  # noqa: BLE001
            colors = theme.DARK_COLORS
        return _assemble_card_header(self, "Next step", status="pending", accent=colors.warning)

    def compose(self) -> Any:
        yield Static("─" * 60, classes="mfb-separator", markup=False)
        yield Static(self._title_content(), classes="mfb-title", markup=False)
        for j, (row_id, _mode, label, desc) in enumerate(_OPTIONS):
            yield Static(
                self._option_text(j),
                id=row_id,
                classes="mfb-option" + (" is-highlighted" if j == self._highlighted else ""),
                markup=False,
            )
            yield Static(
                f"     {desc}",
                classes="mfb-option-desc",
                markup=False,
            )
        yield Static(
            "  ↑/↓ choose · Enter apply · Esc dismiss",
            classes="mfb-hint",
            markup=False,
        )
        yield Static("─" * 60, classes="mfb-separator", markup=False)

    def _option_text(self, index: int) -> str:
        """Row text with the ``❯`` marker on the highlighted option."""
        label = _OPTIONS[index][2]
        marker = "❯ " if index == self._highlighted else "  "
        return f"  {marker}{index + 1}. {label}"

    # ── Highlight updates ────────────────────────────────────────────

    def _refresh_highlight(self) -> None:
        """Re-render the option rows and toggle the highlight class."""
        for j, (row_id, *_rest) in enumerate(_OPTIONS):
            try:
                row = self.query_one(f"#{row_id}", Static)
            except Exception:  # noqa: BLE001
                continue
            row.update(self._option_text(j))
            if j == self._highlighted:
                row.add_class("is-highlighted")
            else:
                row.remove_class("is-highlighted")

    # ── Keybinding actions ─────────────────────────────────────────

    def action_prev_option(self) -> None:
        self._highlighted = (self._highlighted - 1) % len(_OPTIONS)
        self._refresh_highlight()

    def action_next_option(self) -> None:
        self._highlighted = (self._highlighted + 1) % len(_OPTIONS)
        self._refresh_highlight()

    async def action_apply_highlighted(self) -> None:
        await self._apply(self._highlighted)

    async def action_pick(self, index: int) -> None:
        await self._apply(index)

    async def action_dismiss(self) -> None:
        """Remove the bar without changing the mode; refocus the chat input."""
        if self._applied:
            return
        self._applied = True
        app = self.app
        focus = getattr(app, "focus_primary_input", None)
        if callable(focus):
            focus()
        await self._remove_self()

    # ── Click handling ───────────────────────────────────────────────

    @on(Click)
    async def _on_click(self, event: Click) -> None:
        """Apply a clicked option; a click elsewhere starts keyboard mode."""
        if screen_has_text_selection(self.screen):
            return
        event.stop()
        target_id = str(getattr(event.widget, "id", "") or "")
        for index, (row_id, *_rest) in enumerate(_OPTIONS):
            if target_id == row_id:
                await self._apply(index)
                return
        # Click outside the option rows — focus the card so ↑/↓/Enter work
        # (same focus-on-click contract as the plan-review card).
        self.focus(scroll_visible=False)

    # ── Apply ────────────────────────────────────────────────────────

    async def _apply(self, index: int) -> None:
        """Apply the option at *index* and remove the bar."""
        if self._applied:
            return
        self._applied = True
        row_id, mode, _label, _desc = _OPTIONS[index]
        message = _NOTIFY_BY_MODE.get(mode, "")
        await self._apply_mode(mode, message)

    async def _apply_mode(self, mode: str, message: str) -> None:
        """Flip the composer mode, surface the change, refocus input, remove."""
        app = self.app
        apply = getattr(app, "apply_composer_mode", None)
        if callable(apply):
            apply(mode, notify_message=message)
        focus = getattr(app, "focus_primary_input", None)
        if callable(focus):
            focus()
        await self._remove_self()

    async def _remove_self(self) -> None:
        try:
            await self.remove()
        except Exception:  # noqa: BLE001 — removal races are benign
            logger.debug("ModeFollowUpBar already removed", exc_info=True)


__all__ = ["ModeFollowUpBar"]
