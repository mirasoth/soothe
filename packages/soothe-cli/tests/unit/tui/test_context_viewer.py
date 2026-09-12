"""Tests for context viewer goal loading, autopilot detection, and rendering."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from soothe_cli.tui.widgets import context_data, context_viewer


def test_abbreviate_loop_id_uses_prefix_suffix() -> None:
    loop_id = "019f17e6-5432-4a91-b6f2-f265c9876543"
    assert context_viewer._abbreviate_loop_id(loop_id) == "019f17e6...6543"


def test_abbreviate_loop_id_keeps_short_ids() -> None:
    assert context_viewer._abbreviate_loop_id("abc123") == "abc123"


@pytest.mark.asyncio
async def test_load_ce_goals_maps_daemon_history_snapshots() -> None:
    history = SimpleNamespace(
        goals=[
            {
                "goal_id": "g1",
                "goal_text": "First",
                "status": "active",
            }
        ]
    )
    session = SimpleNamespace(fetch_loop_history=AsyncMock(return_value=history))

    goals = await context_data.load_ce_goals("loop-123", session)
    assert len(goals) == 1
    assert goals[0]["id"] == "g1"
    assert goals[0]["description"] == "First"
    assert goals[0]["status"] == "active"
    assert goals[0]["depends_on"] == []
    session.fetch_loop_history.assert_awaited_once_with("loop-123")


@pytest.mark.asyncio
async def test_load_ce_goals_preserves_step_outcomes_and_plan_summary() -> None:
    """Goal dicts should retain step_outcomes / plan_summary for autopilot detection."""
    history = SimpleNamespace(
        goals=[
            {
                "goal_id": "g1",
                "goal_text": "Refactor auth",
                "status": "active",
                "step_outcomes": [{"id": "s1", "description": "step one", "status": "completed"}],
                "plan_summary": "1. step one",
            }
        ]
    )
    session = SimpleNamespace(fetch_loop_history=AsyncMock(return_value=history))
    goals = await context_data.load_ce_goals("loop-1", session)
    assert goals[0]["step_outcomes"] == [
        {"id": "s1", "description": "step one", "status": "completed"}
    ]
    assert goals[0]["plan_summary"] == "1. step one"


@pytest.mark.asyncio
async def test_load_ce_goals_returns_empty_when_history_missing() -> None:
    session = SimpleNamespace(fetch_loop_history=AsyncMock(return_value=SimpleNamespace(goals=[])))
    assert await context_data.load_ce_goals("missing-loop", session) == []


@pytest.mark.asyncio
async def test_load_ce_goals_returns_empty_without_daemon_session() -> None:
    assert await context_data.load_ce_goals("loop-123") == []
    assert await context_data.load_ce_goals("loop-123", None) == []


def test_format_token_usage_includes_breakdown() -> None:
    snapshot = context_data.TokenUsageSnapshot(
        context_tokens=5000,
        conv_tokens=1200,
        model_name="test-model",
        context_limit=128000,
        input_tokens=3800,
        output_tokens=1200,
    )
    rendered = context_data.format_token_usage(snapshot)
    assert "test-model" in rendered
    assert "tokens used this loop" in rendered
    assert "↑" in rendered
    assert "↓" in rendered
    assert "Conversation (est.)" in rendered


def test_summarize_goal_statuses_counts_all_statuses() -> None:
    goals = [
        {"status": "active"},
        {"status": "validated"},
        {"status": "completed"},
    ]
    total, counts = context_data.summarize_goal_statuses(goals)
    assert total == 3
    assert counts == {"active": 1, "validated": 1, "completed": 1}


@pytest.mark.asyncio
async def test_load_token_usage_snapshot_estimates_when_context_zero(monkeypatch) -> None:
    fetch = AsyncMock(return_value=999)
    monkeypatch.setattr(context_data, "fetch_conversation_token_count", fetch)

    snapshot = await context_data.load_token_usage_snapshot(
        context_tokens=0,
        loop_id="loop-123",
        daemon_session=object(),
        model_name="test-model",
    )

    assert snapshot.context_tokens == 999
    assert snapshot.approximate is True


# ── Autopilot detection tests ────────────────────────────────────────────────


def test_detect_autopilot_mode_false_for_regular_goals() -> None:
    """Regular conversation goals have no step_outcomes or plan_summary."""
    goals = [{"id": "g1", "description": "chat", "status": "active", "depends_on": []}]
    assert context_data.detect_autopilot_mode(goals) is False


def test_detect_autopilot_mode_true_when_step_outcomes_present() -> None:
    """Goals with step_outcomes indicate autopilot step DAG."""
    goals = [
        {
            "id": "g1",
            "description": "refactor",
            "status": "active",
            "depends_on": [],
            "step_outcomes": [{"id": "s1", "status": "completed"}],
        }
    ]
    assert context_data.detect_autopilot_mode(goals) is True


def test_detect_autopilot_mode_true_when_plan_summary_present() -> None:
    """Goals with plan_summary indicate autopilot planning."""
    goals = [
        {
            "id": "g1",
            "description": "refactor",
            "status": "active",
            "depends_on": [],
            "plan_summary": "1. analyze 2. implement",
        }
    ]
    assert context_data.detect_autopilot_mode(goals) is True


def test_detect_autopilot_mode_true_when_exec_state_has_steps() -> None:
    """Execution state with a plan containing step nodes indicates autopilot."""
    exec_state = SimpleNamespace(
        plan={"steps": {"nodes": [{"id": "s1"}]}},
        step_index=1,
        iteration=2,
        status="running",
    )
    assert context_data.detect_autopilot_mode([], exec_state) is True


def test_detect_autopilot_mode_false_when_exec_state_has_no_steps() -> None:
    """Execution state without step DAG data is not autopilot."""
    exec_state = SimpleNamespace(
        plan={"steps": {"nodes": []}},
        step_index=0,
        iteration=0,
        status="running",
    )
    assert context_data.detect_autopilot_mode([], exec_state) is False


# ── Step DAG snapshot tests ─────────────────────────────────────────────────


def test_build_step_dag_from_goal_normalizes_step_outcomes() -> None:
    """Step outcomes are normalized to StepDagNode objects."""
    goal = {
        "id": "g1",
        "description": "Refactor auth",
        "status": "active",
        "step_outcomes": [
            {"id": "s1", "description": "analyze", "status": "completed", "dependencies": []},
            {"id": "s2", "description": "implement", "status": "pending", "dependencies": ["s1"]},
        ],
        "plan_summary": "1. analyze 2. implement",
    }
    dag = context_data._build_step_dag_from_goal(goal)
    assert dag.goal_id == "g1"
    assert dag.goal_description == "Refactor auth"
    assert dag.goal_status == "active"
    assert dag.steps_total == 2
    assert dag.steps_completed == 1
    assert dag.steps[0].id == "s1"
    assert dag.steps[0].status == "completed"
    assert dag.steps[1].dependencies == ("s1",)
    assert dag.plan_summary == "1. analyze 2. implement"


def test_build_step_dag_from_goal_handles_zero_steps() -> None:
    """Pre-decomposition goals produce a zero-step snapshot."""
    goal = {"id": "g1", "description": "new goal", "status": "pending"}
    dag = context_data._build_step_dag_from_goal(goal)
    assert dag.steps_total == 0
    assert dag.steps_completed == 0
    assert dag.steps == []


def test_normalize_step_outcomes_handles_missing_fields() -> None:
    """Malformed step outcome dicts degrade gracefully."""
    raw = [
        {"description": "no id"},  # missing id
        {"id": "s2", "status": "completed"},  # missing description
        "not-a-dict",  # skipped
    ]
    nodes = context_data._normalize_step_outcomes(raw)
    assert len(nodes) == 2
    assert nodes[0].id == "?"
    assert nodes[0].description == "no id"
    assert nodes[1].id == "s2"
    assert nodes[1].description == ""


# ── Rail flow state tests ────────────────────────────────────────────────────


def test_extract_rail_flow_from_state_populates_fields() -> None:
    """Rail flow fields are extracted from StrangeLoop channel values."""
    values = {
        "rail_id": "autoresearch_loop",
        "current_phase": "verify",
        "last_fired_rules": ["rule_a", "rule_b", "rule_c"],
        "wave_index": 2,
        "feedback_round": 1,
        "acceptance_met": True,
    }
    rf = context_data._extract_rail_flow_from_state(values)
    assert rf.rail_id == "autoresearch_loop"
    assert rf.current_phase == "verify"
    assert rf.last_fired_rules == ("rule_a", "rule_b", "rule_c")
    assert rf.wave_index == 2
    assert rf.feedback_round == 1
    assert rf.acceptance_met is True


def test_extract_rail_flow_from_state_handles_missing_fields() -> None:
    """Missing rail fields produce None / empty defaults."""
    rf = context_data._extract_rail_flow_from_state({})
    assert rf.rail_id is None
    assert rf.current_phase is None
    assert rf.last_fired_rules == ()
    assert rf.wave_index is None
    assert rf.feedback_round is None
    assert rf.acceptance_met is False


def test_extract_rail_flow_truncates_fired_rules_to_last_six() -> None:
    """Only the last 6 fired rules are kept for compact display."""
    values = {"last_fired_rules": [f"rule_{i}" for i in range(10)]}
    rf = context_data._extract_rail_flow_from_state(values)
    assert len(rf.last_fired_rules) == 6
    assert rf.last_fired_rules[-1] == "rule_9"


# ── Autopilot context loading tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_autopilot_context_returns_none_for_regular_loop() -> None:
    """Non-autopilot loops return None from load_autopilot_context."""
    goals = [{"id": "g1", "description": "chat", "status": "active", "depends_on": []}]
    session = SimpleNamespace(
        fetch_loop_history=AsyncMock(return_value=SimpleNamespace(goals=[])),
        fetch_execution_state=AsyncMock(
            return_value=SimpleNamespace(
                plan=None, step_index=0, iteration=0, status="running", active_runner=False
            )
        ),
        aget_loop_state=AsyncMock(return_value=SimpleNamespace(values={})),
    )
    ctx = await context_data.load_autopilot_context("loop-1", session, goals=goals)
    assert ctx is None


@pytest.mark.asyncio
async def test_load_autopilot_context_builds_full_context() -> None:
    """Autopilot context includes step DAGs, rail flow, and progress text."""
    goals = [
        {
            "id": "g1",
            "description": "Refactor",
            "status": "active",
            "depends_on": [],
            "step_outcomes": [
                {"id": "s1", "description": "analyze", "status": "completed"},
                {
                    "id": "s2",
                    "description": "implement",
                    "status": "pending",
                    "dependencies": ["s1"],
                },
            ],
            "plan_summary": "1. analyze 2. implement",
        }
    ]
    session = SimpleNamespace(
        fetch_execution_state=AsyncMock(
            return_value=SimpleNamespace(
                plan=None, step_index=1, iteration=2, status="running", active_runner=True
            )
        ),
        aget_loop_state=AsyncMock(
            return_value=SimpleNamespace(
                values={
                    "rail_id": "autoresearch_loop",
                    "current_phase": "implement",
                    "wave_index": 1,
                }
            )
        ),
    )
    ctx = await context_data.load_autopilot_context("loop-1", session, goals=goals)
    assert ctx is not None
    assert len(ctx.step_dags) == 1
    assert ctx.step_dags[0].steps_total == 2
    assert ctx.step_dags[0].steps_completed == 1
    assert ctx.rail_flow.rail_id == "autoresearch_loop"
    assert ctx.rail_flow.current_phase == "implement"
    assert ctx.active_runner is True
    assert "running" in ctx.progress_text
    assert "Steps: 1/2" in ctx.progress_text
    assert "Iteration: 2" in ctx.progress_text


@pytest.mark.asyncio
async def test_load_autopilot_context_handles_missing_rpc_methods() -> None:
    """Context loading degrades gracefully when daemon lacks RPC methods."""
    goals = [
        {
            "id": "g1",
            "description": "Refactor",
            "status": "active",
            "depends_on": [],
            "step_outcomes": [{"id": "s1", "status": "completed"}],
        }
    ]
    session = SimpleNamespace()  # no fetch_execution_state / aget_loop_state
    ctx = await context_data.load_autopilot_context("loop-1", session, goals=goals)
    assert ctx is not None
    assert ctx.active_runner is None
    assert ctx.rail_flow.rail_id is None
    assert ctx.rail_flow.current_phase is None


@pytest.mark.asyncio
async def test_load_autopilot_context_handles_rpc_failures() -> None:
    """RPC failures do not prevent autopilot context from loading."""
    goals = [
        {
            "id": "g1",
            "description": "Refactor",
            "status": "active",
            "depends_on": [],
            "plan_summary": "1. analyze",
        }
    ]

    async def _raise_exec(_loop_id: str) -> None:
        raise RuntimeError("rpc down")

    async def _raise_state(_loop_id: str) -> None:
        raise RuntimeError("rpc down")

    session = SimpleNamespace(
        fetch_execution_state=_raise_exec,
        aget_loop_state=_raise_state,
    )
    ctx = await context_data.load_autopilot_context("loop-1", session, goals=goals)
    assert ctx is not None
    assert ctx.active_runner is None
    assert ctx.rail_flow.rail_id is None


# ── Step DAG tree panel rendering tests ──────────────────────────────────────


def test_step_dag_tree_panel_renders_empty_dags() -> None:
    """Empty step DAG list shows a placeholder."""
    panel = context_viewer.StepDagTreePanel(step_dags=[])
    rendered = panel.render()
    assert "No step DAG" in rendered


def test_step_dag_tree_panel_renders_zero_step_goal() -> None:
    """Pre-decomposition goals show a 'pending decomposition' message."""
    dag = context_data.StepDagSnapshot(
        goal_id="g1",
        goal_description="New goal",
        goal_status="pending",
        steps=[],
        steps_total=0,
        steps_completed=0,
    )
    panel = context_viewer.StepDagTreePanel(step_dags=[dag])
    rendered = panel.render()
    assert "g1" in rendered
    assert "New goal" in rendered
    assert "pending decomposition" in rendered


def test_step_dag_tree_panel_renders_steps_with_statuses() -> None:
    """Steps render with status icons and descriptions."""
    dag = context_data.StepDagSnapshot(
        goal_id="g1",
        goal_description="Refactor",
        goal_status="active",
        steps=[
            context_data.StepDagNode(id="s1", description="analyze", status="completed"),
            context_data.StepDagNode(
                id="s2", description="implement", status="pending", dependencies=("s1",)
            ),
        ],
        steps_total=2,
        steps_completed=1,
    )
    panel = context_viewer.StepDagTreePanel(step_dags=[dag], steps_mode="all")
    rendered = panel.render()
    assert "s1" in rendered
    assert "analyze" in rendered
    assert "s2" in rendered
    assert "implement" in rendered
    assert "depends: s1" in rendered
    assert "Steps: 1/2" in rendered


def test_step_dag_tree_panel_collapses_large_dags() -> None:
    """DAGs exceeding the step limit collapse with a '+N more' line."""
    steps = [
        context_data.StepDagNode(id=f"s{i}", description=f"step {i}", status="completed")
        for i in range(context_viewer._MAX_STEPS_PER_GOAL + 5)
    ]
    dag = context_data.StepDagSnapshot(
        goal_id="g1",
        goal_description="Big",
        goal_status="active",
        steps=steps,
        steps_total=len(steps),
        steps_completed=len(steps),
    )
    panel = context_viewer.StepDagTreePanel(step_dags=[dag], steps_mode="all")
    rendered = panel.render()
    assert "+5 more" in rendered


# ── Rail flow panel rendering tests ──────────────────────────────────────────


def test_rail_flow_panel_renders_empty_state() -> None:
    """No rail flow shows an 'unbound' placeholder."""
    panel = context_viewer.RailFlowPanel(rail_flow=None)
    rendered = panel.render()
    assert "unbound" in rendered


def test_rail_flow_panel_renders_rail_id_and_phase() -> None:
    """Rail flow panel shows rail id, phase, and metadata."""
    rf = context_data.RailFlowState(
        rail_id="autoresearch_loop",
        current_phase="verify",
        wave_index=2,
        feedback_round=1,
        acceptance_met=True,
    )
    panel = context_viewer.RailFlowPanel(rail_flow=rf)
    rendered = panel.render()
    assert "autoresearch_loop" in rendered
    assert "verify" in rendered
    assert "Wave: 2" in rendered
    assert "Feedback: 1" in rendered
    assert "Acceptance" in rendered


def test_rail_flow_panel_renders_fired_rules() -> None:
    """Last fired rules are listed in the rail flow panel."""
    rf = context_data.RailFlowState(
        rail_id="autoresearch_loop",
        current_phase="implement",
        last_fired_rules=("rule_a", "rule_b"),
    )
    panel = context_viewer.RailFlowPanel(rail_flow=rf)
    rendered = panel.render()
    assert "rule_a" in rendered
    assert "rule_b" in rendered


# ── Autopilot progress panel rendering tests ─────────────────────────────────


def test_autopilot_progress_panel_renders_loading_state() -> None:
    """No context yet shows a loading placeholder."""
    panel = context_viewer.AutopilotProgressPanel(context=None)
    rendered = panel.render()
    assert "Loading" in rendered


def test_autopilot_progress_panel_renders_progress_text() -> None:
    """Progress text and active runner are shown."""
    ctx = context_data.AutopilotContext(
        step_dags=[],
        rail_flow=context_data.RailFlowState(rail_id="test_rail"),
        progress_text="running · Steps: 3/5",
        active_runner=True,
    )
    panel = context_viewer.AutopilotProgressPanel(context=ctx)
    rendered = panel.render()
    assert "running · Steps: 3/5" in rendered
    assert "test_rail" in rendered


@pytest.mark.asyncio
async def test_load_token_usage_snapshot_includes_conversation_breakdown(monkeypatch) -> None:
    monkeypatch.setattr(
        context_data,
        "fetch_conversation_token_count",
        AsyncMock(return_value=1200),
    )

    snapshot = await context_data.load_token_usage_snapshot(
        context_tokens=5000,
        loop_id="loop-123",
        daemon_session=object(),
        model_name="test-model",
        context_limit=128000,
    )

    assert snapshot.conv_tokens == 1200
    assert "Conversation (est.)" in context_data.format_token_usage(snapshot)


# ── Interactive view state (ported from autopilot `top` keymaps) ─────────────


def test_context_view_state_defaults() -> None:
    """Defaults mirror the autopilot `top` view state."""
    state = context_data.ContextViewState()
    assert state.include_terminal is False
    assert state.steps_mode == "active"
    assert state.show_loops is True
    assert state.interval == context_data.CONTEXT_REFRESH_DEFAULT_S
    assert state.force_refresh is False


def test_apply_context_key_toggles() -> None:
    """Toggle keys behave like autopilot `top` (a/s/l/d/+/-/space)."""
    state = context_data.ContextViewState()
    assert state.interval == context_data.CONTEXT_REFRESH_DEFAULT_S

    context_data.apply_context_key(state, "a")
    assert state.include_terminal is True
    assert state.force_refresh is True

    context_data.apply_context_key(state, "s")
    assert state.steps_mode == "all"
    context_data.apply_context_key(state, "s")
    assert state.steps_mode == "off"
    context_data.apply_context_key(state, "s")
    assert state.steps_mode == "active"

    context_data.apply_context_key(state, "l")
    assert state.show_loops is False

    # density: compact (off + no loops) -> steps-only (all, no loops) -> full
    state.steps_mode = "off"
    context_data.apply_context_key(state, "d")
    assert state.steps_mode == "all" and state.show_loops is False
    context_data.apply_context_key(state, "d")
    assert state.steps_mode == "all" and state.show_loops is True
    context_data.apply_context_key(state, "d")
    assert state.steps_mode == "off" and state.show_loops is False

    context_data.apply_context_key(state, "+")
    assert state.interval < context_data.CONTEXT_REFRESH_DEFAULT_S
    context_data.apply_context_key(state, "-")
    assert state.interval == context_data.CONTEXT_REFRESH_DEFAULT_S

    context_data.apply_context_key(state, "space")
    assert state.force_refresh is True


def test_apply_context_key_interval_clamps() -> None:
    """Interval never leaves the 0.2–10s range."""
    state = context_data.ContextViewState()
    for _ in range(100):
        context_data.apply_context_key(state, "+")
    assert state.interval == context_data.CONTEXT_REFRESH_MIN_S
    for _ in range(100):
        context_data.apply_context_key(state, "-")
    assert state.interval == context_data.CONTEXT_REFRESH_MAX_S


def test_filter_goals_for_view_hides_terminal() -> None:
    """Active-only mode drops completed/failed/cancelled goals."""
    goals = [
        {"id": "g1", "status": "active"},
        {"id": "g2", "status": "completed"},
        {"id": "g3", "status": "failed"},
        {"id": "g4", "status": "cancelled"},
        {"id": "g5", "status": "pending"},
    ]
    active = context_data.filter_goals_for_view(goals, include_terminal=False)
    assert [g["id"] for g in active] == ["g1", "g5"]
    all_goals = context_data.filter_goals_for_view(goals, include_terminal=True)
    assert [g["id"] for g in all_goals] == ["g1", "g2", "g3", "g4", "g5"]


def test_filter_step_dags_for_view_hides_terminal_goals() -> None:
    """Active-only mode drops step DAGs whose goal is terminal."""
    dags = [
        context_data.StepDagSnapshot(goal_id="g1", goal_description="live", goal_status="active"),
        context_data.StepDagSnapshot(
            goal_id="g2", goal_description="done", goal_status="completed"
        ),
    ]
    active = context_data.filter_step_dags_for_view(dags, include_terminal=False)
    assert [d.goal_id for d in active] == ["g1"]
    assert len(context_data.filter_step_dags_for_view(dags, include_terminal=True)) == 2


def _goal_dag_with_steps() -> context_data.StepDagSnapshot:
    """Build a step DAG with terminal + non-terminal steps for mode tests."""
    return context_data.StepDagSnapshot(
        goal_id="g1",
        goal_description="Refactor",
        goal_status="active",
        steps=[
            context_data.StepDagNode(id="s1", description="analyze", status="completed"),
            context_data.StepDagNode(id="s2", description="implement", status="active"),
            context_data.StepDagNode(id="s3", description="verify", status="pending"),
            context_data.StepDagNode(id="s4", description="docs", status="failed"),
        ],
        steps_total=4,
        steps_completed=1,
    )


def test_step_dag_tree_panel_steps_mode_active_shows_only_active_and_pending() -> None:
    """`steps_mode=active` lists only active/pending steps (mirrors `top`)."""
    panel = context_viewer.StepDagTreePanel(step_dags=[_goal_dag_with_steps()], steps_mode="active")
    rendered = panel.render()
    assert "s2" in rendered and "implement" in rendered
    assert "s3" in rendered and "verify" in rendered
    assert "s1" not in rendered
    assert "s4" not in rendered
    # Real progress counts are still shown (not just visible rows).
    assert "Steps: 1/4" in rendered


def test_step_dag_tree_panel_steps_mode_all_shows_every_step() -> None:
    """`steps_mode=all` lists completed and failed steps too."""
    panel = context_viewer.StepDagTreePanel(step_dags=[_goal_dag_with_steps()], steps_mode="all")
    rendered = panel.render()
    assert "s1" in rendered
    assert "s2" in rendered
    assert "s3" in rendered
    assert "s4" in rendered


def test_step_dag_tree_panel_steps_mode_off_hides_step_rows() -> None:
    """`steps_mode=off` hides step rows but keeps the goal header + counts."""
    panel = context_viewer.StepDagTreePanel(step_dags=[_goal_dag_with_steps()], steps_mode="off")
    rendered = panel.render()
    assert "g1" in rendered
    assert "Refactor" in rendered
    assert "Steps: 1/4" in rendered
    assert "s1" not in rendered
    assert "s2" not in rendered
    assert "steps hidden" in rendered


def test_context_viewer_footer_reflects_state() -> None:
    """The footer renders the live mode/steps/rail/delay badges."""
    import re

    def plain(text: str) -> str:
        """Strip Textual markup tags for substring assertions."""
        return re.sub(r"\[/?[^\]]*\]", "", text)

    screen = context_viewer.ContextViewerScreen("loop-1")
    screen._state.include_terminal = True
    screen._state.steps_mode = "all"
    screen._state.show_loops = False
    screen._state.interval = 2.5
    footer = plain(screen._render_footer())
    assert "mode=all" in footer
    assert "(live)" not in footer
    assert "steps=all" in footer
    assert "rail=off" in footer
    assert "delay=2.5s" in footer
    # default (active-only) carries the live hint
    fresh = plain(context_viewer.ContextViewerScreen("loop-2")._render_footer())
    assert "mode=active" in fresh
    assert "(live)" in fresh


def test_context_viewer_help_overlay_content_lists_keys() -> None:
    """The help overlay documents every ported keymap."""
    screen = context_viewer.ContextViewerScreen("loop-1")
    help_text = screen._render_help()
    for key in ("a", "s", "l", "d", "+/-", "Space", "g/G", "h/?", "q"):
        assert key in help_text


# ── Global top dashboard (all loops/goals/steps) ────────────────────────────


def _plain(text: str) -> str:
    """Strip Textual markup tags for substring assertions."""
    import re

    return re.sub(r"\[/?[^\]]*\]", "", text)


def _global_snapshot_two_loops() -> context_data.GlobalContextSnapshot:
    """Build a two-loop global snapshot for panel tests."""
    return context_data.GlobalContextSnapshot(
        loops=[
            context_data.LoopContextSnapshot(
                loop_id="loop-A",
                status="running",
                live=True,
                prompt="Implement auth",
                updated="2026-09-12T02:00:00+00:00",
                goals=[
                    {"id": "g1", "goal_id": "g1", "description": "auth", "status": "active"},
                    {"id": "g2", "goal_id": "g2", "description": "docs", "status": "completed"},
                ],
                step_dags=[
                    context_data.StepDagSnapshot(
                        goal_id="g1",
                        goal_description="auth",
                        goal_status="active",
                        steps=[
                            context_data.StepDagNode(
                                id="s1", description="scaffold", status="completed"
                            ),
                            context_data.StepDagNode(id="s2", description="jwt", status="pending"),
                        ],
                        steps_total=2,
                        steps_completed=1,
                    ),
                ],
                goals_total=2,
            ),
            context_data.LoopContextSnapshot(
                loop_id="loop-B",
                status="paused",
                live=False,
                prompt="Write tests",
                updated="2026-09-12T01:00:00+00:00",
                goals=[
                    {"id": "g3", "goal_id": "g3", "description": "tests", "status": "pending"},
                ],
                step_dags=[],
                goals_total=1,
            ),
        ],
        loops_total=2,
        loops_active=1,
        goals_total=3,
        goals_by_status={"active": 1, "completed": 1, "pending": 1},
        steps_total=2,
        steps_completed=1,
    )


@pytest.mark.asyncio
async def test_load_global_context_aggregates_loops() -> None:
    """The global aggregator enumerates loops and tallies goals/steps."""
    loops_resp = {
        "loops": [
            {
                "loop_id": "loop-A",
                "status": "running",
                "live": True,
                "prompt": "Implement auth",
                "updated": "2026-09-12T02:00:00+00:00",
                "goals": 1,
            }
        ]
    }
    history = SimpleNamespace(
        goals=[
            {
                "goal_id": "g1",
                "goal_text": "auth",
                "status": "active",
                "step_outcomes": [
                    {"id": "s1", "description": "scaffold", "status": "completed"},
                    {"id": "s2", "description": "jwt", "status": "pending"},
                ],
            }
        ]
    )
    session = SimpleNamespace(
        list_loops=AsyncMock(return_value=loops_resp),
        fetch_loop_history=AsyncMock(return_value=history),
    )

    snapshot = await context_data.load_global_context(session)
    assert snapshot.loops_total == 1
    assert snapshot.loops_active == 1
    assert snapshot.goals_total == 1
    assert snapshot.goals_by_status.get("active") == 1
    assert snapshot.steps_total == 2
    assert snapshot.steps_completed == 1
    assert snapshot.loops[0].prompt == "Implement auth"


@pytest.mark.asyncio
async def test_load_global_context_returns_empty_without_session() -> None:
    """A missing daemon session yields an empty (non-error) snapshot."""
    snapshot = await context_data.load_global_context(None)
    assert snapshot.loops_total == 0
    assert snapshot.goals_total == 0


def test_global_top_panel_renders_loop_goal_step_forest() -> None:
    """The global forest shows loop → goal → step rows plus header counts."""
    panel = context_viewer.GlobalTopPanel(
        snapshot=_global_snapshot_two_loops(),
        state=context_data.ContextViewState(include_terminal=True, steps_mode="all"),
    )
    rendered = _plain(panel.render())
    assert "Context · global" in rendered
    assert "loops=2" in rendered and "live 1" in rendered
    assert "goals=3" in rendered
    assert "steps=1/2" in rendered
    assert "LOOP" in rendered
    assert "g1" in rendered and "auth" in rendered
    assert "s1" in rendered and "scaffold" in rendered
    assert "s2" in rendered and "jwt" in rendered


def test_global_top_panel_active_mode_drops_terminal_goals_and_loops() -> None:
    """Active-only mode omits terminal goals and fully-terminal loops."""
    state = context_data.ContextViewState(include_terminal=False, steps_mode="all")
    panel = context_viewer.GlobalTopPanel(snapshot=_global_snapshot_two_loops(), state=state)
    rendered = _plain(panel.render())
    # g2 (completed) is dropped under loop-A.
    assert "docs" not in rendered
    # loop-A still shows (it has g1 active + is live).
    assert "Implement auth" in rendered


def test_global_top_panel_steps_mode_off_hides_step_rows() -> None:
    """steps_mode=off hides step rows but keeps the loop/goal headers."""
    state = context_data.ContextViewState(include_terminal=True, steps_mode="off")
    panel = context_viewer.GlobalTopPanel(snapshot=_global_snapshot_two_loops(), state=state)
    rendered = _plain(panel.render())
    assert "scaffold" not in rendered
    assert "jwt" not in rendered
    assert "g1" in rendered and "LOOP" in rendered


def test_global_top_panel_steps_mode_active_shows_only_active_and_pending() -> None:
    """steps_mode=active lists only active/pending steps (completed hidden)."""
    state = context_data.ContextViewState(include_terminal=True, steps_mode="active")
    panel = context_viewer.GlobalTopPanel(snapshot=_global_snapshot_two_loops(), state=state)
    rendered = _plain(panel.render())
    assert "jwt" in rendered  # pending step
    assert "scaffold" not in rendered  # completed step hidden


@pytest.mark.asyncio
async def test_context_viewer_t_toggle_switches_to_global() -> None:
    """Pressing `t` switches the /context body to the global top dashboard."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class HostApp(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    loops_resp = {
        "loops": [
            {
                "loop_id": "loop-A",
                "status": "running",
                "live": True,
                "prompt": "Implement auth",
                "updated": "2026-09-12T02:00:00+00:00",
                "goals": 1,
            }
        ]
    }
    history = SimpleNamespace(goals=[{"goal_id": "g1", "goal_text": "auth", "status": "active"}])
    session = SimpleNamespace(
        list_loops=AsyncMock(return_value=loops_resp),
        fetch_loop_history=AsyncMock(return_value=history),
    )
    app = HostApp()
    async with app.run_test() as pilot:
        screen = context_viewer.ContextViewerScreen(
            "loop-A",
            daemon_session=session,
            load_token_snapshot=AsyncMock(return_value=None),
        )
        app.push_screen(screen)
        await pilot.pause()
        assert screen._view_mode == "loop"
        await pilot.press("t")
        await pilot.pause()
        assert screen._view_mode == "global"
        assert screen._global_snapshot is not None
        assert screen._global_snapshot.loops_total == 1
        # back to loop view
        await pilot.press("t")
        await pilot.pause()
        assert screen._view_mode == "loop"


@pytest.mark.asyncio
async def test_context_viewer_h_pushes_help_view_dismissed_by_any_key() -> None:
    """`h` opens a dedicated help modal; any key dismisses it."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from soothe_cli.tui.widgets.context_viewer import ContextHelpScreen

    class HostApp(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    session = SimpleNamespace(
        fetch_loop_history=AsyncMock(return_value=SimpleNamespace(goals=[])),
        list_loops=AsyncMock(return_value={"loops": []}),
    )
    app = HostApp()
    async with app.run_test() as pilot:
        screen = context_viewer.ContextViewerScreen(
            "loop-A",
            daemon_session=session,
            load_token_snapshot=AsyncMock(return_value=None),
        )
        app.push_screen(screen)
        await pilot.pause()
        # `h` pushes the help view on top of the context screen.
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, ContextHelpScreen)
        # any key dismisses the help view and returns to /context.
        await pilot.press("a")
        await pilot.pause()
        assert app.screen is screen
