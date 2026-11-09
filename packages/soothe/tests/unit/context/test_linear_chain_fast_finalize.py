"""Unit tests for _is_linear_chain and _ledger_direct_eligible with DAG nodes."""

from __future__ import annotations

from soothe.context.models import StepCloseReport, StepExecution, StepNode
from soothe.context.planning_completion import _is_linear_chain, _ledger_direct_eligible


def _node(
    id: str,
    *,
    parent_step_id: str | None = None,
    dependencies: list[str] | None = None,
    status: str = "completed",
) -> StepNode:
    return StepNode(
        id=id,
        description=f"Step {id}",
        status=status,
        parent_step_id=parent_step_id,
        dependencies=dependencies or [],
        execution=StepExecution(outcome={"type": "llm_call"}),
        close_report=StepCloseReport(),
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
