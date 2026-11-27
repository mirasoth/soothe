"""Assistant message widget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Click
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Static

from soothe_cli.display import theme
from soothe_cli.display.card import _card_dot_prefix_content, _card_prefix_width
from soothe_cli.display.markdown_theme import ThemedMarkdownRenderer, resolve_markdown_theme_parts
from soothe_cli.display.tool_display import display_width
from soothe_cli.settings import get_glyphs
from soothe_cli.tui.widgets.messages._helpers import _RUNNING_SPINNER_INTERVAL_SECONDS

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.timer import Timer


def _rich_style_with_textual_selection(
    segment_style: Any,
    selection_background: Any,
) -> Any:
    """Blend `screen--selection` over a Rich segment like Content visuals do.

    Rich `Style.__add__` replaces `bgcolor` outright, which makes markdown
    selections look like an opaque block over code blocks. Use the component
    background Color (with alpha from `$primary 50%`), not `get_component_rich_style`,
    which pre-multiplies alpha away.
    """
    from rich.style import Style as RichStyle
    from textual.style import Style as TextualStyle

    base = TextualStyle.from_rich_style(segment_style) if segment_style else TextualStyle.null()
    if selection_background is None or getattr(selection_background, "a", 0) == 0:
        return base.rich_style
    selection = TextualStyle(selection_background, None)
    merged: RichStyle = (base + selection).rich_style
    return merged


class _SelectableMarkdownBody(Static):
    """Static body that supports text selection over Rich renderables."""

    def render_line(self, y: int) -> Strip:  # type: ignore[override]
        from rich.segment import Segment as _Segment
        from textual.visual import RichVisual

        line = super().render_line(y).apply_offsets(0, y)
        selection = self.text_selection
        if selection is None:
            return line
        # Plain-string Content visuals already stylize selection in the cache.
        if not isinstance(self.visual, RichVisual):
            return line
        span = selection.get_span(y)
        if span is None:
            return line
        start, end = span
        if end == -1:
            end = line.cell_length
        start = max(0, min(start, line.cell_length))
        end = max(start, min(end, line.cell_length))
        if start == end:
            return line
        selection_bg = self.screen.get_component_styles("screen--selection").background
        left = line.crop(0, start)
        middle = line.crop(start, end)
        right = line.crop(end, line.cell_length)
        middle_segments = [
            _Segment(
                text,
                _rich_style_with_textual_selection(style, selection_bg),
                control,
            )
            for text, style, control in middle
        ]
        return Strip(
            list(left) + middle_segments + list(right),
            line.cell_length,
        )

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:  # type: ignore[override]
        lines = [strip.text for strip in self._render_cache.lines]
        if lines:
            text = "\n".join(lines)
            return selection.extract(text), "\n"
        return super().get_selection(selection)


REPORT_VARIANT = "report"
"""CSS class marking a goal-completion report card (tree-rail dot column).

The card itself carries no bar; the prefix-dot column is redrawn as a tree
rail: status dot on the first line, then a dim olive-green vertical line
extending to the message bottom (see :func:`_report_rail_content`).
"""


_REPORT_RAIL_DIM_FACTOR = 0.55
"""Fraction blended toward the background when dimming the rail-line color."""


def _report_rail_content(
    dot: Content,
    rail_line: str,
    rail_tone: str,
    body_height: int,
) -> Content:
    """Report-card dot column: status dot, then rail lines to the bottom.

    The rail extends from beneath the prefix dot to the message bottom,
    mirroring the step-tree branch gutter so the report reads as part of the
    step tree rather than a floating card.
    """
    rail = Content.styled(rail_line, rail_tone)
    parts: list[object] = []
    for line in range(max(1, body_height)):
        if line:
            parts.append("\n")
        parts.append(dot if line == 0 else rail)
    return Content.assemble(*parts)


class AssistantMessage(Vertical):
    """Assistant reply card: markdown or plain text body with inline status dot.

    When `render_markdown` is enabled (default), model output is rendered as
    Markdown via the configured markdown theme preset inside a single
    `Static` widget.
    When disabled, output is shown verbatim. This avoids the heavy widget tree
    that `textual.widgets.Markdown` creates (one child widget per block).

    Pass `variant=REPORT_VARIANT` for goal-completion report cards; the dot
    column then becomes a tree rail — status dot on the first line, then a
    dim olive-green vertical line extending to the message bottom, matching
    the step-tree branch style.
    """

    ALLOW_SELECT = True
    """Enable text selection for copy functionality."""

    DEFAULT_CSS = """
    AssistantMessage {
        height: auto;
        /* Shared 1-col inset: every stream card's prefix dot lines up. */
        padding: 0 1;
        margin: 0 0 1 0;
        background: transparent;
    }

    AssistantMessage.report {
        /* Goal-completion report: no card-level bar. The dot column is
           redrawn as a tree rail (dot, then a dim olive-green vertical line
           to the message bottom — see _report_rail_content), matching the
           step-tree branch style. */
        border: none;
    }

    AssistantMessage .assistant-row {
        height: auto;
        width: 1fr;
    }

    AssistantMessage .assistant-dot {
        width: auto;
        height: auto;
        padding: 0;
        margin: 0;
    }

    AssistantMessage .assistant-body {
        padding: 0;
        margin: 0;
        width: 1fr;
        height: auto;
    }
    """

    # Default flush interval (can be overridden by config)
    DEFAULT_STREAM_FLUSH_INTERVAL: float = 0.2  # 200ms batching for streaming
    DEFAULT_FIRST_FLUSH_INTERVAL: float = 0.05  # 50ms fast first-token
    _FAST_FLUSH_THRESHOLD_CHARS: int = 500  # chars before switching to normal interval

    def __init__(
        self,
        content: str = "",
        *,
        render_markdown: bool | None = None,
        render_ansi: bool = False,
        variant: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize an assistant message.

        Args:
        content: Initial assistant text (rendered as Markdown if enabled).
        render_markdown: When set, overrides CLI `render_markdown` config for
        this card (e.g. simple-bypass `plan_direct` next-action lines).
        render_ansi: When True and markdown is disabled, parse ANSI escape
        sequences in `content` via Rich (e.g. TUI shell command output).
        variant: Optional CSS-class variant (currently `REPORT_VARIANT` for
        goal-completion report cards).
        **kwargs: Additional arguments passed to parent.
        """
        super().__init__(**kwargs)
        if variant:
            self.add_class(variant)
        self._content = content
        self._body: Static | None = None
        self._dot: Static | None = None
        self._streaming_active: bool = False
        self._render_ansi = render_ansi
        self._dot_spinner_position: int = 0
        self._dot_animation_timer: Timer | None = None

        # Batching buffer for streaming content
        self._pending_buffer: str = ""
        self._flush_timer: Timer | None = None
        self._stream_flush_interval: float = self.DEFAULT_STREAM_FLUSH_INTERVAL
        self._first_flush_interval: float = self.DEFAULT_FIRST_FLUSH_INTERVAL

        # Cached markdown theme parts (entry, colors, code_theme) — resolved
        # once on first markdown render and reused for the widget's lifetime.
        self._cached_theme_parts: tuple[Any, Any, str] | None = None

        # Determine markdown rendering and flush interval from config
        self._render_markdown: bool = True
        try:
            from soothe_cli.config.loader import load_config

            config = load_config()
            if render_markdown is not None:
                self._render_markdown = render_markdown
            else:
                self._render_markdown = config.render_markdown
            if hasattr(config, "agent"):
                streaming_cfg = config.agent.loop.output_streaming
                self._stream_flush_interval = streaming_cfg.tui_flush_interval_ms / 1000.0
                self._first_flush_interval = streaming_cfg.tui_first_flush_interval_ms / 1000.0
        except Exception:
            if render_markdown is not None:
                self._render_markdown = render_markdown

    def compose(self) -> ComposeResult:  # noqa: PLR6301  # Textual widget method convention
        """Compose inline prefix glyph and body on one row."""
        with Horizontal(classes="assistant-row"):
            yield Static("", classes="assistant-dot", id="assistant-dot")
            yield _SelectableMarkdownBody(
                "", markup=False, classes="assistant-body", id="assistant-body"
            )

    def _assistant_card_status(self) -> str:
        return "running" if self._streaming_active else "success"

    def _report_rail_line(self) -> str:
        """Padded rail-line string aligned under the prefix dot column."""
        glyph = get_glyphs().box_vertical
        pad = max(0, _card_prefix_width() - display_width(glyph))
        return f"{glyph}{' ' * pad}"

    def _report_rail_tone(self) -> str:
        """Dim olive-green tone for the rail line (success blended to background)."""
        try:
            colors = theme.get_theme_colors(self)
        except Exception:  # noqa: BLE001
            colors = theme.DARK_COLORS
        return (
            Color.parse(colors.card_success)
            .blend(Color.parse(colors.background), _REPORT_RAIL_DIM_FACTOR)
            .hex
        )

    def _refresh_assistant_dot(self) -> None:
        if self._dot is None:
            return
        if REPORT_VARIANT in self.classes:
            body_height = self._body.size.height if self._body is not None else 1
            self._dot.update(
                _report_rail_content(
                    _card_dot_prefix_content(
                        self,
                        self._assistant_card_status(),
                        spinner_position=self._dot_spinner_position,
                        animate_running=self._streaming_active,
                    ),
                    self._report_rail_line(),
                    self._report_rail_tone(),
                    body_height,
                )
            )
            return
        self._dot.update(
            _card_dot_prefix_content(
                self,
                self._assistant_card_status(),
                spinner_position=self._dot_spinner_position,
                animate_running=self._streaming_active,
            )
        )

    def _start_dot_animation(self) -> None:
        if self._dot_animation_timer is not None or not getattr(self, "is_mounted", False):
            return
        self._dot_animation_timer = self.set_interval(
            _RUNNING_SPINNER_INTERVAL_SECONDS,
            self._tick_dot_animation,
        )

    def _stop_dot_animation(self) -> None:
        if self._dot_animation_timer is not None:
            self._dot_animation_timer.stop()
            self._dot_animation_timer = None

    def _tick_dot_animation(self) -> None:
        if not self._streaming_active:
            return
        self._dot_spinner_position += 1
        self._refresh_assistant_dot()

    def on_mount(self) -> None:
        """Wire child widget references."""
        self._dot = self.query_one("#assistant-dot", Static)
        self._body = self.query_one("#assistant-body", Static)
        self._refresh_assistant_dot()
        if self._content:
            self._render_to_body()
        if self._streaming_active:
            self._start_dot_animation()

    def _get_theme_parts(self) -> tuple[Any, Any, str]:
        """Resolve and cache markdown theme parts for repeated renders."""
        if self._cached_theme_parts is None:
            self._cached_theme_parts = resolve_markdown_theme_parts(self)
        return self._cached_theme_parts

    def _render_to_body(self) -> None:
        """Render current content into the body Static widget.

        Two-phase rendering: while streaming, the body shows plain text (O(delta)
        per flush). On stream completion, the full markdown is rendered once.
        This avoids re-parsing the entire accumulated content on every flush.
        """
        if self._body is None:
            return
        if not self._content:
            self._body.update("")
            return
        if self._render_markdown:
            if self._streaming_active:
                # Two-phase: plain text while streaming for O(delta) flushes.
                self._body.update(self._content)
            else:
                entry, colors, code_theme = self._get_theme_parts()
                self._body.update(
                    ThemedMarkdownRenderer(
                        self._content,
                        entry=entry,
                        colors=colors,
                        code_theme=code_theme,
                    )
                )
        elif self._render_ansi:
            from rich.text import Text

            self._body.update(Text.from_ansi(self._content))
        else:
            self._body.update(self._content)
        if REPORT_VARIANT in self.classes and getattr(self, "is_mounted", False):
            # The rail length follows the body height; re-sync once layout
            # has settled on the content just rendered.
            self.call_after_refresh(self._refresh_assistant_dot)

    def on_click(self, event: Click) -> None:
        """Handle click on assistant message."""
        event.stop()

    async def _flush_pending_content(self) -> None:
        """Flush buffered streaming content to the body (plain text while streaming)."""
        self._flush_timer = None
        if not self._pending_buffer:
            return
        self._pending_buffer = ""
        self._render_to_body()

    def _adaptive_flush_interval(self) -> float:
        """Fast interval for the first tokens, normal interval once content grows."""
        if len(self._content) < self._FAST_FLUSH_THRESHOLD_CHARS:
            return self._first_flush_interval
        return self._stream_flush_interval

    async def append_content(self, text: str) -> None:
        """Append content to the message (for streaming with batching).

        Uses internal buffering to batch writes and reduce render frequency.
        The flush interval adapts: a short interval for the first ~500 chars
        (fast perceived first-token) then switches to the normal interval.
        """
        if not text:
            return

        # Accumulate content
        self._content += text
        self._pending_buffer += text
        was_streaming = self._streaming_active
        self._streaming_active = True
        if not was_streaming:
            self._refresh_assistant_dot()
            self._start_dot_animation()

        # Schedule batched flush if not already scheduled
        if self._flush_timer is None:
            self._flush_timer = self.set_timer(
                self._adaptive_flush_interval(),
                self._flush_pending_content,
            )

    async def write_initial_content(self) -> None:
        """Write initial content from constructor and finalize streaming state."""
        if self._content:
            self._streaming_active = True
            self._refresh_assistant_dot()
            await self.stop_stream()

    async def stop_stream(self) -> None:
        """End streaming batched updates.

        Clears the pending buffer without an intermediate render, then does
        a single full markdown render now that `_streaming_active` is False.
        """
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

        self._pending_buffer = ""
        self._streaming_active = False
        self._stop_dot_animation()
        self._render_to_body()
        self._refresh_assistant_dot()

    async def set_content(self, content: str) -> None:
        """Set the full message content (stops any active stream).

        Sets content before `stop_stream` so the single `_render_to_body`
        call inside `stop_stream` renders the new content — no redundant
        double render.
        """
        self._content = content
        await self.stop_stream()
