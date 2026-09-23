"""Step completion prose renders as themed Markdown (parity with goal-completion report).

When an execute step completes successfully, the detail panel should render
the accumulated prose as Markdown via ``ThemedMarkdownRenderer`` — the same
renderer used for the goal-completion report — rather than the plain
tree-gutter wrapping (``branched_prose_body``) used during streaming.

These tests verify:
  * ``set_complete`` passes a ``ThemedMarkdownRenderer`` to the detail widget.
  * Expanding a completed card re-renders prose as Markdown.
  * The running (streaming) display still uses plain branched prose.
  * Markdown parse failures degrade to the branched-prose fallback.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual.app import App, ComposeResult

from soothe_cli.display.markdown_theme import ThemedMarkdownRenderer
from soothe_cli.tui.widgets.messages import CognitionStepMessage


class _StepCardApp(App[None]):
    """Minimal app that mounts a single step card."""

    def __init__(self, card: CognitionStepMessage) -> None:
        super().__init__()
        self.card = card

    def compose(self) -> ComposeResult:
        yield self.card


_MD_PROSE = (
    "## Result\n\n- Read `config.yaml`\n- Updated 3 files\n\n```python\nprint('done')\n```\n"
)


@pytest.mark.asyncio
async def test_set_complete_renders_markdown_to_detail() -> None:
    """Completed step prose is rendered as ThemedMarkdownRenderer, not plain Content."""
    card = CognitionStepMessage(
        "S-MD-01",
        "Execute task",
        interaction_mode=None,
        id="step-md-complete",
    )
    async with _StepCardApp(card).run_test() as pilot:
        card.set_running()
        card.append_execute_assistant_delta(_MD_PROSE)
        card.set_complete(True, 800, 2, "Done")
        await pilot.pause()

        assert card._detail_widget is not None
        content = getattr(card._detail_widget, "_Static__content", "")
        assert isinstance(content, ThemedMarkdownRenderer)


@pytest.mark.asyncio
async def test_expand_completed_card_renders_markdown() -> None:
    """Expanding a completed card re-renders prose as Markdown."""
    card = CognitionStepMessage(
        "S-MD-02",
        "Execute task",
        interaction_mode=None,
        id="step-md-expand",
    )
    async with _StepCardApp(card).run_test() as pilot:
        card.set_running()
        card.append_execute_assistant_delta(_MD_PROSE)
        card.set_complete(True, 500, 1, "Done")
        await pilot.pause()

        assert card._card_collapsed is True

        card.toggle_collapse()
        await pilot.pause()

        assert not card._card_collapsed
        assert card._detail_widget is not None
        assert card._detail_widget.display is True
        content = getattr(card._detail_widget, "_Static__content", "")
        assert isinstance(content, ThemedMarkdownRenderer)


@pytest.mark.asyncio
async def test_running_display_uses_plain_prose() -> None:
    """While streaming (before completion), prose stays as plain branched Content."""
    card = CognitionStepMessage(
        "S-MD-03",
        "Execute task",
        interaction_mode=None,
        id="step-md-running",
    )
    async with _StepCardApp(card).run_test() as pilot:
        card.set_running()
        card.append_execute_assistant_delta("Partial output so far...")
        await pilot.pause()

        assert card._detail_widget is not None
        content = getattr(card._detail_widget, "_Static__content", "")
        # Running display must NOT be a Markdown renderer (O(delta) streaming).
        assert not isinstance(content, ThemedMarkdownRenderer)


def test_markdown_render_failure_falls_back_to_prose() -> None:
    """A Markdown construction error degrades to branched-prose Content."""
    card = CognitionStepMessage("S-MD-04", "Execute task", interaction_mode=None)

    fallback = MagicMock()
    card._step_branched_execute_body = MagicMock(return_value=fallback)

    with pytest.MonkeyPatch.context() as mp:
        import soothe_cli.tui.widgets.messages.cognition_step as mod

        mp.setattr(mod, "build_markdown", MagicMock(side_effect=RuntimeError("boom")))
        result = card._step_markdown_execute_body("## Heading\n\nbody")

    assert result is fallback
    card._step_branched_execute_body.assert_called_once()
