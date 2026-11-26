"""Tests for interaction-mode-aware spinner labels (plan / ask turns)."""

from __future__ import annotations

import pytest

from soothe_cli.display.spinner_labels import (
    SPINNER_LABEL_EXECUTING,
    SPINNER_LABEL_SUBMITTING,
    SPINNER_LABEL_THINKING,
    SPINNER_LABEL_TOOLS,
    mode_aware_spinner_label,
)


@pytest.mark.parametrize(
    ("label", "mode", "expected"),
    [
        # Execution-flavored words become research words with a read-only suffix.
        (SPINNER_LABEL_EXECUTING, "plan", "Planning (read-only)"),
        (SPINNER_LABEL_TOOLS, "plan", "Planning (read-only)"),
        (SPINNER_LABEL_THINKING, "plan", "Planning (read-only)"),
        (SPINNER_LABEL_EXECUTING, "ask", "Researching (read-only)"),
        (SPINNER_LABEL_TOOLS, "ask", "Researching (read-only)"),
        # Lifecycle words pass through unchanged.
        (SPINNER_LABEL_SUBMITTING, "plan", SPINNER_LABEL_SUBMITTING),
        (SPINNER_LABEL_SUBMITTING, "ask", SPINNER_LABEL_SUBMITTING),
        # Agent mode (None) and unknown modes pass everything through.
        (SPINNER_LABEL_EXECUTING, None, SPINNER_LABEL_EXECUTING),
        (SPINNER_LABEL_EXECUTING, "agent", SPINNER_LABEL_EXECUTING),
        (SPINNER_LABEL_EXECUTING, "bypass", SPINNER_LABEL_EXECUTING),
        (SPINNER_LABEL_EXECUTING, "garbage", SPINNER_LABEL_EXECUTING),
        # None label passes through for every mode.
        (None, "plan", None),
        (None, None, None),
    ],
)
def test_mode_aware_spinner_label(
    label: str | None, mode: str | None, expected: str | None
) -> None:
    assert mode_aware_spinner_label(label, mode) == expected


def test_plan_and_ask_use_distinct_words() -> None:
    assert mode_aware_spinner_label(SPINNER_LABEL_EXECUTING, "plan") != mode_aware_spinner_label(
        SPINNER_LABEL_EXECUTING, "ask"
    )
