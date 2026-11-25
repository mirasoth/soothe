"""Unit tests for last_step_output_content."""

from __future__ import annotations

from soothe.context.models import StepExecution, StepNode
from soothe.sloop.utils.messages import LoopAIMessage, last_step_output_content


def _make_state(loop_messages: list | None = None) -> object:
    class _State:
        def __init__(self, msgs):
            self.loop_messages = msgs or []

    return _State(loop_messages)


def _step(
    step_id: str = "01",
    *,
    status: str = "completed",
    plan_iteration: int = 0,
    output_messages: list | None = None,
    kind: str = "action",
) -> StepNode:
    return StepNode(
        id=step_id,
        description=f"Step {step_id}",
        status=status,
        plan_iteration=plan_iteration,
        execution=StepExecution(output_messages=output_messages or []),
        kind=kind,
    )


class TestLastStepOutputContent:
    def test_empty_goal_steps_falls_back_to_ledger(self) -> None:
        state = _make_state(
            [
                LoopAIMessage(content="Ledger answer.", thread_id="t", phase="execute_step"),
            ]
        )
        result = last_step_output_content(state, None)
        assert result == "Ledger answer."

    def test_empty_dict_falls_back_to_ledger(self) -> None:
        state = _make_state(
            [
                LoopAIMessage(content="Ledger answer.", thread_id="t", phase="execute_step"),
            ]
        )
        result = last_step_output_content(state, {})
        assert result == "Ledger answer."

    def test_no_completed_steps_falls_back_to_ledger(self) -> None:
        state = _make_state(
            [
                LoopAIMessage(content="Ledger answer.", thread_id="t", phase="execute_step"),
            ]
        )
        nodes = {"01": _step("01", status="pending")}
        result = last_step_output_content(state, nodes)
        assert result == "Ledger answer."

    def test_returns_ai_text_from_step_output(self) -> None:
        state = _make_state()
        nodes = {
            "01": _step(
                "01",
                output_messages=[
                    {"role": "assistant", "content": "## Design Draft\n\nContent here."},
                ],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert "Design Draft" in result
        assert "Content here." in result

    def test_concatenates_multiple_ai_messages(self) -> None:
        state = _make_state()
        nodes = {
            "01": _step(
                "01",
                output_messages=[
                    {"role": "assistant", "content": "First part."},
                    {"role": "user", "content": "Ignored."},
                    {"role": "assistant", "content": "Second part."},
                ],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert "First part." in result
        assert "Second part." in result

    def test_picks_last_completed_by_plan_iteration(self) -> None:
        state = _make_state()
        nodes = {
            "01": _step(
                "01",
                plan_iteration=0,
                output_messages=[
                    {"role": "assistant", "content": "First step output."},
                ],
            ),
            "02": _step(
                "02",
                plan_iteration=1,
                output_messages=[
                    {"role": "assistant", "content": "Second step output."},
                ],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert result == "Second step output."

    def test_empty_step_output_falls_back_to_ledger(self) -> None:
        state = _make_state(
            [
                LoopAIMessage(content="Ledger fallback.", thread_id="t", phase="execute_step"),
            ]
        )
        nodes = {"01": _step("01", output_messages=[])}
        result = last_step_output_content(state, nodes)
        assert result == "Ledger fallback."

    def test_accepts_stepdag_object(self) -> None:
        state = _make_state()

        class _FakeDAG:
            def __init__(self, nodes):
                self.nodes = nodes

        dag = _FakeDAG(
            {
                "01": _step(
                    "01",
                    output_messages=[
                        {"role": "assistant", "content": "DAG output."},
                    ],
                ),
            }
        )
        result = last_step_output_content(state, dag)
        assert result == "DAG output."

    def test_handles_list_content_in_messages(self) -> None:
        state = _make_state()
        nodes = {
            "01": _step(
                "01",
                output_messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Block 1."},
                            {"type": "text", "text": "Block 2."},
                        ],
                    },
                ],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert "Block 1." in result
        assert "Block 2." in result

    def test_prefers_execute_deliverable_over_eval_verdict(self) -> None:
        """The eval audit step must not become the report source."""
        state = _make_state()
        nodes = {
            "01": _step(
                "01",
                plan_iteration=0,
                output_messages=[
                    {"role": "assistant", "content": "Updated file /tmp/appr-1.txt"},
                ],
            ),
            "01-EVAL": _step(
                "01-EVAL",
                plan_iteration=1,
                kind="eval",
                output_messages=[
                    {"role": "assistant", "content": "Coverage verdict recorded (complete)."},
                ],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert result == "Updated file /tmp/appr-1.txt"

    def test_eval_only_dag_returns_eval_output(self) -> None:
        state = _make_state()
        nodes = {
            "01-EVAL": _step(
                "01-EVAL",
                plan_iteration=1,
                kind="eval",
                output_messages=[
                    {"role": "assistant", "content": "Coverage verdict recorded (complete)."},
                ],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert result == "Coverage verdict recorded (complete)."

    def test_later_execute_step_wins_over_earlier_eval(self) -> None:
        """Among non-eval steps the latest by plan_iteration still wins."""
        state = _make_state()
        nodes = {
            "01": _step(
                "01",
                plan_iteration=0,
                output_messages=[{"role": "assistant", "content": "First deliverable."}],
            ),
            "01-EVAL": _step(
                "01-EVAL",
                plan_iteration=1,
                kind="eval",
                output_messages=[{"role": "assistant", "content": "Verdict one."}],
            ),
            "02": _step(
                "02",
                plan_iteration=2,
                output_messages=[{"role": "assistant", "content": "Second deliverable."}],
            ),
            "02-EVAL": _step(
                "02-EVAL",
                plan_iteration=3,
                kind="eval",
                output_messages=[{"role": "assistant", "content": "Verdict two."}],
            ),
        }
        result = last_step_output_content(state, nodes)
        assert result == "Second deliverable."
