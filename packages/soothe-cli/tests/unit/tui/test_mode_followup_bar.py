"""Tests for the ask-answer mode follow-up bar (`ModeFollowUpBar`).

The bar renders as a plan-review-style keyboard option picker (numbered
rows, ``❯`` highlight) instead of buttons: click an option to apply it, or
focus the card and use ↑/↓/Enter/1/2/Esc.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from soothe_cli.tui.widgets.messages.mode_followup_bar import ModeFollowUpBar

_PLAN_APPLY = ("plan", "Composer set to Plan mode — describe the goal to draft a plan.")
_EXEC_APPLY = ("auto", "Composer set to agent mode — describe the change to execute.")


class _BarApp(App[Any]):
    """Minimal app exposing the hooks the bar's handlers call."""

    def __init__(self) -> None:
        super().__init__()
        self.applied: list[tuple[str, str | None]] = []
        self.focused_input = False

    def compose(self) -> ComposeResult:
        yield ModeFollowUpBar()

    def apply_composer_mode(self, mode: str, *, notify_message: str | None = None) -> None:
        self.applied.append((mode, notify_message))

    def focus_primary_input(self) -> None:
        self.focused_input = True


@pytest.mark.asyncio
async def test_bar_composes_plan_and_execute_option_rows() -> None:
    app = _BarApp()
    async with app.run_test():
        bar = app.query_one(ModeFollowUpBar)
        row_ids = [row.id for row in bar.query(".mfb-option")]
        assert row_ids == ["mode-followup-plan", "mode-followup-exec"]
        # No buttons — options are Static rows, not Button widgets.
        assert len(bar.query("Button")) == 0


@pytest.mark.asyncio
async def test_click_plan_row_flips_composer_to_plan_and_removes_bar() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        await pilot.click("#mode-followup-plan")
        await pilot.pause()
        assert app.applied == [_PLAN_APPLY]
        assert app.focused_input is True
        assert len(app.query(ModeFollowUpBar)) == 0


@pytest.mark.asyncio
async def test_click_exec_row_flips_composer_to_agent_and_removes_bar() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        await pilot.click("#mode-followup-exec")
        await pilot.pause()
        assert app.applied == [_EXEC_APPLY]
        assert app.focused_input is True
        assert len(app.query(ModeFollowUpBar)) == 0


@pytest.mark.asyncio
async def test_keyboard_enter_applies_highlighted_option() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModeFollowUpBar)
        bar.focus()
        await pilot.pause()
        # Highlight starts on Draft a plan — Enter applies it.
        await pilot.press("enter")
        await pilot.pause()
        assert app.applied == [_PLAN_APPLY]
        assert len(app.query(ModeFollowUpBar)) == 0


@pytest.mark.asyncio
async def test_keyboard_down_then_enter_applies_execute() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModeFollowUpBar)
        bar.focus()
        await pilot.pause()
        await pilot.press("down")  # highlight Execute in agent mode
        await pilot.pause()
        row = bar.query_one("#mode-followup-exec", Static)
        assert "❯" in str(row.render())
        await pilot.press("enter")
        await pilot.pause()
        assert app.applied == [_EXEC_APPLY]
        assert len(app.query(ModeFollowUpBar)) == 0


@pytest.mark.asyncio
async def test_digit_keys_apply_the_numbered_option() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModeFollowUpBar)
        bar.focus()
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert app.applied == [_EXEC_APPLY]
        assert len(app.query(ModeFollowUpBar)) == 0


@pytest.mark.asyncio
async def test_escape_dismisses_without_changing_mode() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModeFollowUpBar)
        bar.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.applied == []
        assert app.focused_input is True
        assert len(app.query(ModeFollowUpBar)) == 0


@pytest.mark.asyncio
async def test_highlight_moves_with_arrow_keys() -> None:
    app = _BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModeFollowUpBar)
        bar.focus()
        await pilot.pause()
        plan_row = bar.query_one("#mode-followup-plan", Static)
        assert "❯" in str(plan_row.render())
        await pilot.press("down")
        await pilot.pause()
        assert "❯" not in str(plan_row.render())
        exec_row = bar.query_one("#mode-followup-exec", Static)
        assert "❯" in str(exec_row.render())
        await pilot.press("up")
        await pilot.pause()
        assert "❯" in str(plan_row.render())
