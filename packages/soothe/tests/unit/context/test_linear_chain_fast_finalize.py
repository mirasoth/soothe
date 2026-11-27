"""Unit tests for _is_linear_chain and _ledger_direct_eligible with DAG nodes."""

from __future__ import annotations

import logging

from soothe.context.models import StepCloseReport, StepExecution, StepNode
from soothe.context.planning_completion import _is_linear_chain, _ledger_direct_eligible


def _node(
    id: str,
    *,
    parent_step_id: str | None = None,
    dependencies: list[str] | None = None,
    status: str = "completed",
    kind: str = "action",
    tool_call_count: int = 0,
) -> StepNode:
    return StepNode(
        id=id,
        description=f"Step {id}",
        status=status,
        parent_step_id=parent_step_id,
        dependencies=dependencies or [],
        execution=StepExecution(
            outcome={"type": "llm_call"},
            tool_call_count=tool_call_count,
        ),
        close_report=StepCloseReport(goal_portion_complete=True, early_exit=False),
        kind=kind,
    )


class TestIsLinearChain:
    def test_empty_dag_is_linear(self) -> None:
        assert _is_linear_chain({}, max_depth=3) is True

    def test_single_step_is_linear(self) -> None:
        nodes = {"01": _node("01")}
        assert _is_linear_chain(nodes, max_depth=3) is True

    def test_two_step_linear_chain(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
        }
        assert _is_linear_chain(nodes, max_depth=3) is True

    def test_three_step_linear_chain(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
            "03": _node("03", dependencies=["02"]),
        }
        assert _is_linear_chain(nodes, max_depth=3) is True

    def test_parallel_branches_not_linear(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
            "03": _node("03", dependencies=["01"]),
        }
        assert _is_linear_chain(nodes, max_depth=3) is False

    def test_decomposition_children_not_linear(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", parent_step_id="01"),
        }
        assert _is_linear_chain(nodes, max_depth=3) is False

    def test_chain_exceeds_max_depth(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
            "03": _node("03", dependencies=["02"]),
            "04": _node("04", dependencies=["03"]),
        }
        assert _is_linear_chain(nodes, max_depth=3) is False

    def test_two_roots_not_linear(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02"),
        }
        assert _is_linear_chain(nodes, max_depth=3) is False


class TestLedgerDirectEligibleWithDagNodes:
    def test_linear_single_step_eligible(self) -> None:
        nodes = {"01": _node("01")}
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=1,
                last_wave_tool_call_count=0,
            )
            is True
        )

    def test_linear_two_step_eligible(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=2,
                last_wave_tool_call_count=0,
            )
            is True
        )

    def test_parallel_not_eligible(self) -> None:
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
            "03": _node("03", dependencies=["01"]),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=3,
                last_wave_tool_call_count=0,
            )
            is False
        )

    def test_failures_not_eligible(self) -> None:
        nodes = {"01": _node("01", status="failed")}
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=1,
                total_steps=1,
                last_wave_tool_call_count=0,
            )
            is False
        )

    def test_tool_cap_exceeded_not_eligible(self) -> None:
        nodes = {"01": _node("01")}
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=1,
                last_wave_tool_call_count=51,
            )
            is False
        )

    def test_eval_ride_along_does_not_break_linear_chain(self) -> None:
        """ROOT_EVAL's audit step (parent_step_id=root, no deps) is excluded
        from the topology check."""
        nodes = {
            "01": _node("01"),
            "01-EVAL": _node("01-EVAL", parent_step_id="01", kind="eval"),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=2,
                last_wave_tool_call_count=1,
            )
            is True
        )

    def test_parallel_execute_steps_still_not_eligible_with_eval(self) -> None:
        """Excluding eval steps must not mask real parallel branches."""
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
            "03": _node("03", dependencies=["01"]),
            "03-EVAL": _node("03-EVAL", parent_step_id="03", kind="eval"),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=4,
                last_wave_tool_call_count=0,
            )
            is False
        )

    def test_all_eval_dag_not_eligible(self) -> None:
        """An all-eval DAG keeps the fail-safe synthesize path."""
        nodes = {"01-EVAL": _node("01-EVAL", kind="eval")}
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=1,
                last_wave_tool_call_count=0,
            )
            is False
        )

    def test_eval_steps_do_not_consume_step_cap(self) -> None:
        """Three deliverable steps plus an audit step remain ledger-direct eligible."""
        nodes = {
            "01": _node("01"),
            "02": _node("02", dependencies=["01"]),
            "03": _node("03", dependencies=["02"]),
            "03-EVAL": _node("03-EVAL", parent_step_id="03", kind="eval"),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=4,
                last_wave_tool_call_count=0,
            )
            is True
        )

    def test_eval_tool_calls_excluded_from_cap(self) -> None:
        """Audit tool calls do not push a deliverable step past the tool cap."""
        nodes = {
            "01": _node("01"),
            "01-EVAL": _node(
                "01-EVAL",
                parent_step_id="01",
                kind="eval",
                tool_call_count=2,
            ),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=2,
                last_wave_tool_call_count=51,
            )
            is True
        )

    def test_step_depending_on_eval_not_eligible(self) -> None:
        """A dangling dependency on an excluded eval step must not look linear."""
        nodes = {
            "01-EVAL": _node("01-EVAL", kind="eval"),
            "02": _node("02", dependencies=["01-EVAL"]),
        }
        assert (
            _ledger_direct_eligible(
                dag_nodes=nodes,
                failed_steps=0,
                total_steps=2,
                last_wave_tool_call_count=0,
            )
            is False
        )

    def test_failure_gate_logs_only_its_own_reason(self, caplog) -> None:
        """Rejection logs name the decisive gate, not an unrelated false one."""
        with caplog.at_level(logging.INFO, logger="soothe.context.planning_completion"):
            assert (
                _ledger_direct_eligible(
                    dag_nodes={"01": _node("01")},
                    failed_steps=1,
                    total_steps=1,
                    last_wave_tool_call_count=0,
                )
                is False
            )
        assert "failed_steps=1" in caplog.text
        assert "total_steps=1 > max=3" not in caplog.text

    def test_tool_cap_gate_logs_decisive_numbers(self, caplog) -> None:
        """The tool-call rejection log carries the measured count and cap."""
        with caplog.at_level(logging.INFO, logger="soothe.context.planning_completion"):
            assert (
                _ledger_direct_eligible(
                    dag_nodes={"01": _node("01")},
                    failed_steps=0,
                    total_steps=1,
                    last_wave_tool_call_count=51,
                )
                is False
            )
        assert "tool_calls=51 > max=50" in caplog.text
