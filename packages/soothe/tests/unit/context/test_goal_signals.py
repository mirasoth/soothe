"""CoverageSignal persistence tests (RFC-SLP-005).

Signals live on GoalNode and round-trip through the sqlite context
store, so a reloaded loop keeps its signal history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from soothe.context.engine import ContextEngine
from soothe.context.models import CoverageSignal
from soothe.context.store_sqlite import SqliteContextPersistence


class TestCoverageSignalModel:
    def test_defaults_and_requires_audit_matrix(self) -> None:
        assert not CoverageSignal(goal_id="g", kind="on_track", confidence=0.1).requires_audit()
        assert CoverageSignal(goal_id="g", kind="coverage_gap", confidence=0.99).requires_audit()
        assert CoverageSignal(goal_id="g", kind="runaway_step", confidence=0.4).requires_audit()
        assert CoverageSignal(goal_id="g", kind="stalled", confidence=0.9).requires_audit()
        assert CoverageSignal(goal_id="g", kind="scope_drift", confidence=0.6).requires_audit()
        assert not CoverageSignal(
            goal_id="g", kind="goal_likely_complete", confidence=0.85
        ).requires_audit()
        assert CoverageSignal(
            goal_id="g", kind="goal_likely_complete", confidence=0.7
        ).requires_audit()

    def test_consumed_by_default_none(self) -> None:
        signal = CoverageSignal(goal_id="g", kind="on_track", confidence=0.9)
        assert signal.consumed_by is None
        assert signal.source == "pce"


class TestCoverageSignalPersistence:
    @pytest.mark.asyncio
    async def test_goalnode_serializes_signals(self) -> None:
        engine = ContextEngine()
        goal = await engine.create_goal("finish the work")
        goal.coverage_signals.append(
            CoverageSignal(goal_id=goal.id, kind="runaway_step", confidence=0.66)
        )
        data = goal.model_dump()
        restored = type(goal).model_validate(data)
        assert restored.coverage_signals[0].kind == "runaway_step"
        assert restored.coverage_signals[0].confidence == pytest.approx(0.66)
        assert restored.coverage_signals[0].requires_audit()

    @pytest.mark.asyncio
    async def test_signals_roundtrip_sqlite_store(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ctx.db"
        engine = ContextEngine()
        goal = await engine.create_goal("finish the work")
        await engine.activate_goal(goal.id)
        goal.coverage_signals.append(
            CoverageSignal(goal_id=goal.id, kind="coverage_gap", confidence=0.8)
        )

        from soothe.context.models import GoalStepDAG

        dag = GoalStepDAG()
        dag.add_goal(goal)
        store = SqliteContextPersistence("loop-1", db_path)
        await store.save_dag(dag)
        loaded = await store.load_dag()
        assert loaded is not None, "dag persisted"
        restored = loaded.get_goal(goal.id)
        assert restored is not None
        assert restored.coverage_signals, "signal persisted with the goal"
        assert restored.coverage_signals[0].kind == "coverage_gap"
        assert restored.coverage_signals[0].requires_audit()
        assert restored.coverage_signals[0].consumed_by is None
        await store.close()
