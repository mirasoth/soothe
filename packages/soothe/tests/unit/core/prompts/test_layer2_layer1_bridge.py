"""Integration tests for StrangeLoop execution guidance.

Execution guidance is delivered in the per-turn user message envelope
(``EXPECTED OUTPUT`` / ``INSTRUCTIONS`` via ``UserMessageBuilder.build_execute_step_message``),
not by mutating ``system_prompt``.
"""

from __future__ import annotations

from soothe.prompts.user_message import UserMessageBuilder
from soothe.sloop.engine.execute.step_predecessor_context import (
    ExecuteStepEnvelopeBody,
    build_dependent_execution_hints,
)
from soothe.sloop.state.schemas import StepAction


def _envelope_body(*, expected_output: str | None) -> ExecuteStepEnvelopeBody:
    """Build the same guidance as ``executor.py`` for a root step without dependencies."""
    return build_dependent_execution_hints(
        StepAction(id="01", description="Step"),
        has_predecessor_evidence=False,
        expected_output=expected_output,
    )


class TestExecutionGuidanceEnvelopeIntegration:
    """Guidance in user envelope via UserMessageBuilder."""

    def test_envelope_includes_expected_output_and_instructions(self) -> None:
        """Executor-format guidance appears in EXPECTED OUTPUT and INSTRUCTIONS sections."""
        body = _envelope_body(expected_output="Page summary")
        builder = UserMessageBuilder()
        envelope = builder.build_execute_step_message(
            "Open the page",
            step_id="01",
            short_description="Open the page",
            expected_output=body.expected_output,
            instructions=body.instructions,
        )
        assert "EXECUTION HINTS:" not in envelope
        assert "EXPECTED OUTPUT:" in envelope
        assert "INSTRUCTIONS:" in envelope
        assert "Suggested subagent:" not in envelope
        assert "Page summary" in envelope
        assert "EXECUTION METADATA:" in envelope
        assert "step_id: 01" in envelope
        task_idx = envelope.index("EXECUTION TASK:")
        expected_idx = envelope.index("EXPECTED OUTPUT:")
        instructions_idx = envelope.index("INSTRUCTIONS:")
        metadata_idx = envelope.index("EXECUTION METADATA:")
        assert task_idx < expected_idx < instructions_idx < metadata_idx

    def test_envelope_expected_output_only(self) -> None:
        """Guidance renders expected_output without any delegation hint."""
        body = _envelope_body(expected_output="File contents")
        builder = UserMessageBuilder()
        envelope = builder.build_execute_step_message(
            "Read file",
            expected_output=body.expected_output,
            instructions=body.instructions,
        )
        assert "EXECUTION HINTS:" not in envelope
        assert "EXPECTED OUTPUT:" in envelope
        assert "File contents" in envelope
        assert "Suggested subagent:" not in envelope

    def test_envelope_omits_guidance_sections_when_empty(self) -> None:
        """No step metadata → no EXPECTED OUTPUT or INSTRUCTIONS sections."""
        builder = UserMessageBuilder()
        envelope = builder.build_execute_step_message(
            "Plain step",
        )
        assert "EXECUTION HINTS:" not in envelope
        assert "EXPECTED OUTPUT:" not in envelope
        assert "INSTRUCTIONS:" not in envelope
        assert "EXECUTION TASK:" in envelope

    def test_default_step_envelope_has_no_completion_status_prose(self) -> None:
        """Steps without a deliverable never see a status line as expected output."""
        step = StepAction(id="01", description="Greet the user")
        body = build_dependent_execution_hints(
            step,
            has_predecessor_evidence=False,
            expected_output=step.expected_output,
        )
        envelope = UserMessageBuilder().build_execute_step_message(
            step.description,
            step_id=step.id,
            short_description=step.description,
            expected_output=body.expected_output,
            instructions=body.instructions,
        )
        assert body.expected_output is None
        assert "EXPECTED OUTPUT:" not in envelope
        assert "Step completed successfully" not in envelope
        assert "EXPECTED OUTPUT specification" not in body.instructions
