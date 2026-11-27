"""Tests for UserMessage card styling."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

from soothe_cli.display import theme
from soothe_cli.tui.widgets.messages.user import UserMessage


def test_user_message_is_quoted_block_with_primary_accent_bar() -> None:
    """UserMessage is a surface block with a primary left accent bar (quote style)."""
    css = UserMessage.DEFAULT_CSS
    assert "background: $surface" in css
    assert "border-left: thick $primary" in css


def test_user_message_ascii_mode_uses_ascii_accent_bar() -> None:
    """The -ascii variant swaps the thick accent bar for the ascii border type."""
    css = UserMessage.DEFAULT_CSS
    assert "UserMessage.-ascii" in css
    assert "border-left: ascii $primary" in css


def test_user_message_has_no_mode_border_rules() -> None:
    """The accent bar is theme-primary only — no per-mode border overrides exist."""
    css = UserMessage.DEFAULT_CSS
    assert "UserMessage.-mode-shell" not in css
    assert "UserMessage.-mode-command" not in css
    assert "$mode-bash" not in css
    assert "$mode-command" not in css


def _render_user_message(content: str) -> tuple[object, theme.ThemeColors]:
    """Render a `UserMessage` with a fixed dark palette (no live app)."""
    colors = theme.DARK_COLORS
    widget = UserMessage(content)
    with (
        patch.object(type(widget), "app", new_callable=PropertyMock) as mock_app,
        patch("soothe_cli.display.theme.get_theme_colors", return_value=colors),
    ):
        mock_app.return_value = MagicMock()
        return widget.render(), colors


def test_user_message_highlights_command_token_after_mode_glyph() -> None:
    """Submitted `/skill:foo args` paints the token, not just the glyph."""
    content, colors = _render_user_message("/skill:diagnose-soothe run now")
    plain = content.plain
    assert plain.startswith("> / skill:diagnose-soothe")
    command_style = f"bold {colors.mode_command}"
    styled = [(span.start, span.end, span.style) for span in content.spans]
    assert any(
        plain[start:end] == "skill:diagnose-soothe" and style == command_style
        for start, end, style in styled
    )


def test_user_message_highlights_file_mention() -> None:
    """@file mentions stay primary-accented in the body."""
    content, colors = _render_user_message("see @README.md please")
    plain = content.plain
    mention_style = f"bold {colors.primary}"
    styled = [(span.start, span.end, span.style) for span in content.spans]
    assert any(
        plain[start:end] == "@README.md" and style == mention_style for start, end, style in styled
    )
