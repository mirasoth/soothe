"""GraphRecursionError must scope to the current goal, not kill the loop.

When ``invoke_strange_loop_graph`` raises ``GraphRecursionError`` (LangGraph's
per-invoke safety tripwire, e.g. from a routing cycle), ``pump_graph`` must:

1. Mark the in-flight goal ``interrupted`` (resumable via ``retry``/``resume``)
   so the goal cursor survives — not a hard fatal that loses the goal.
2. Surface a goal-scoped ``fatal_error`` event so the runner/TUI shows the
   cause. The loop thread itself survives; the next goal starts fresh with a
   full recursion budget.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.errors import GraphRecursionError

from soothe.sloop.state.execution_checkpoint import GoalIndexEntry
from soothe.sloop.strange_loop import StrangeLoop


def _make_strange_loop() -> StrangeLoop:
    config = MagicMock()
    config.agent.loop.working_memory.enabled = False
    config.agent.loop.context_engine = MagicMock()
    config.agent.loop.execute_prompt_ledger = MagicMock()
    config.agent.loop.execute_prompt_ledger.execute_ai_ledger_max_tokens = 0
    config.persistence.default_backend = "sqlite"
    config.persistence.postgres_base_dsn = None
    config.persistence.soothe_postgres_dsn = None
    config.home = "/tmp/soothe-test"
    config.observability.langfuse.enabled = False
    return StrangeLoop(core_agent=MagicMock(), config=config)


def _running_checkpoint() -> MagicMock:
    now = datetime.now(UTC)
    goal = GoalIndexEntry(
        goal_id="goal-0",
        status="running",
        thread_id="L1",
        started_at=now,
        completed_at=None,
        duration_ms=0,
        tokens_used=0,
    )
    checkpoint = MagicMock()
    checkpoint.status = "running"
    checkpoint.current_goal_index = 0
    checkpoint.goal_history = [goal]
    checkpoint.thread_ids = ["L1"]
    checkpoint.current_thread_id = "L1"
    checkpoint.execution_checkpoint = {"iteration": 5}
    return checkpoint


async def _drive_with_recursion_error() -> tuple[list[tuple[str, object]], MagicMock]:
    """Drive ``run_with_progress`` where ``invoke_strange_loop_graph`` raises.

    Returns ``(events, runtime_ctx)`` so assertions can inspect the goal-record
    interrupt marking.
    """
    sl = _make_strange_loop()
    mock_sm = MagicMock()
    mock_sm.loop_id = "L1"
    mock_sm.load = AsyncMock(return_value=_running_checkpoint())
    mock_sm.save = AsyncMock()
    mock_sm.close = AsyncMock()
    mock_sm.mark_goal_interrupted = AsyncMock()

    ce_instance = MagicMock()
    ce_instance.load = AsyncMock(return_value=True)
    ce_instance.get_ledger_entries = MagicMock(return_value=[])
    ce_instance.get_all_goals = MagicMock(return_value=[])
    ce_instance.create_goal = AsyncMock(return_value=MagicMock(id="g1"))
    ce_instance.activate_goal = AsyncMock()
    ce_instance._semantic = MagicMock()

    events: list[tuple[str, object]] = []
    runtime_ctx: MagicMock = MagicMock()

    with (
        patch.object(sl, "_ce", ce_instance),
        patch(
            "soothe.sloop.strange_loop.StrangeLoopStateManager",
            return_value=mock_sm,
        ),
        patch(
            "soothe.sloop.orchestrator.runner.invoke_strange_loop_graph",
            new=AsyncMock(
                side_effect=GraphRecursionError(
                    "Recursion limit of 10008 reached without hitting a stop condition."
                )
            ),
        ),
        patch("soothe.sloop.strange_loop.LoopRuntimeContext") as runtime_ctx_cls,
        patch(
            "soothe_nano.workspace.workspace_paths.WorkspacePaths.from_config",
            return_value=MagicMock(virtual_mode=False),
        ),
        patch("soothe_nano.skills.catalog.parse_slash_skill_user_line", return_value=None),
        patch("soothe_nano.skills.catalog.try_expand_slash_skill_user_line", return_value=None),
    ):
        runtime_ctx = MagicMock()
        runtime_ctx.emit = AsyncMock()
        runtime_ctx.goal_record = MagicMock()
        runtime_ctx.loop_state = MagicMock(iteration=5)
        runtime_ctx.state_manager = mock_sm
        runtime_ctx_cls.return_value = runtime_ctx

        gen: AsyncIterator[tuple[str, object]] = sl.run_with_progress(
            goal="deploy agentspace",
            thread_id="L1",
            workspace=None,
            max_iterations=99,
            loop_id="L1",
        )
        try:
            async for event in gen:
                events.append(event)
        except Exception:
            pass
        finally:
            await gen.aclose()

    return events, runtime_ctx


@pytest.mark.asyncio
async def test_recursion_error_marks_goal_interrupted() -> None:
    """GraphRecursionError must mark the goal interrupted (resumable), not lost."""
    _, runtime_ctx = await _drive_with_recursion_error()

    runtime_ctx.state_manager.mark_goal_interrupted.assert_awaited_once()
    call = runtime_ctx.state_manager.mark_goal_interrupted.await_args
    assert call.kwargs.get("reason") == "graph_recursion_limit"
    assert call.kwargs.get("iteration") == 5


@pytest.mark.asyncio
async def test_recursion_error_emits_goal_scoped_fatal_event() -> None:
    """A fatal_error event with a goal-scoped message reaches the stream."""
    events, _ = await _drive_with_recursion_error()

    fatal_events = [e for e in events if e[0] == "fatal_error"]
    assert fatal_events, "Expected a fatal_error event on the stream"
    msg = str(fatal_events[0][1].get("error", ""))
    assert "recursion limit" in msg.lower(), f"Unexpected message: {msg}"
    assert "interrupted" in msg.lower(), f"Message must mention interrupted goal: {msg}"
