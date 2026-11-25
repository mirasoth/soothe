"""Tests for clarification history in the ROOT_EVAL eval envelope.

The eval envelope feeds the coverage-audit LLM its step brief. When prior
clarification Q&A exist in ``clarification_history`` (answered by veritas
or a human), they must appear in the envelope so the eval LLM does not
re-ask the same question — the root cause of the d3b2 coverage-gate dead
loop where ``coverage_verdict(complete=false, pending_clarification=…)``
cycled indefinitely.
"""

from __future__ import annotations

from soothe.sloop.stations.decompose.root_eval import (
    _eval_envelope,
    _format_prior_clarifications,
)


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


def test_format_empty_history_returns_empty_string() -> None:
    """No history → no section."""
    assert _format_prior_clarifications(None) == ""
    assert _format_prior_clarifications([]) == ""


def test_format_skips_retry_sentinel_answers() -> None:
    """Autopilot ``(retry)`` sentinels are not real answers — skip them."""
    history = [
        _history_entry(
            [{"question": "Which approach?", "header": "Approach"}],
            ["(retry)"],
        )
    ]
    assert _format_prior_clarifications(history) == ""


def test_format_real_answer_produces_section() -> None:
    """A veritas-answered question renders as a prior-clarification line."""
    history = [
        _history_entry(
            [{"question": "Which integration approach?", "header": "Integration"}],
            ["Depend on veya-streaming"],
        )
    ]
    result = _format_prior_clarifications(history)
    assert "PRIOR CLARIFICATIONS" in result
    assert "Which integration approach?" in result
    assert "Depend on veya-streaming" in result
    assert "do NOT re-ask" in result


def test_format_handles_string_questions() -> None:
    """Plain-string questions (not QuestionSpec dicts) are handled."""
    history = [_history_entry(["Which crate?"], ["soothe-core"], source="human", confidence=None)]
    result = _format_prior_clarifications(history)
    assert "Which crate?" in result
    assert "soothe-core" in result
    assert "source=human" in result


def test_format_mixed_real_and_retry_keeps_real_only() -> None:
    """When some answers are retry sentinels and others are real, keep the real ones."""
    history = [
        _history_entry(
            ["Q1", "Q2"],
            ["(retry)", "real answer"],
        )
    ]
    result = _format_prior_clarifications(history)
    assert "Q1" not in result
    assert "Q2" in result
    assert "real answer" in result


def test_eval_envelope_without_history_has_no_prior_section() -> None:
    """The envelope omits the prior-clarifications section when history is empty."""
    envelope = _eval_envelope("do something", [])
    assert "PRIOR CLARIFICATIONS" not in envelope


def test_eval_envelope_with_history_includes_prior_section() -> None:
    """The envelope includes prior Q&A so the eval LLM sees answered questions."""
    history = [
        _history_entry(
            [{"question": "Which approach?", "header": "Approach"}],
            ["Refactor into shared crate"],
        )
    ]
    envelope = _eval_envelope("analyze integration", [], prior_clarifications=history)
    assert "PRIOR CLARIFICATIONS" in envelope
    assert "Which approach?" in envelope
    assert "Refactor into shared crate" in envelope


def test_eval_envelope_with_only_retry_history_omits_section() -> None:
    """Retry-only history does not produce a misleading empty section."""
    history = [
        _history_entry(["Q1"], ["(retry)"]),
    ]
    envelope = _eval_envelope("goal", [], prior_clarifications=history)
    assert "PRIOR CLARIFICATIONS" not in envelope
