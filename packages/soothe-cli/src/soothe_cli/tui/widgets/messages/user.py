"""User message widget."""

from __future__ import annotations

from typing import Any

from textual.content import Content
from textual.widgets import Static

from soothe_cli.display import theme
from soothe_cli.settings import (
    MODE_DISPLAY_GLYPHS,
    PREFIX_TO_MODE,
    is_ascii_mode,
)
from soothe_cli.tui.input import EMAIL_PREFIX_PATTERN, FILE_MENTION_PATTERN, command_token_span
from soothe_cli.tui.widgets.messages._helpers import _mode_color


class UserMessage(Static):
    """Widget displaying a user message with enhanced styling."""

    ALLOW_SELECT = True
    """Enable text selection for copy functionality."""

    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0;
        background: $surface;
        border-left: thick $primary;
    }

    UserMessage.-ascii {
        border-left: ascii $primary;
    }
    """
    """Quoted-block card: surface fill with a primary left accent bar, so the
    user's query reads as a distinct quote in the message flow instead of
    blending into the surrounding step/assistant cards."""

    def __init__(self, content: str, **kwargs: Any) -> None:
        """Initialize a user message.

        Args:
        content: The message content
        **kwargs: Additional arguments passed to parent
        """
        super().__init__(**kwargs)
        self._content = content

    def on_mount(self) -> None:
        """Add CSS class for ASCII rendering."""
        if is_ascii_mode():
            self.add_class("-ascii")

    def render(self) -> Content:
        """Render the styled user message with role indicator.

        Returns:
        Styled Content with role header, mode prefix, and highlighted mentions.
        """
        colors = theme.get_theme_colors(self)
        parts: list[str | tuple[str, str]] = []
        content = self._content

        # Add role indicator header
        parts.append(("> ", f"bold {colors.primary}"))

        # Use mode-specific prefix indicator when content starts with a
        # mode trigger character (e.g. "!" for shell, "/" for commands).
        # The display glyph may differ from the trigger (e.g. "$" for shell).
        mode = PREFIX_TO_MODE.get(content[:1]) if content else None
        if mode:
            glyph = MODE_DISPLAY_GLYPHS.get(mode, content[0])
            parts.append((f"{glyph} ", f"bold {_mode_color(mode, self)}"))
            content = content[1:]

        # Highlight the leading command token (after `/` moved into the glyph)
        # and any @file mentions. Shared with the live input box via
        # `command_token_span` so `/skill:name` highlights in full.
        last_end = 0
        if mode == "command":
            start, end = command_token_span(content)
            if end > start:
                parts.append((content[start:end], f"bold {colors.mode_command}"))
                last_end = end

        for match in FILE_MENTION_PATTERN.finditer(content):
            start, end = match.span()
            if start < last_end:
                continue
            token = match.group()

            # Skip @mentions that look like email addresses
            if start > 0:
                char_before = content[start - 1]
                if EMAIL_PREFIX_PATTERN.match(char_before):
                    continue

            if start > last_end:
                parts.append((content[last_end:start], colors.foreground))

            parts.append((token, f"bold {colors.primary}"))
            last_end = end

        # Add remaining text after last match
        if last_end < len(content):
            parts.append((content[last_end:], colors.foreground))

        return Content.assemble(*parts)
