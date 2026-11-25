"""Tests for coverage-gate deduplication against clarification history.

When the eval LLM re-emits ``coverage_verdict(complete=false,
pending_clarification=…)`` with a question already answered in
``clarification_history``, the gate is skipped instead of re-queued to the
relay. This breaks the coverage-gate re-ask dead loop (d3b2) where veritas
answered the same question on every iteration but the answer never fed
back to the eval step.
"""

from __future__ import annotations

from soothe.sloop.stations.execute.execute import _coverage_gate_already_answered


def _gate_question(question: str, header: str = "Integration") -> dict:
    return {
        "question": question,
        "header": header,
        "options": [
            {"label": "Option A", "description": "first"},
            {"label": "Option B", "description": "second"},
        ],
    }


def _history_entry(
    questions: list,
    answers: list[str],
    *,
    source: str = "veritas",
    confidence: float | None = 0.70,
) -> dict:
    return {
        "questions": questions,
        "answers": answers,
        "source": source,
        "confidence": confidence,
    }


def test_no_history_returns_false() -> None:
    """No prior history → gate is not already answered."""
    assert _coverage_gate_already_answered([_gate_question("Which?")], None) is False
    assert _coverage_gate_already_answered([_gate_question("Which?")], []) is False


def test_no_gate_questions_returns_false() -> None:
    """Empty gate questions → not already answered (caller should skip anyway)."""
    assert _coverage_gate_already_answered([], [_history_entry(["Q"], ["A"])]) is False


def test_matching_question_with_real_answer_returns_true() -> None:
    """Same question text in history with a real answer → skip the gate."""
    history = [
        _history_entry(
            [{"question": "Which integration approach?", "header": "Integration"}],
            ["Depend on veya-streaming"],
        )
    ]
    gate = [_gate_question("Which integration approach?")]
    assert _coverage_gate_already_answered(gate, history) is True


def test_different_question_returns_false() -> None:
    """A new, unasked question → must be queued."""
    history = [
        _history_entry(
            [{"question": "Which integration approach?", "header": "Integration"}],
            ["Depend on veya-streaming"],
        )
    ]
    gate = [_gate_question("Which deployment strategy?")]
    assert _coverage_gate_already_answered(gate, history) is False


def test_retry_sentinel_answer_does_not_match() -> None:
    """A prior ``(retry)`` sentinel is not a real answer → gate must re-queue."""
    history = [
        _history_entry(["Which integration approach?"], ["(retry)"]),
    ]
    gate = [_gate_question("Which integration approach?")]
    assert _coverage_gate_already_answered(gate, history) is False


def test_case_insensitive_match() -> None:
    """Question text matching is case-insensitive (LLM may rephrase casing)."""
    history = [
        _history_entry(["Which Integration Approach?"], ["Shared crate"]),
    ]
    gate = [_gate_question("which integration approach?")]
    assert _coverage_gate_already_answered(gate, history) is True


def test_all_gate_questions_must_be_answered() -> None:
    """When the gate has multiple questions, ALL must have prior answers."""
    history = [
        _history_entry(["Q1"], ["A1"]),
    ]
    gate = [_gate_question("Q1"), _gate_question("Q2")]
    assert _coverage_gate_already_answered(gate, history) is False


def test_all_gate_questions_answered_returns_true() -> None:
    """All gate questions have prior answers → skip the gate."""
    history = [
        _history_entry(["Q1", "Q2"], ["A1", "A2"]),
    ]
    gate = [_gate_question("Q1"), _gate_question("Q2")]
    assert _coverage_gate_already_answered(gate, history) is True


def test_history_with_mixed_retry_and_real_for_different_questions() -> None:
    """One question answered, one retry-sentinel → only the real one counts."""
    history = [
        _history_entry(["Q1", "Q2"], ["A1", "(retry)"]),
    ]
    gate = [_gate_question("Q1"), _gate_question("Q2")]
    assert _coverage_gate_already_answered(gate, history) is False
