"""Tests for the Reasoning branch on the step card.

Verifies that captured thinking-token reasoning is rendered in the
Reasoning section of the step card activity tree, with proper ordering
(before To-do), truncation, and line capping.
"""

from __future__ import annotations

from typing import Any

from soothe_cli.display.preview_limits import (
    STEP_CARD_REASONING_PREVIEW_LINES,
)
from soothe_cli.settings import get_glyphs
from soothe_cli.tui.widgets.messages.cognition_step_activity import (
    StepActivityTree,
    StepRowIndex,
    has_task_activity_body,
)


def _empty_index() -> StepRowIndex:
    """An empty row index (no tools, no tasks)."""
    return StepRowIndex()


def test_has_task_activity_body_true_with_reasoning() -> None:
    """Reasoning text alone should trigger the activity panel."""
    index = _empty_index()
    assert has_task_activity_body(index, [], {}, reasoning="thinking...")


def test_has_task_activity_body_false_without_reasoning() -> None:
    """No reasoning and no tools means no activity panel."""
    index = _empty_index()
    assert not has_task_activity_body(index, [], {}, reasoning="")


def test_has_task_activity_body_false_with_whitespace_reasoning() -> None:
    """Whitespace-only reasoning should not trigger the panel."""
    index = _empty_index()
    assert not has_task_activity_body(index, [], {}, reasoning="   \n  ")


def test_render_reasoning_section_appears_before_todo() -> None:
    """Reasoning section header appears before To-do header."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="I should check the file first.",
        todos=[{"content": "Read file", "status": "pending"}],
    )
    text = str(content)
    reasoning_pos = text.find("Reasoning")
    todo_pos = text.find("To-do")
    assert reasoning_pos >= 0
    assert todo_pos >= 0
    assert reasoning_pos < todo_pos


def test_render_reasoning_section_content_shown() -> None:
    """Reasoning text appears as item lines under the header."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="Let me analyze the code.",
    )
    text = str(content)
    assert "Reasoning" in text
    assert "Let me analyze the code." in text


def test_render_reasoning_truncates_long_lines() -> None:
    """Long reasoning lines are truncated with an ellipsis."""
    long_line = "x" * 200
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning=long_line,
        reasoning_line_max_chars=50,
    )
    text = str(content)
    assert "…" in text
    # The full 200-char line should not appear
    assert long_line not in text


def test_render_reasoning_caps_line_count() -> None:
    """Reasoning with more lines than the preview limit shows '+N more lines'."""
    lines = [f"line {i}" for i in range(STEP_CARD_REASONING_PREVIEW_LINES + 5)]
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="\n".join(lines),
        reasoning_preview_lines=STEP_CARD_REASONING_PREVIEW_LINES,
    )
    text = str(content)
    assert "+5 more lines" in text


def test_render_reasoning_empty_string_no_section() -> None:
    """Empty reasoning string does not produce a Reasoning section."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="",
    )
    text = str(content)
    assert "Reasoning" not in text


def test_render_reasoning_multiline_content() -> None:
    """Multi-line reasoning shows each non-empty line as an item."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="First thought\nSecond thought\nThird thought",
    )
    text = str(content)
    assert "First thought" in text
    assert "Second thought" in text
    assert "Third thought" in text


def test_render_reasoning_skips_empty_lines() -> None:
    """Empty lines in reasoning are skipped, not rendered as items."""
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=_empty_index(),
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="Real thought\n\n\nAnother thought",
    )
    text = str(content)
    assert "Real thought" in text
    assert "Another thought" in text


def test_render_reasoning_with_tools_section_ordering() -> None:
    """Reasoning appears before Tool-use when both are present."""
    from soothe_cli.tui.widgets.messages.cognition_step_activity import StepToolRow

    index = StepRowIndex(
        main_tools=[
            StepToolRow(
                tool_call_id="s1:s:0",
                tool_name="read_file",
                args={"file_path": "test.py"},
                phase="success",
            )
        ]
    )
    content = StepActivityTree.render(
        step_id="s1",
        step_status="running",
        index=index,
        subagent_notes=[],
        subagent_notes_by_task={},
        spinner_position=0,
        colors=None,
        g=get_glyphs(),
        reasoning="I need to read the file first.",
    )
    text = str(content)
    reasoning_pos = text.find("Reasoning")
    tool_use_pos = text.find("Tool-use")
    assert reasoning_pos >= 0
    assert tool_use_pos >= 0
    assert reasoning_pos < tool_use_pos


def test_append_reasoning_delta_from_subgraph_namespace() -> None:
    """Reasoning from an execute subgraph namespace should reach the step card.

    The reasoning chunk arrives from the execute subgraph (non-empty namespace),
    but the step card is registered under ``()`` (main graph). The fallback
    to any running step card must not be gated on ``is_main_agent``.
    """
    from unittest.mock import MagicMock

    from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage

    # Simulate a step card registered under the main-graph namespace.
    step_card = CognitionStepMessage(step_id="s1", description="test step")
    step_card.set_running()

    # Simulate an adapter with the step card under () but not under the
    # execute subgraph namespace.
    adapter = MagicMock()
    adapter._step_by_namespace = {(): step_card}
    adapter._current_step_messages = {"s1": step_card}

    # The reasoning chunk arrives from the execute subgraph namespace.
    ns_key = ("execute:thread-abc",)
    from langchain_core.messages import AIMessageChunk

    message = AIMessageChunk(
        content="",
        additional_kwargs={"reasoning_content": "I should check the config."},
    )

    # _resolve_token_target_card returns None (no tool calls to infer from),
    # so the fallback to any running step card must fire.
    router = MagicMock()
    router.active_step_ids = {"s1"}

    # Replicate the routing logic from textual_adapter.
    reasoning_text = str(getattr(message, "additional_kwargs", {}).get("reasoning_content") or "")
    assert reasoning_text  # sanity check

    step_w = adapter._step_by_namespace.get(ns_key)
    assert step_w is None  # not bound under the subgraph namespace

    # Fallback: any running step card.
    for _sw in adapter._current_step_messages.values():
        if getattr(_sw, "_status", "") == "running":
            step_w = _sw
            break

    assert step_w is step_card
    assert hasattr(step_w, "append_reasoning_delta")
    step_w.append_reasoning_delta(reasoning_text)
    assert step_w._reasoning_buffer == "I should check the config."


def test_step_reasoning_candidate_accepts_untagged_step_text() -> None:
    """CoreAgent reply after a tool call: step scope, text, no phase."""
    from langchain_core.messages import AIMessage

    from soothe_cli.tui.textual_adapter import _is_step_reasoning_candidate

    message = AIMessage(content="文件完整内容如下：…")
    assert _is_step_reasoning_candidate(
        is_step_scope=True,
        text="文件完整内容如下：…",
        reasoning_text="",
        message=message,
        phase_loop=getattr(message, "phase", None),
    )


def test_step_reasoning_candidate_rejects_user_visible_phase() -> None:
    """goal_completion / chitchat text keeps its own user-visible path."""
    from langchain_core.messages import AIMessageChunk

    from soothe_cli.tui.textual_adapter import _is_step_reasoning_candidate

    message = AIMessageChunk(content="final report", phase="goal_completion")
    assert not _is_step_reasoning_candidate(
        is_step_scope=True,
        text="final report",
        reasoning_text="",
        message=message,
        phase_loop="goal_completion",
    )


def test_step_reasoning_candidate_rejects_captured_thinking_tokens() -> None:
    """Native `reasoning_content` already flows through its own channel."""
    from langchain_core.messages import AIMessageChunk

    from soothe_cli.tui.textual_adapter import _is_step_reasoning_candidate

    message = AIMessageChunk(
        content="",
        additional_kwargs={"reasoning_content": "internal monologue"},
    )
    assert not _is_step_reasoning_candidate(
        is_step_scope=True,
        text="visible text",
        reasoning_text="internal monologue",
        message=message,
        phase_loop=None,
    )


def test_step_reasoning_candidate_rejects_execute_phases_and_out_of_scope() -> None:
    """execute_step/execute_wave prose and subagent namespaces are not step reasoning."""
    from langchain_core.messages import AIMessage

    from soothe_cli.tui.textual_adapter import _is_step_reasoning_candidate

    message = AIMessage(content="step prose")
    assert not _is_step_reasoning_candidate(
        is_step_scope=True,
        text="step prose",
        reasoning_text="",
        message=message,
        phase_loop="execute_step",
    )
    assert not _is_step_reasoning_candidate(
        is_step_scope=False,
        text="subagent prose",
        reasoning_text="",
        message=message,
        phase_loop=None,
    )


def _adapter_with_step(
    step_card: Any,
    *,
    bindings: dict[tuple[str, ...], Any] | None = None,
    active_step_ids: tuple[str, ...] = ("s1",),
) -> tuple[Any, Any]:
    """Minimal adapter + router pair holding one step card."""
    from types import SimpleNamespace

    from soothe_cli.runtime.state.step_router import StepTaskRouter

    adapter = SimpleNamespace(
        _step_by_namespace=dict(bindings or {}),
        _current_step_messages={"s1": step_card},
        _tool_to_step={},
        _tool_display_by_call_id={},
    )
    router = StepTaskRouter()
    router.active_step_ids.update(active_step_ids)
    return adapter, router


def test_resolve_step_card_for_main_namespace() -> None:
    """Root-namespace step text resolves and binds (the live-run shape)."""
    from soothe_cli.tui.textual_adapter import _resolve_step_card_for_scoped_text
    from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage

    step_card = CognitionStepMessage(step_id="s1", description="read the file")
    step_card.set_running()
    adapter, router = _adapter_with_step(step_card)

    resolved = _resolve_step_card_for_scoped_text(adapter, router, ())
    assert resolved is step_card
    assert adapter._step_by_namespace[()] is step_card


def test_resolve_step_card_for_execute_namespace() -> None:
    """Execute subgraph namespaces resolve through the single-active-step fallback."""
    from soothe_cli.tui.textual_adapter import _resolve_step_card_for_scoped_text
    from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage

    step_card = CognitionStepMessage(step_id="s1", description="read the file")
    step_card.set_running()
    adapter, router = _adapter_with_step(step_card)

    ns_key = ("execute:thread-abc",)
    resolved = _resolve_step_card_for_scoped_text(adapter, router, ns_key)
    assert resolved is step_card
    assert adapter._step_by_namespace[ns_key] is step_card


def test_resolve_step_card_skips_subagent_namespace_and_idle_turn() -> None:
    """Subagent namespaces and turns with no active step stay untouched."""
    from soothe_cli.tui.textual_adapter import _resolve_step_card_for_scoped_text
    from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage

    step_card = CognitionStepMessage(step_id="s1", description="read the file")
    step_card.set_running()

    adapter, router = _adapter_with_step(step_card)
    assert _resolve_step_card_for_scoped_text(adapter, router, ("tools:abc",)) is None
    assert adapter._step_by_namespace == {}

    adapter, router = _adapter_with_step(step_card, active_step_ids=())
    assert _resolve_step_card_for_scoped_text(adapter, router, ()) is None
    assert adapter._step_by_namespace == {}


def test_untagged_step_text_reaches_step_card_reasoning() -> None:
    """End-to-end: a real post-tool message lands in the step card buffer."""
    from langchain_core.messages import AIMessage

    from soothe_cli.tui.textual_adapter import (
        _is_step_reasoning_candidate,
        _resolve_step_card_for_scoped_text,
    )
    from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage

    step_card = CognitionStepMessage(step_id="s1", description="read the file")
    step_card.set_running()
    adapter, router = _adapter_with_step(step_card)

    message = AIMessage(content="文件完整内容如下：PROBE-MARKER-ALPHA")
    text = str(message.content)
    assert _is_step_reasoning_candidate(
        is_step_scope=True,
        text=text,
        reasoning_text="",
        message=message,
        phase_loop=getattr(message, "phase", None),
    )
    resolved = _resolve_step_card_for_scoped_text(adapter, router, (), message)
    assert resolved is step_card
    resolved.append_reasoning_delta(text)

    assert step_card._reasoning_buffer == text
    assert step_card._has_task_activity_body()


def test_append_reasoning_delta_from_dict_message() -> None:
    """Reasoning extracted from a wire dict message (normalization failure)."""
    from soothe_cli.tui.widgets.messages.cognition_step import CognitionStepMessage

    step_card = CognitionStepMessage(step_id="s1", description="test step")
    step_card.set_running()

    # A wire dict that failed to normalize back to AIMessageChunk.
    message = {
        "type": "AIMessageChunk",
        "content": "",
        "additional_kwargs": {"reasoning_content": "Dict-path reasoning."},
    }

    reasoning_text = ""
    if isinstance(message, dict):
        ak = message.get("additional_kwargs")
        if isinstance(ak, dict):
            reasoning_text = str(ak.get("reasoning_content") or "")

    assert reasoning_text == "Dict-path reasoning."
    step_card.append_reasoning_delta(reasoning_text)
    assert step_card._reasoning_buffer == "Dict-path reasoning."
