"""Tests for short, single-line execution step titles."""

from __future__ import annotations

from soothe.sloop.utils.step_title import (
    ROOT_STEP_TITLE,
    STEP_TITLE_PREVIEW_MAX_CHARS,
    preview_step_title,
)


def test_short_description_is_unchanged() -> None:
    assert preview_step_title("  Scan files  ") == "Scan files"


def test_multiline_description_becomes_one_line() -> None:
    text = "## 分析\n\n第一段\n第二段\n\n### 结论"
    out = preview_step_title(text)
    assert "\n" not in out
    assert "  " not in out
    assert "结论" in out


def test_long_description_is_clipped_with_ellipsis() -> None:
    out = preview_step_title("长输入 " * 500)
    assert len(out) <= STEP_TITLE_PREVIEW_MAX_CHARS
    assert out.endswith("…")
    assert "\n" not in out


def test_root_title_is_short_and_stable() -> None:
    assert ROOT_STEP_TITLE == "Execute user goal"
    assert len(ROOT_STEP_TITLE) < STEP_TITLE_PREVIEW_MAX_CHARS
