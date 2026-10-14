"""Cognition reason (plan) message widget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Static

from soothe_cli.display.card import _assemble_card_header, _card_dot_prefix_content
from soothe_cli.display.markdown_theme import ThemedMarkdownRenderer, resolve_markdown_theme_parts

if TYPE_CHECKING:
    from textual.app import ComposeResult


class _ReasonMarkdownBody(Static):
    """Static body that renders reason text as themed Markdown."""

    DEFAULT_CSS = """
    _ReasonMarkdownBody {
        padding: 0;
        margin: 0;
        width: 1fr;
        height: auto;
    }
    """


class CognitionReasonMessage(Vertical):
    """Single card for plan assessment and plan reasoning.

    Header uses the same stateful prefix glyph plus foreground body as `CognitionStepMessage`.

    The body is rendered as themed Markdown (headings, lists, code spans,
    emphasis) via the active markdown theme preset, so multi-line reasoning
    with Markdown formatting (e.g. numbered subtask lists) renders properly
    instead of as a single plain-text line.
    """

    ALLOW_SELECT = True

    DEFAULT_CSS = """
    CognitionReasonMessage {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        background: transparent;
    }

    CognitionReasonMessage .cognition-plan-row {
        height: auto;
        width: 1fr;
    }

    CognitionReasonMessage .cognition-plan-dot {
        width: auto;
        height: auto;
        padding: 0;
        margin: 0;
    }

    CognitionReasonMessage .cognition-plan-header {
        height: auto;
        margin: 0;
        color: $foreground;
        padding: 0;
        width: 1fr;
    }
    """

    def __init__(
        self,
        *,
        status: str,
        iteration: int,
        plan_action: str = "new",
        assessment_reasoning: str = "",
        plan_reasoning: str = "",
        **kwargs: Any,
    ) -> None:
        """Initialize a plan-reason card.

        Args:
        status: Plan status (continue, replan, done).
        iteration: Agent-loop iteration index.
        plan_action: `keep` or `new` (internal execution strategy, not displayed).
        assessment_reasoning: Phase-1 status justification from plan-assess.
        plan_reasoning: Optional strategy/summary line (e.g. step completion).
        **kwargs: Passed to `Vertical`.
        """
        super().__init__(**kwargs)
        self._status = status
        self._iteration = iteration
        self._plan_action = plan_action if plan_action in ("keep", "new") else ""
        self._assessment_reasoning = assessment_reasoning.strip()
        self._plan_reasoning = plan_reasoning.strip()
        # Cached markdown theme parts (entry, colors, code_theme) — resolved
        # once on first markdown render and reused for the widget's lifetime.
        self._cached_theme_parts: tuple[Any, Any, str] | None = None
        self._dot_widget: Static | None = None
        self._body_widget: _ReasonMarkdownBody | None = None

    def _plan_body_text(self) -> str:
        """Assemble the plan body text from assessment and plan reasoning."""
        parts: list[str] = []
        if self._assessment_reasoning:
            parts.append(self._assessment_reasoning)
        if self._plan_reasoning:
            parts.append(self._plan_reasoning)
        if len(parts) == 2:
            first = parts[0]
            if not first.endswith((".", "!", "?")):
                first = f"{first}."
            return f"{first} {parts[1]}"
        if parts:
            return parts[0]
        return ""

    def _plan_header_content(self) -> Content:
        """Build the header Content (prefix glyph + plain body).

        Kept for backward compatibility with tests that inspect the header
        content directly. The live widget renders the body as Markdown in
        :meth:`_render_body`; this method returns the plain-text equivalent.
        """
        body = self._plan_body_text()
        return _assemble_card_header(self, body, status=self._status)

    def _get_theme_parts(self) -> tuple[Any, Any, str]:
        """Resolve and cache markdown theme parts for repeated renders."""
        if self._cached_theme_parts is None:
            self._cached_theme_parts = resolve_markdown_theme_parts(self)
        return self._cached_theme_parts

    def _render_body(self) -> Any:
        """Render the plan body as themed Markdown.

        Falls back to plain styled Content when Markdown construction fails,
        so a parse error never blanks out the card.
        """
        text = self._plan_body_text()
        if not text:
            return Content("")
        try:
            entry, colors, code_theme = self._get_theme_parts()
            return ThemedMarkdownRenderer(
                text,
                entry=entry,
                colors=colors,
                code_theme=code_theme,
            )
        except Exception:  # noqa: BLE001  # Degrade gracefully on parse errors.
            return _assemble_card_header(self, text, status=self._status)

    def _refresh_dot(self) -> None:
        if self._dot_widget is None:
            return
        self._dot_widget.update(_card_dot_prefix_content(self, self._status))

    def on_mount(self) -> None:
        """Wire child widget references and render initial content."""
        self._dot_widget = self.query_one("#cognition-plan-dot", Static)
        self._body_widget = self.query_one("#cognition-plan-header", _ReasonMarkdownBody)
        self._refresh_dot()
        if self._body_widget is not None:
            self._body_widget.update(self._render_body())

    def compose(self) -> ComposeResult:
        """Compose inline prefix glyph and markdown body on one row."""
        with Horizontal(classes="cognition-plan-row"):
            yield Static("", classes="cognition-plan-dot", id="cognition-plan-dot")
            yield _ReasonMarkdownBody(
                "", markup=False, classes="cognition-plan-header", id="cognition-plan-header"
            )
