"""Newline shortcut regression tests for the chat input.

Terminals that support the kitty keyboard protocol report Shift+Enter /
Alt+Enter / Ctrl+Enter as distinct escape sequences. Terminals that do not
send the same bytes as plain Enter for Shift+Enter, so the input also binds
`ctrl+j` (always a bare LF, in legacy and kitty mode alike) as a universal
newline shortcut.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical

from soothe_cli.tui.widgets.chat_input import ChatTextArea


class _HostApp(App):
    """Minimal host that captures submitted values."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def get_css_variables(self) -> dict[str, str]:
        vars_ = super().get_css_variables()
        vars_.setdefault("mode-bash", "red")
        vars_.setdefault("mode-command", "blue")
        vars_.setdefault("mode-normal", "green")
        return vars_

    def compose(self) -> ComposeResult:
        with Vertical():
            yield ChatTextArea(id="composer")

    def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        self.submitted.append(event.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["shift+enter", "alt+enter", "ctrl+enter", "ctrl+j"])
async def test_newline_keys_insert_newline_without_submitting(key: str) -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        text_area = app.query_one("#composer", ChatTextArea)
        text_area.focus()
        text_area.insert("hello")
        await pilot.pause()

        await pilot.press(key)
        await pilot.pause()

        assert text_area.text == "hello\n", f"key={key}, text={text_area.text!r}"
        assert app.submitted == [], f"key={key} must not submit"


@pytest.mark.asyncio
async def test_plain_enter_still_submits() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        text_area = app.query_one("#composer", ChatTextArea)
        text_area.focus()
        text_area.insert("hello")
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert app.submitted == ["hello"]
        assert "\n" not in text_area.text


@pytest.mark.asyncio
async def test_newline_keys_derive_from_bindings() -> None:
    """`_NEWLINE_KEYS` must stay in sync with the insert_newline BINDINGS."""
    bound = {
        key.strip()
        for binding in ChatTextArea.BINDINGS
        if binding.action == "insert_newline"
        for key in binding.key.split(",")
    }
    assert ChatTextArea._NEWLINE_KEYS == frozenset(bound)
    assert "ctrl+j" in bound
