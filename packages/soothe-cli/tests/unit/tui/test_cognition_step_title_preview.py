"""Step card titles stay on one line whatever the description length."""

from __future__ import annotations

from soothe_cli.display.preview_limits import STEP_CARD_TITLE_MAX_CHARS
from soothe_cli.display.tool_display import display_width
from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage


def test_long_description_renders_as_single_line_preview() -> None:
    long_text = "## Kindle 越狱分析\n\n" + ("选项与步骤 " * 200)
    card = CognitionStepMessage("S-1", long_text, id="step-long")

    body = card._step_title_body("")
    header = str(card._step_header_content())
    assert "\n" not in header
    assert body.endswith("…")
    assert display_width(body) <= (card._available_line_width() or STEP_CARD_TITLE_MAX_CHARS)
    assert card._description == long_text.strip()


def test_short_description_keeps_all_text() -> None:
    card = CognitionStepMessage("S-2", "Scan files", id="step-short")
    header = str(card._step_header_content())
    assert "Scan files" in header
    assert "…" not in header


def test_set_description_reclips_the_title() -> None:
    card = CognitionStepMessage("S-3", "short", id="step-update")
    card.set_description("## 长文\n" + "内容 " * 300)
    header = str(card._step_header_content())
    assert "\n" not in header
    assert "…" in header
