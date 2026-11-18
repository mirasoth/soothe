"""One-line previews for StrangeLoop step descriptions."""

from __future__ import annotations

from soothe.runner._runner_strange_loop import (
    _AGENTIC_STEP_DESC_UI_MAX,
    _clip_sloop_step_description,
)


def test_short_description_is_unchanged() -> None:
    assert _clip_sloop_step_description("  Scan files  ") == "Scan files"


def test_multiline_description_becomes_one_line() -> None:
    text = "## 分析\n\n第一段\n第二段\n\n### 结论"
    out = _clip_sloop_step_description(text)
    assert "\n" not in out
    assert "  " not in out
    assert "结论" in out


def test_long_description_is_clipped_with_ellipsis() -> None:
    out = _clip_sloop_step_description("长输入 " * 500)
    assert len(out) <= _AGENTIC_STEP_DESC_UI_MAX
    assert out.endswith("…")
    assert "\n" not in out
