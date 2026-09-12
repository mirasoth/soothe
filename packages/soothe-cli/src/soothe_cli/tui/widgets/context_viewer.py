"""Context Engine viewer screen for /context command."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from textual import events
from textual.binding import Binding, BindingType
from textual.containers import ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

from soothe_cli.runtime.presentation.id_format import abbreviate_compact_id
from soothe_cli.settings import is_ascii_mode
from soothe_cli.tui.widgets.context_data import (
    AutopilotContext,
    ContextViewState,
    GlobalContextSnapshot,
    LoadTokenSnapshotFn,
    RailFlowState,
    StepDagNode,
    StepDagSnapshot,
    TokenUsageSnapshot,
    apply_context_key,
    filter_goals_for_view,
    filter_step_dags_for_view,
    format_token_usage,
    load_autopilot_context,
    load_ce_goals,
    load_global_context,
    summarize_goal_statuses,
)

logger = logging.getLogger(__name__)
_COMPACT_MIN_WIDTH = 116
_COMPACT_MIN_HEIGHT = 32

# Step statuses visible in steps_mode="active" (mirrors autopilot `top`).
_VIEW_ACTIVE_STEP_STATUSES: frozenset[str] = frozenset({"active", "pending"})

# Status color mapping for goal/context display
STATUS_COLORS: dict[str, str] = {
    "pending": "dim",
    "active": "yellow",
    "validated": "blue",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim red",
    "suspended": "magenta",
    "blocked": "orange",
    "awaiting_clarification": "magenta",
}

STATUS_ICONS: dict[str, str] = {
    "pending": "○",
    "active": "◉",
    "completed": "✓",
    "failed": "✗",
    "cancelled": "⊘",
    "suspended": "⏸",
    "blocked": "●",
    "awaiting_clarification": "?",
    "validated": "◆",
}

# Step-specific status icons (superset of goal statuses with step-only states)
STEP_STATUS_ICONS: dict[str, str] = {
    **STATUS_ICONS,
    "skipped": "↷",
    "decomposed": "⤳",
    "superseded": "⇄",
}

# Max steps to render per goal before collapsing with a "+N more" line.
_MAX_STEPS_PER_GOAL = 40


def _abbreviate_loop_id(loop_id: str) -> str:
    """Render loop id in `prefix...suffix` form for compact status lines."""
    return abbreviate_compact_id(loop_id, empty="unknown")


def _truncate_text(value: str, *, max_len: int) -> str:
    """Truncate `value` to `max_len` characters with ellipsis."""
    normalized = value.strip()
    if max_len <= 3:
        return normalized[:max_len]
    if len(normalized) <= max_len:
        return normalized
    return f"{normalized[: max_len - 3]}..."


def _abbreviate_goal_id(goal_id: str) -> str:
    """Render verbose goal ids in compact `prefix...suffix` form."""
    raw = str(goal_id or "").strip()
    if len(raw) <= 14:
        return raw
    return f"{raw[:8]}...{raw[-4:]}"


def _join_with_wrap(segments: list[str], *, width: int, prefix: str = "  ") -> list[str]:
    """Join segments into wrapped status lines using ` | ` separators."""
    if not segments:
        return [prefix.rstrip()]
    max_width = max(28, width)
    lines: list[str] = []
    current = prefix
    for segment in segments:
        candidate = segment if current.strip() == "" else f" | {segment}"
        if len(current) + len(candidate) > max_width and current.strip():
            lines.append(current)
            current = f"{prefix}{segment}"
            continue
        current += candidate
    if current.strip():
        lines.append(current)
    return lines


class TokenUsagePanel(Static):
    """Displays context window token usage."""

    DEFAULT_CSS = """
    TokenUsagePanel {
        width: 1fr;
        height: auto;
        min-height: 3;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, snapshot: TokenUsageSnapshot | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._snapshot = snapshot

    def set_snapshot(self, snapshot: TokenUsageSnapshot | None) -> None:
        """Replace the token snapshot and refresh."""
        self._snapshot = snapshot
        self.update(self.render())

    def render(self) -> str:
        """Render token usage summary."""
        if self._snapshot is None:
            return "[bold cyan]Token Usage[/]\n  [dim]Loading…[/]"
        body = format_token_usage(self._snapshot)
        return f"[bold cyan]Token Usage[/]\n  {body}"


class GoalDagPanel(Static):
    """Displays goal DAG as a text tree."""

    DEFAULT_CSS = """
    GoalDagPanel {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, goals: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._goals = goals

    def set_goals(self, goals: list[dict[str, Any]]) -> None:
        """Replace current goals and refresh panel content."""
        self._goals = goals
        self.update(self.render())

    def render(self) -> str:
        """Render goal DAG as styled text."""
        if not self._goals:
            return "[bold green]Goal DAG[/]\n\n  [dim]No goals in context engine[/]"

        lines = ["[bold green]Goal DAG[/]", ""]
        panel_width = self.size.width if self.size.width > 0 else 80
        desc_max_len = max(40, panel_width - 24)
        for goal in self._goals:
            status = str(goal.get("status") or "pending")
            color = STATUS_COLORS.get(status, "dim")
            icon = STATUS_ICONS.get(status, "○")
            gid = _abbreviate_goal_id(str(goal.get("id", "?")))
            desc = str(goal.get("description") or "")
            desc = _truncate_text(desc, max_len=desc_max_len)
            base_line = f"  [{color}]{icon}[/] [{color}]{gid}[/]"
            if desc:
                base_line = f"{base_line} {desc}"
            lines.append(base_line)

            depends_on = goal.get("depends_on")
            if depends_on:
                dep_list = [str(dep) for dep in depends_on[:4]]
                dep_suffix = ""
                remaining = len(depends_on) - len(dep_list)
                if remaining > 0:
                    dep_suffix = f" (+{remaining} more)"
                deps_text = _truncate_text(
                    ", ".join(dep_list) + dep_suffix,
                    max_len=max(24, panel_width - 18),
                )
                lines.append(f"    [dim]depends on: {deps_text}[/]")

        return "\n".join(lines)


class StatusPanel(Static):
    """Displays context engine status summary."""

    DEFAULT_CSS = """
    StatusPanel {
        width: 1fr;
        height: auto;
        min-height: 3;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, goals: list[dict[str, Any]], loop_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._goals = goals
        self._loop_id = loop_id

    def set_goals(self, goals: list[dict[str, Any]]) -> None:
        """Replace current goals and refresh panel content."""
        self._goals = goals
        self.update(self.render())

    def render(self) -> str:
        """Render status summary."""
        total, counts = summarize_goal_statuses(self._goals)
        panel_width = self.size.width if self.size.width > 0 else 80
        if total == 0:
            lines = [
                f"[bold blue]Context Status[/]  [dim]Loop: {_abbreviate_loop_id(self._loop_id)}[/]",
                "  Total: 0",
            ]
            return "\n".join(lines)
        else:
            primary_parts = [f"Total: {total}"]
            for status in ("active", "completed", "pending"):
                value = counts.get(status, 0)
                if value:
                    primary_parts.append(f"{status.title()}: {value}")

            secondary_order = (
                "failed",
                "validated",
                "suspended",
                "blocked",
                "awaiting_clarification",
                "cancelled",
            )
            secondary_parts: list[str] = []
            for status in secondary_order:
                value = counts.get(status, 0)
                if value:
                    secondary_parts.append(f"{status.title()}: {value}")
            for status, value in sorted(counts.items()):
                if status in {"active", "completed", "pending", *secondary_order}:
                    continue
                if value:
                    secondary_parts.append(f"{status.title()}: {value}")
            status_lines = _join_with_wrap(primary_parts, width=panel_width - 2)
            if secondary_parts:
                status_lines.extend(
                    _join_with_wrap(
                        [f"Other: {secondary_parts[0]}", *secondary_parts[1:]],
                        width=panel_width - 2,
                    )
                )

        lines = [
            f"[bold blue]Context Status[/]  [dim]Loop: {_abbreviate_loop_id(self._loop_id)}[/]",
            *status_lines,
        ]
        return "\n".join(lines)


class AutopilotProgressPanel(Static):
    """Progress summary for autopilot-mode loops.

    Shows step completion counts, rail phase, iteration, and loop status
    in a compact one-or-two-line summary.
    """

    DEFAULT_CSS = """
    AutopilotProgressPanel {
        width: 1fr;
        height: auto;
        min-height: 3;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, context: AutopilotContext | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._context = context

    def set_context(self, context: AutopilotContext | None) -> None:
        """Replace the autopilot context and refresh."""
        self._context = context
        self.update(self.render())

    def render(self) -> str:
        """Render autopilot progress summary."""
        if self._context is None:
            return "[bold magenta]Autopilot Progress[/]\n  [dim]Loading…[/]"
        ctx = self._context
        header_parts = ["[bold magenta]Autopilot Progress[/]"]
        if ctx.rail_flow.rail_id:
            header_parts.append(f"[dim]{ctx.rail_flow.rail_id}[/]")
        lines = ["  ".join(header_parts)]
        lines.append(f"  {ctx.progress_text}")
        if ctx.active_runner is not None:
            runner_tag = "live" if ctx.active_runner else "idle"
            lines.append(f"  [dim]Runner: {runner_tag}[/]")
        return "\n".join(lines)


class StepDagTreePanel(Static):
    """Renders step DAG trees for autopilot-mode goals.

    Each goal with steps is rendered as a tree showing step descriptions,
    statuses, dependencies, and execution records. Handles zero-step
    (pre-decomposition) and large DAGs gracefully. ``steps_mode`` mirrors
    the autopilot ``top`` keymap: ``off`` hides step rows, ``active`` lists
    only active/pending steps, ``all`` lists every step.
    """

    DEFAULT_CSS = """
    StepDagTreePanel {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        step_dags: list[StepDagSnapshot] | None = None,
        *,
        steps_mode: Literal["off", "active", "all"] = "active",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._step_dags = step_dags or []
        self._steps_mode = steps_mode

    def set_step_dags(
        self,
        step_dags: list[StepDagSnapshot],
        *,
        steps_mode: Literal["off", "active", "all"] | None = None,
    ) -> None:
        """Replace step DAG snapshots and refresh.

        Args:
        step_dags: New step DAG snapshots to render.
        steps_mode: When provided, update the active/off/all step filter.
        """
        self._step_dags = step_dags
        if steps_mode is not None:
            self._steps_mode = steps_mode
        self.update(self.render())

    def _visible_steps(self, dag: StepDagSnapshot) -> list[StepDagNode]:
        """Return the step rows to render for ``dag`` under the current mode.

        Args:
        dag: Step DAG snapshot for one goal.

        Returns:
        Step nodes to render (capped at ``_MAX_STEPS_PER_GOAL``). Empty when
        ``steps_mode == "off"`` or no steps match the active filter.
        """
        if self._steps_mode == "off":
            return []
        steps = dag.steps
        if self._steps_mode == "active":
            steps = [s for s in dag.steps if str(s.status).lower() in _VIEW_ACTIVE_STEP_STATUSES]
        return steps[:_MAX_STEPS_PER_GOAL]

    def render(self) -> str:
        """Render step DAG trees as styled text."""
        if not self._step_dags:
            return "[bold green]Step DAG[/]\n\n  [dim]No step DAG available[/]"

        lines = ["[bold green]Step DAG[/]", ""]
        panel_width = self.size.width if self.size.width > 0 else 80
        desc_max = max(30, panel_width - 28)

        for dag in self._step_dags:
            goal_color = STATUS_COLORS.get(dag.goal_status, "dim")
            goal_icon = STATUS_ICONS.get(dag.goal_status, "○")
            gid = _abbreviate_goal_id(dag.goal_id)
            gdesc = _truncate_text(dag.goal_description, max_len=desc_max)
            count_str = f"Steps: {dag.steps_completed}/{dag.steps_total}"
            lines.append(
                f"  [{goal_color}]{goal_icon}[/] [{goal_color}]{gid}[/] {gdesc} [dim]{count_str}[/]"
            )

            if not dag.steps:
                if dag.plan_summary:
                    summary = _truncate_text(str(dag.plan_summary), max_len=desc_max)
                    lines.append(f"    [dim]plan: {summary}[/]")
                else:
                    lines.append("    [dim]pending decomposition[/]")
                lines.append("")
                continue

            visible = self._visible_steps(dag)
            for step in visible:
                self._render_step(lines, step, panel_width)
            remaining = len(dag.steps) - len(visible)
            if self._steps_mode == "active" and remaining > 0:
                lines.append(f"    [dim]… +{remaining} hidden steps[/]")
            elif self._steps_mode == "all" and remaining > 0:
                lines.append(f"    [dim]… +{remaining} more steps[/]")
            elif self._steps_mode == "off":
                lines.append("    [dim]steps hidden (s)[/]")

            if dag.plan_summary:
                summary = _truncate_text(str(dag.plan_summary), max_len=desc_max)
                lines.append(f"    [dim]plan: {summary}[/]")
            lines.append("")

        return "\n".join(lines)

    def _render_step(self, lines: list[str], step: StepDagNode, panel_width: int) -> None:
        """Append one step node to the lines list.

        Args:
        lines: Accumulating output lines list.
        step: Step node to render.
        panel_width: Available panel width for truncation.
        """
        color = STATUS_COLORS.get(step.status, "dim")
        icon = STEP_STATUS_ICONS.get(step.status, "○")
        sid = _abbreviate_goal_id(step.id)
        desc_max = max(24, panel_width - 30)
        desc = _truncate_text(step.description, max_len=desc_max)
        kind_tag = f" [dim italic]{step.kind}[/]" if step.kind else ""
        lines.append(f"    [{color}]{icon}[/] [{color}]{sid}[/] {desc}{kind_tag}")
        if step.dependencies:
            deps_text = _truncate_text(
                ", ".join(step.dependencies[:4]),
                max_len=max(20, panel_width - 22),
            )
            suffix = f" (+{len(step.dependencies) - 4} more)" if len(step.dependencies) > 4 else ""
            lines.append(f"      [dim]depends: {deps_text}{suffix}[/]")
        if step.execution_summary:
            exec_text = _truncate_text(step.execution_summary, max_len=max(20, panel_width - 22))
            lines.append(f"      [dim]exec: {exec_text}[/]")


class RailFlowPanel(Static):
    """Displays rail flow state for autopilot-mode loops.

    Shows the bound rail id, current phase, last fired rules, and
    fan-out wave / feedback round metadata.
    """

    DEFAULT_CSS = """
    RailFlowPanel {
        width: 1fr;
        height: auto;
        min-height: 3;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, rail_flow: RailFlowState | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._rail_flow = rail_flow

    def set_rail_flow(self, rail_flow: RailFlowState | None) -> None:
        """Replace rail flow state and refresh."""
        self._rail_flow = rail_flow
        self.update(self.render())

    def render(self) -> str:
        """Render rail flow state."""
        if self._rail_flow is None:
            return "[bold cyan]Rail Flow[/]\n  [dim]Rail unbound[/]"
        rf = self._rail_flow
        if rf.rail_id is None:
            return "[bold cyan]Rail Flow[/]\n  [dim]Rail unbound[/]"

        lines = [f"[bold cyan]Rail Flow[/]  [dim]{rf.rail_id}[/]"]
        if rf.current_phase:
            lines.append(f"  Phase: [yellow]{rf.current_phase}[/]")
        if rf.last_fired_rules:
            rules_text = _truncate_text(", ".join(rf.last_fired_rules[:6]), max_len=60)
            suffix = (
                f" (+{len(rf.last_fired_rules) - 6} more)" if len(rf.last_fired_rules) > 6 else ""
            )
            lines.append(f"  Last rules: [dim]{rules_text}{suffix}[/]")
        meta_parts: list[str] = []
        if rf.wave_index:
            meta_parts.append(f"Wave: {rf.wave_index}")
        if rf.feedback_round:
            meta_parts.append(f"Feedback: {rf.feedback_round}")
        if rf.acceptance_met:
            meta_parts.append("Acceptance: ✓")
        if meta_parts:
            lines.append(f"  [dim]{' · '.join(meta_parts)}[/]")
        return "\n".join(lines)


class GlobalTopPanel(Static):
    """Global ``top``-style forest: Loop → Goal → Step with an htop-style header."""

    DEFAULT_CSS = """
    GlobalTopPanel {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        snapshot: GlobalContextSnapshot | None = None,
        *,
        state: ContextViewState | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._snapshot = snapshot
        self._state = state or ContextViewState()

    def set_snapshot(
        self,
        snapshot: GlobalContextSnapshot | None,
        *,
        state: ContextViewState | None = None,
    ) -> None:
        """Replace the global snapshot and refresh the forest."""
        self._snapshot = snapshot
        if state is not None:
            self._state = state
        self.update(self.render())

    def _visible_steps(self, dag: StepDagSnapshot) -> list[StepDagNode]:
        """Return step rows to render for ``dag`` under the current mode."""
        if self._state.steps_mode == "off":
            return []
        steps = dag.steps
        if self._state.steps_mode == "active":
            steps = [s for s in dag.steps if str(s.status).lower() in _VIEW_ACTIVE_STEP_STATUSES]
        return steps[:_MAX_STEPS_PER_GOAL]

    def render(self) -> str:
        """Render the global header + Loop → Goal → Step forest."""
        snapshot = self._snapshot
        if snapshot is None:
            return "[bold]Context · global[/]\n  [dim]Loading…[/]"
        state = self._state
        if snapshot.loops_total == 0:
            return (
                "[bold]Context · global[/]\n\n  [dim]No loops. Press t to return to this loop.[/]"
            )

        lines = ["[bold]Context · global[/]", ""]
        lines.append(
            f"[dim]loops=[/][yellow]{snapshot.loops_total}[/][dim] (live "
            f"{snapshot.loops_active})  goals=[/][yellow]{snapshot.goals_total}[/]"
            f"[dim]  steps=[/][yellow]{snapshot.steps_completed}/"
            f"{snapshot.steps_total}[/]"
        )
        status_order = (
            "active",
            "pending",
            "completed",
            "failed",
            "validated",
            "suspended",
            "blocked",
            "awaiting_clarification",
            "cancelled",
        )
        bits = [
            f"{k}={snapshot.goals_by_status.get(k, 0)}"
            for k in status_order
            if snapshot.goals_by_status.get(k, 0)
        ]
        if bits:
            lines.append(f"[dim]{' '.join(bits)}[/]")
        lines.append("")

        for loop in snapshot.loops:
            visible_goals = filter_goals_for_view(
                loop.goals, include_terminal=state.include_terminal
            )
            if not visible_goals and not state.include_terminal and not loop.live:
                continue
            lid = _abbreviate_goal_id(loop.loop_id)
            live_tag = " [bright_yellow]live[/]" if loop.live else ""
            prompt = _truncate_text(loop.prompt, max_len=40)
            steps_done = sum(d.steps_completed for d in loop.step_dags)
            steps_all = sum(d.steps_total for d in loop.step_dags)
            counts = f"goals:{loop.goals_total} steps:{steps_done}/{steps_all}"
            lines.append(
                f"  [cyan]LOOP[/] [cyan]{lid}[/] [yellow]{loop.status}[/]{live_tag}"
                f' [dim]{counts}[/] [dim]"{prompt}"[/]'
            )
            dag_by_goal = {d.goal_id: d for d in loop.step_dags}
            for goal in visible_goals:
                gid = str(goal.get("id") or goal.get("goal_id") or "?")
                status = str(goal.get("status") or "pending")
                color = STATUS_COLORS.get(status, "dim")
                icon = STATUS_ICONS.get(status, "○")
                agid = _abbreviate_goal_id(gid)
                desc = _truncate_text(
                    str(goal.get("description") or goal.get("goal_text") or ""),
                    max_len=40,
                )
                dag = dag_by_goal.get(gid)
                count_str = (
                    f" [dim]Steps: {dag.steps_completed}/{dag.steps_total}[/]"
                    if dag and dag.steps_total
                    else ""
                )
                lines.append(f"    [{color}]{icon}[/] [{color}]{agid}[/] {desc}{count_str}")
                if dag is not None:
                    for step in self._visible_steps(dag):
                        sc = STATUS_COLORS.get(step.status, "dim")
                        si = STEP_STATUS_ICONS.get(step.status, "○")
                        sid = _abbreviate_goal_id(step.id)
                        sdesc = _truncate_text(step.description, max_len=36)
                        lines.append(f"      [{sc}]{si}[/] [{sc}]{sid}[/] {sdesc}")
            lines.append("")

        return "\n".join(lines)


class ContextHelpScreen(ModalScreen[None]):
    """Modal showing the ``/context`` keymap reference; any key dismisses it."""

    CSS = """
    ContextHelpScreen {
        align: center middle;
        background: $background 60%;
    }

    ContextHelpScreen > Vertical {
        width: 70;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
    }

    ContextHelpScreen .context-help-title {
        text-style: bold;
        color: $accent;
        text-align: center;
        margin-bottom: 1;
    }

    ContextHelpScreen .context-help-body {
        color: $text;
    }
    """

    def __init__(self, content: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content = content

    def compose(self) -> ComposeResult:
        """Compose the centered help card."""
        with Vertical():
            yield Static("Context keymap", classes="context-help-title")
            yield Static(self._content, classes="context-help-body")

    def on_key(self, _event: events.Key) -> None:
        """Dismiss on any keypress (mirrors ``top`` help behavior)."""
        self.dismiss(None)


class ContextViewerScreen(ModalScreen[None]):
    """Modal showing token usage + context status and a goals/steps view.

    Interactive keymaps (ported from the autopilot ``top`` dashboard):
    ``t`` loop↔global, ``a`` all/active, ``s`` steps cycle, ``l`` rail-flow,
    ``d`` density, ``+/-`` refresh interval, ``Space`` force refresh,
    ``j/k``/page/``g``/``G`` scroll, ``h``/``?`` help, ``q``/``Esc`` quit.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("q", "quit_viewer", "Quit", show=False, priority=True),
        Binding("h", "toggle_help", "Help", show=False, priority=True),
        Binding("question_mark", "toggle_help", "Help", show=False, priority=True),
        Binding("a", "toggle_all", "All goals", show=False, priority=True),
        Binding("s", "cycle_steps", "Steps", show=False, priority=True),
        Binding("l", "toggle_loops", "Rail flow", show=False, priority=True),
        Binding("d", "cycle_density", "Density", show=False, priority=True),
        Binding("plus", "faster", "Faster", show=False, priority=True),
        Binding("equals_sign", "faster", "Faster", show=False, priority=True),
        Binding("minus", "slower", "Slower", show=False, priority=True),
        Binding("underscore", "slower", "Slower", show=False, priority=True),
        Binding("space", "force_refresh", "Refresh", show=False, priority=True),
        Binding("up,k", "scroll_up", "Up", show=False, priority=True),
        Binding("down,j", "scroll_down", "Down", show=False, priority=True),
        Binding("pageup", "page_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page down", show=False, priority=True),
        Binding("ctrl+d", "half_page_down", "Half down", show=False, priority=True),
        Binding("ctrl+u", "half_page_up", "Half up", show=False, priority=True),
        Binding("ctrl+f", "page_down", "Page down", show=False, priority=True),
        Binding("ctrl+b", "page_up", "Page up", show=False, priority=True),
        Binding("ctrl+e", "scroll_down", "Down", show=False, priority=True),
        Binding("ctrl+y", "scroll_up", "Up", show=False, priority=True),
        Binding("g", "scroll_top", "Top", show=False, priority=True),
        Binding("G", "scroll_bottom", "Bottom", show=False, priority=True),
        Binding("home", "scroll_top", "Top", show=False, priority=True),
        Binding("end", "scroll_bottom", "Bottom", show=False, priority=True),
        Binding("t", "toggle_view", "Global", show=False, priority=True),
    ]

    CSS = """
    ContextViewerScreen {
        align: center middle;
        background: transparent;
    }

    ContextViewerScreen > Vertical {
        width: 96;
        max-width: 96%;
        height: 84%;
        max-height: 90%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    ContextViewerScreen .context-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 0;
    }

    ContextViewerScreen .context-summary {
        height: auto;
        min-height: 4;
        background: $panel;
        border: solid $primary 50%;
        padding: 0 1;
        margin-bottom: 1;
    }

    ContextViewerScreen .context-summary TokenUsagePanel,
    ContextViewerScreen .context-summary StatusPanel,
    ContextViewerScreen .context-summary AutopilotProgressPanel {
        min-height: 0;
        margin-bottom: 0;
        padding: 0;
    }

    ContextViewerScreen ScrollableContainer {
        height: 1fr;
        min-height: 10;
        scrollbar-gutter: stable;
        background: $background;
        padding: 0 1;
    }

    ContextViewerScreen RailFlowPanel,
    ContextViewerScreen StepDagTreePanel,
    ContextViewerScreen GoalDagPanel {
        min-height: 0;
        margin-bottom: 0;
    }

    ContextViewerScreen .context-help {
        height: 1;
        min-height: 1;
        color: $text-muted;
        margin-top: 0;
        text-align: center;
    }

    ContextViewerScreen.compact > Vertical {
        width: 98%;
        max-width: 98%;
        height: 91%;
        max-height: 96%;
        padding: 0 1;
    }

    ContextViewerScreen.compact .context-summary {
        min-height: 3;
        margin-bottom: 0;
    }

    ContextViewerScreen.compact ScrollableContainer {
        min-height: 8;
    }

    ContextViewerScreen.compact .context-title {
        margin-bottom: 0;
    }

    ContextViewerScreen.compact .context-help {
        margin-top: 0;
    }
    """

    def __init__(
        self,
        loop_id: str | None,
        *,
        daemon_session: Any = None,
        load_token_snapshot: LoadTokenSnapshotFn | None = None,
        initial_token_snapshot: TokenUsageSnapshot | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the ``/context`` screen."""
        super().__init__(**kwargs)
        self._loop_id = loop_id or "unknown"
        self._daemon_session = daemon_session
        self._goals: list[dict[str, Any]] = []
        self._load_token_snapshot = load_token_snapshot
        self._token_snapshot = initial_token_snapshot
        self._autopilot_context: AutopilotContext | None = None
        self._is_autopilot: bool = False
        self._state = ContextViewState()
        self._refresh_timer: Any = None
        self._view_mode: Literal["loop", "global"] = "loop"
        self._global_snapshot: GlobalContextSnapshot | None = None

    def compose(self) -> ComposeResult:
        """Compose head (token + status), the goals/steps body, and footer."""
        with Vertical():
            yield Static("Context", classes="context-title")
            with Vertical(classes="context-summary", id="context-summary"):
                yield TokenUsagePanel(snapshot=self._token_snapshot)
                if self._is_autopilot:
                    yield AutopilotProgressPanel(context=self._autopilot_context)
                else:
                    yield StatusPanel(goals=self._goals, loop_id=self._loop_id)
            if self._is_autopilot:
                yield ScrollableContainer(
                    RailFlowPanel(
                        rail_flow=self._autopilot_context.rail_flow
                        if self._autopilot_context
                        else None
                    ),
                    StepDagTreePanel(
                        step_dags=self._autopilot_context.step_dags
                        if self._autopilot_context
                        else [],
                        steps_mode=self._state.steps_mode,
                    ),
                    id="context-loop-body",
                )
            else:
                yield ScrollableContainer(
                    GoalDagPanel(goals=self._goals),
                    id="context-loop-body",
                )
            yield ScrollableContainer(
                GlobalTopPanel(snapshot=self._global_snapshot, state=self._state),
                id="context-global-body",
            )
            yield Static(self._render_footer(), id="context-footer")

    def on_mount(self) -> None:
        """Apply ASCII border if needed and start refresh workers."""
        self._apply_responsive_mode()
        if is_ascii_mode():
            container = self.query_one(Vertical)
            from soothe_cli.display import theme

            colors = theme.get_theme_colors(self)
            container.styles.border = ("ascii", colors.success)
        self._apply_view_visibility()
        self._active_body().focus()
        self.run_worker(self._async_refresh(), exclusive=False, group="context-viewer")
        self._refresh_timer = self.set_interval(self._state.interval, self._schedule_refresh)

    # ── keymap actions (ported from autopilot `top`) ──────────────────

    def _after_key(self) -> None:
        """Re-render filtered panels and honor a forced refresh."""
        state = self._state
        self._reapply_view_state()
        if state.force_refresh:
            state.force_refresh = False
            self._schedule_refresh()

    def action_cancel(self) -> None:
        """Dismiss the modal (Esc)."""
        self.dismiss(None)

    def action_quit_viewer(self) -> None:
        """Dismiss the modal (q)."""
        self.dismiss(None)

    def action_toggle_help(self) -> None:
        """Push the keymap help view (h / ?); any key dismisses it."""
        self.app.push_screen(ContextHelpScreen(self._render_help()))

    def action_toggle_all(self) -> None:
        """Toggle all-goals vs active-only (a)."""
        apply_context_key(self._state, "a")
        self._after_key()

    def action_cycle_steps(self) -> None:
        """Cycle step visibility off → active → all (s)."""
        apply_context_key(self._state, "s")
        self._after_key()

    def action_toggle_loops(self) -> None:
        """Toggle the rail-flow panel (l)."""
        apply_context_key(self._state, "l")
        self._after_key()

    def action_cycle_density(self) -> None:
        """Cycle density compact → steps → full (d)."""
        apply_context_key(self._state, "d")
        self._after_key()

    def action_faster(self) -> None:
        """Decrease the refresh interval (+ / =)."""
        apply_context_key(self._state, "+")
        self._restart_refresh_timer()
        self._after_key()

    def action_slower(self) -> None:
        """Increase the refresh interval (- / _)."""
        apply_context_key(self._state, "-")
        self._restart_refresh_timer()
        self._after_key()

    def action_force_refresh(self) -> None:
        """Force an immediate data refresh (Space)."""
        apply_context_key(self._state, "space")
        self._after_key()

    # ── scroll actions ────────────────────────────────────────────────

    def _active_body(self) -> ScrollableContainer:
        """Return the scrollable body for the current view mode."""
        body_id = "context-global-body" if self._view_mode == "global" else "context-loop-body"
        return self.query_one(f"#{body_id}", ScrollableContainer)

    def _apply_view_visibility(self) -> None:
        """Show the active view's body and hide the inactive one."""
        is_global = self._view_mode == "global"
        try:
            self.query_one("#context-loop-body", ScrollableContainer).styles.display = (
                "none" if is_global else "block"
            )
            self.query_one("#context-global-body", ScrollableContainer).styles.display = (
                "block" if is_global else "none"
            )
            self.query_one("#context-summary", Vertical).styles.display = (
                "none" if is_global else "block"
            )
        except Exception:
            logger.debug("Failed to apply context view visibility", exc_info=True)

    def action_toggle_view(self) -> None:
        """Toggle between this-loop and the global top dashboard (t)."""
        self._view_mode = "global" if self._view_mode == "loop" else "loop"
        self._apply_view_visibility()
        self._active_body().focus()
        if self._view_mode == "global" and self._global_snapshot is None:
            self._schedule_refresh()
        else:
            self._reapply_view_state()
        self._update_footer()

    def _scroll(self, *, delta: int) -> None:
        """Scroll the body by `delta` lines."""
        self._active_body().scroll_relative(y=delta, animate=False)

    def action_scroll_up(self) -> None:
        """Scroll one line up in the body."""
        self._scroll(delta=-1)

    def action_scroll_down(self) -> None:
        """Scroll one line down in the body."""
        self._scroll(delta=1)

    def action_page_up(self) -> None:
        """Scroll up by half the visible body height."""
        body = self._active_body()
        self._scroll(delta=-max(1, body.size.height // 2))

    def action_page_down(self) -> None:
        """Scroll down by half the visible body height."""
        body = self._active_body()
        self._scroll(delta=max(1, body.size.height // 2))

    def action_half_page_up(self) -> None:
        """Scroll up by a quarter of the visible body height (Ctrl+U)."""
        body = self._active_body()
        self._scroll(delta=-max(1, body.size.height // 4))

    def action_half_page_down(self) -> None:
        """Scroll down by a quarter of the visible body height (Ctrl+D)."""
        body = self._active_body()
        self._scroll(delta=max(1, body.size.height // 4))

    def action_scroll_top(self) -> None:
        """Scroll to the top of the body (g / Home)."""
        self._active_body().scroll_to(y=0, animate=False)

    def action_scroll_bottom(self) -> None:
        """Scroll to the bottom of the body (G / End)."""
        body = self._active_body()
        body.scroll_to(y=body.max_scroll_y, animate=False)

    def on_resize(self, _event: events.Resize) -> None:
        """Re-evaluate compact layout mode when terminal size changes."""
        self._apply_responsive_mode()

    # ── refresh + view reapplication ──────────────────────────────────

    def _schedule_refresh(self) -> None:
        """Kick off a background refresh on the timer tick."""
        self.run_worker(self._async_refresh(), exclusive=False, group="context-viewer")

    def _restart_refresh_timer(self) -> None:
        """Restart the auto-refresh timer at the current interval."""
        if self._refresh_timer is not None:
            try:
                self._refresh_timer.stop()
            except Exception:
                logger.debug("Failed to stop context refresh timer", exc_info=True)
        self._refresh_timer = self.set_interval(self._state.interval, self._schedule_refresh)

    def _apply_responsive_mode(self) -> None:
        """Toggle compact layout when terminal is narrow/short."""
        size = self.app.size
        is_compact = size.width < _COMPACT_MIN_WIDTH or size.height < _COMPACT_MIN_HEIGHT
        if is_compact:
            self.add_class("compact")
            return
        self.remove_class("compact")

    async def _async_refresh(self) -> None:
        """Reload context: global snapshot in global mode, else this loop's data."""
        if self._view_mode == "global":
            self._global_snapshot = await load_global_context(self._daemon_session)
            self._reapply_view_state()
            return

        goals = await load_ce_goals(self._loop_id, self._daemon_session)
        token_snapshot = self._token_snapshot
        if self._load_token_snapshot is not None:
            try:
                token_snapshot = await self._load_token_snapshot()
            except Exception:
                logger.debug("Failed to refresh token usage snapshot", exc_info=True)
        self._goals = goals
        self._token_snapshot = token_snapshot

        self._autopilot_context = await load_autopilot_context(
            self._loop_id, self._daemon_session, goals=goals
        )
        if self._autopilot_context is not None and not self._is_autopilot:
            self._is_autopilot = True
            await self._recompose_for_autopilot()

        self._reapply_view_state()

    def _reapply_view_state(self) -> None:
        """Apply the current view state (``a``/``s``/``l``) to cached data, no RPC."""
        state = self._state
        if self._view_mode == "global":
            try:
                self.query_one(GlobalTopPanel).set_snapshot(self._global_snapshot, state=state)
            except Exception:
                logger.debug("Failed to refresh global context panel", exc_info=True)
            self._update_footer()
            return

        try:
            self.query_one(TokenUsagePanel).set_snapshot(self._token_snapshot)
            if self._is_autopilot and self._autopilot_context is not None:
                ctx = self._autopilot_context
                self.query_one(AutopilotProgressPanel).set_context(ctx)
                dags = filter_step_dags_for_view(
                    ctx.step_dags, include_terminal=state.include_terminal
                )
                self.query_one(StepDagTreePanel).set_step_dags(dags, steps_mode=state.steps_mode)
                rail_panel = self.query_one(RailFlowPanel)
                rail_panel.set_rail_flow(ctx.rail_flow)
                rail_panel.styles.display = "block" if state.show_loops else "none"
            else:
                filtered = filter_goals_for_view(
                    self._goals, include_terminal=state.include_terminal
                )
                self.query_one(StatusPanel).set_goals(filtered)
                self.query_one(GoalDagPanel).set_goals(filtered)
        except Exception:
            logger.debug("Failed to refresh context viewer panels", exc_info=True)
        self._update_footer()

    async def _recompose_for_autopilot(self) -> None:
        """Swap legacy panels for autopilot panels on first autopilot detection."""
        try:
            status_panel = self.query_one(StatusPanel)
            status_panel.remove()
            await self.mount(
                AutopilotProgressPanel(context=self._autopilot_context), after=status_panel
            )  # type: ignore[arg-type]
        except Exception:
            logger.debug("StatusPanel swap failed (already swapped?)", exc_info=True)
        try:
            goal_dag = self.query_one(GoalDagPanel)
            goal_dag.remove()
            container = self.query_one("#context-loop-body", ScrollableContainer)
            await container.mount(
                RailFlowPanel(
                    rail_flow=self._autopilot_context.rail_flow if self._autopilot_context else None
                )
            )
            await container.mount(
                StepDagTreePanel(
                    step_dags=self._autopilot_context.step_dags if self._autopilot_context else [],
                    steps_mode=self._state.steps_mode,
                )
            )
        except Exception:
            logger.debug("GoalDagPanel swap failed (already swapped?)", exc_info=True)

    # ── footer + help view rendering ─────────────────────────────────

    def _render_footer(self) -> str:
        """Build the compact one-line footer; full keys live behind ``h``."""
        state = self._state
        mode = "all" if state.include_terminal else "active"
        live_hint = "" if state.include_terminal else " (live)"
        rail = "on" if state.show_loops else "off"
        view = self._view_mode
        return (
            f"[dim]view=[/][yellow]{view}[/][dim]  mode=[/][yellow]{mode}[/][dim]"
            f"{live_hint}  steps=[/][yellow]{state.steps_mode}[/][dim]  rail=[/]"
            f"[yellow]{rail}[/][dim]  delay=[/][yellow]{state.interval:g}s[/]"
            f"[dim]  · h help[/]"
        )

    def _render_help(self) -> str:
        """Build the keymap reference shown in the help view."""
        return "\n".join(
            [
                "[bold]Context keymap[/]",
                "  [yellow]t[/]  toggle this loop ↔ global top dashboard",
                "  [yellow]a[/]  toggle all goals (active \u2194 all incl. terminal)",
                "  [yellow]s[/]  cycle steps (off \u2192 active \u2192 all)",
                "  [yellow]l[/]  toggle rail flow panel",
                "  [yellow]d[/]  cycle density (compact \u2192 steps \u2192 full)",
                "  [yellow]+/-[/]  faster / slower refresh",
                "  [yellow]Space[/]  force refresh now",
                "  [yellow]j/k \u2191\u2193[/]  scroll  [yellow]Ctrl-D/U[/] half page  [yellow]Ctrl-F/B[/] page",
                "  [yellow]g/G[/] or [yellow]Home/End[/]  scroll top / bottom",
                "  [yellow]h/?[/]  toggle this help  [yellow]q/Esc[/]  close",
                "",
                "[dim]any key dismisses this view[/]",
            ]
        )

    def _update_footer(self) -> None:
        """Refresh the footer text from the current view state."""
        try:
            self.query_one("#context-footer", Static).update(self._render_footer())
        except Exception:
            logger.debug("Failed to update context footer", exc_info=True)
