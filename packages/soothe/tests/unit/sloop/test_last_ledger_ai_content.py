"""Tests for ``last_ledger_ai_content`` phase exclusion.

Preamble AI turns (ancestor goal-report pairs) must not be surfaced as final
user-facing output in ``ledger_direct`` completion mode — they are context,
not the current goal's answer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from soothe.sloop.utils.messages import LoopAIMessage, LoopHumanMessage, last_ledger_ai_content


def _state_with(messages: list[Any]) -> Any:
    state = MagicMock()
    state.loop_messages = messages
    return state


class TestLastLedgerAIContent:
    def test_preamble_ai_turn_excluded(self) -> None:
        """A preamble AI turn must not be returned as the final output."""
        state = _state_with(
            [
                LoopHumanMessage(content="ancestor directive", phase="preamble"),
                LoopAIMessage(content="ancestor report", phase="preamble"),
                LoopHumanMessage(content="current goal", phase="intake"),
            ]
        )
        # No execute-step AI message → empty (preamble excluded).
        assert last_ledger_ai_content(state) == ""

    def test_preamble_not_surfaces_over_real_output(self) -> None:
        state = _state_with(
            [
                LoopAIMessage(content="ancestor report", phase="preamble"),
                LoopAIMessage(content="real step output", phase="execute_step"),
            ]
        )
        assert last_ledger_ai_content(state) == "real step output"

    def test_intake_phase_still_excluded(self) -> None:
        state = _state_with(
            [
                LoopAIMessage(content="intake reasoning", phase="intake"),
            ]
        )
        assert last_ledger_ai_content(state) == ""

    def test_eval_step_output_excluded(self) -> None:
        """Coverage-audit step outputs must not surface as the final answer."""
        state = _state_with(
            [
                LoopAIMessage(
                    content="step deliverable text",
                    phase="execute_step",
                    step_id="s1",
                    step_kind="execute",
                ),
                LoopAIMessage(
                    content="Coverage audit complete — the goal was fully achieved.",
                    phase="execute_step",
                    step_id="s2",
                    step_kind="eval",
                ),
            ]
        )
        assert last_ledger_ai_content(state) == "step deliverable text"

    def test_eval_step_output_excluded_via_additional_kwargs(self) -> None:
        """Serialized round-trip form (step_kind in additional_kwargs) is honored."""
        msg = LoopAIMessage(
            content="eval verdict narration",
            phase="execute_step",
            step_id="s2",
        )
        msg.additional_kwargs["step_kind"] = "eval"
        state = _state_with([msg])
        assert last_ledger_ai_content(state) == ""

    def test_eval_only_ledger_returns_empty(self) -> None:
        """When only eval-tagged AI rows exist, nothing is surfaced."""
        state = _state_with(
            [
                LoopAIMessage(
                    content="I assessed the evidence.",
                    phase="execute_step",
                    step_id="s2",
                    step_kind="eval",
                ),
            ]
        )
        assert last_ledger_ai_content(state) == ""
