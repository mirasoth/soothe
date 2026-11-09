"""Unit tests for _extract_structured_signals in eval_decision."""

from __future__ import annotations

from soothe.context.models import StepCloseReport, StepExecution, StepNode
from soothe.sloop.eval.eval_decision import _extract_structured_signals


def _step(
    *,
    status: str = "completed",
    kind: str = "action",
    close_report: StepCloseReport | None = None,
    output_messages: list | None = None,
    outcome: dict | None = None,
) -> StepNode:
    return StepNode(
        id="01",
        description="Test step",
        status=status,
        kind=kind,
        close_report=close_report,
        execution=StepExecution(
            output_messages=output_messages or [],
            outcome=outcome,
        ),
    )


class TestExtractStructuredSignals:
    def test_no_completed_steps_returns_fail_safe(self) -> None:
        signals = _extract_structured_signals([_step(status="pending")])
        assert signals["close_report_requires_eval"] is True
        assert signals["step_kind"] is None
        assert signals["ask_user_interrupt"] is False
        assert signals["artifact_produced"] is False

    def test_close_report_requires_eval_false(self) -> None:
        step = _step(close_report=StepCloseReport(early_exit=False, deferred_items=[]))
        signals = _extract_structured_signals([step])
        assert signals["close_report_requires_eval"] is False

    def test_close_report_requires_eval_true_when_early_exit(self) -> None:
        step = _step(close_report=StepCloseReport(early_exit=True))
        signals = _extract_structured_signals([step])
        assert signals["close_report_requires_eval"] is True

    def test_no_close_report_defaults_to_requires_eval_true(self) -> None:
        step = _step(close_report=None)
        signals = _extract_structured_signals([step])
        assert signals["close_report_requires_eval"] is True

    def test_step_kind_extracted(self) -> None:
        step = _step(kind="eval")
        signals = _extract_structured_signals([step])
        assert signals["step_kind"] == "eval"

    def test_ask_user_interrupt_detected_in_output_messages(self) -> None:
        step = _step(
            output_messages=[
                {"role": "assistant", "content": "Let me ask you a question."},
                {"role": "assistant", "content": "Calling ask_user tool now."},
            ],
        )
        signals = _extract_structured_signals([step])
        assert signals["ask_user_interrupt"] is True

    def test_ask_user_not_detected_when_absent(self) -> None:
        step = _step(
            output_messages=[{"role": "assistant", "content": "Done."}],
        )
        signals = _extract_structured_signals([step])
        assert signals["ask_user_interrupt"] is False

    def test_artifact_produced_detected_in_outcome(self) -> None:
        step = _step(
            outcome={"type": "tool", "result": "Created .platonic/drafts/2026-11-09-design.md"},
        )
        signals = _extract_structured_signals([step])
        assert signals["artifact_produced"] is True

    def test_artifact_produced_detected_in_output_messages(self) -> None:
        step = _step(
            output_messages=[
                {"role": "assistant", "content": "Wrote report to output.md"},
            ],
        )
        signals = _extract_structured_signals([step])
        assert signals["artifact_produced"] is True

    def test_artifact_not_detected_when_absent(self) -> None:
        step = _step(
            outcome={"type": "llm_call", "result": "Just some text."},
        )
        signals = _extract_structured_signals([step])
        assert signals["artifact_produced"] is False

    def test_brainstorm_mode_completion_signals(self) -> None:
        """Loop 46a7 scenario: action step with artifact + ask_user + no requires_eval."""
        step = _step(
            kind="action",
            close_report=StepCloseReport(early_exit=False, deferred_items=[]),
            output_messages=[
                {
                    "role": "assistant",
                    "content": "Produced .platonic/drafts/2026-11-09-wkr-design.md",
                },
                {"role": "assistant", "content": "ask_user: which path next?"},
            ],
        )
        signals = _extract_structured_signals([step])
        assert signals["close_report_requires_eval"] is False
        assert signals["step_kind"] == "action"
        assert signals["ask_user_interrupt"] is True
        assert signals["artifact_produced"] is True
