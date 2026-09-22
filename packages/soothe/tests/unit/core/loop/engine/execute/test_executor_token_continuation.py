"""Token-budget auto-continuation in the Act stream loop.

When the LLM hits max_tokens mid-turn, the executor injects a 'continue'
nudge and re-dispatches the stream, accumulating output across hops.
Diminishing-returns detection stops continuation when a hop produces
fewer than the configured minimum token delta after 3+ continuations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from soothe.sloop.engine.execute.executor import Executor
from soothe.sloop.engine.execute.step_wave_types import _StreamCollectChunk
from soothe.sloop.plans.wired_subagent_plan import _WIRED_SUBAGENT_EXPECTED_OUTPUT
from soothe.sloop.state.schemas import StepAction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _empty_async_gen(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
    """Yield nothing — placeholder for mock agent streams."""
    if False:  # pragma: no cover
        yield


def _ai_with_stop_reason(
    content: str,
    *,
    stop_reason: str | None = None,
    finish_reason: str | None = None,
    output_tokens: int = 0,
) -> AIMessage:
    """Build an AIMessage with the given stop/finish reason and usage metadata."""
    response_metadata: dict[str, Any] = {}
    if stop_reason is not None:
        response_metadata["stop_reason"] = stop_reason
    if finish_reason is not None:
        response_metadata["finish_reason"] = finish_reason
    kwargs: dict[str, Any] = {"response_metadata": response_metadata}
    if output_tokens:
        kwargs["usage_metadata"] = {
            "input_tokens": 100,
            "output_tokens": output_tokens,
            "total_tokens": 100 + output_tokens,
        }
    return AIMessage(content=content, **kwargs)


def _make_executor(config: Any = None) -> Executor:
    """Build an Executor with a mock CoreAgent and optional config."""
    mock_agent = MagicMock()
    mock_agent.execution_astream = MagicMock(side_effect=lambda *a, **k: _empty_async_gen())
    mock_agent.execution_aget_state = AsyncMock(return_value=MagicMock())
    mock_agent.aget_state = AsyncMock(return_value=MagicMock())
    mock_agent.can_read_graph_state = False
    return Executor(mock_agent, config=config)


def _make_step() -> StepAction:
    return StepAction(
        id="step-cont",
        description="Generate a long detailed analysis report",
        expected_output=_WIRED_SUBAGENT_EXPECTED_OUTPUT,
        requires_tool_use=True,
    )


# ---------------------------------------------------------------------------
# _detect_max_tokens_truncation unit tests
# ---------------------------------------------------------------------------


class TestDetectMaxTokensTruncation:
    def test_anthropic_stop_reason_max_tokens(self) -> None:
        msg = _ai_with_stop_reason("partial", stop_reason="max_tokens", output_tokens=4096)
        truncated, tokens = Executor._detect_max_tokens_truncation([msg])
        assert truncated is True
        assert tokens == 4096

    def test_openai_finish_reason_length(self) -> None:
        msg = _ai_with_stop_reason("partial", finish_reason="length", output_tokens=2048)
        truncated, tokens = Executor._detect_max_tokens_truncation([msg])
        assert truncated is True
        assert tokens == 2048

    def test_normal_stop_reason_not_truncated(self) -> None:
        msg = _ai_with_stop_reason("done", stop_reason="end_turn", output_tokens=500)
        truncated, tokens = Executor._detect_max_tokens_truncation([msg])
        assert truncated is False
        assert tokens == 0

    def test_tool_use_stop_reason_not_truncated(self) -> None:
        msg = _ai_with_stop_reason("calling tool", stop_reason="tool_use", output_tokens=100)
        truncated, tokens = Executor._detect_max_tokens_truncation([msg])
        assert truncated is False
        assert tokens == 0

    def test_no_ai_messages(self) -> None:
        truncated, tokens = Executor._detect_max_tokens_truncation([])
        assert truncated is False
        assert tokens == 0

    def test_uses_last_ai_message(self) -> None:
        """When multiple AIMessages exist, only the last one's stop reason matters."""
        msgs: list[BaseMessage] = [
            _ai_with_stop_reason("first", stop_reason="max_tokens", output_tokens=1000),
            _ai_with_stop_reason("second", stop_reason="end_turn", output_tokens=500),
        ]
        truncated, tokens = Executor._detect_max_tokens_truncation(msgs)
        assert truncated is False
        assert tokens == 0

    def test_truncated_without_usage_metadata(self) -> None:
        """Truncation detected even when usage_metadata is absent."""
        msg = _ai_with_stop_reason("partial", stop_reason="max_tokens")
        truncated, tokens = Executor._detect_max_tokens_truncation([msg])
        assert truncated is True
        assert tokens == 0

    def test_case_insensitive_stop_reason(self) -> None:
        msg = _ai_with_stop_reason("partial", stop_reason="MAX_TOKENS", output_tokens=300)
        truncated, tokens = Executor._detect_max_tokens_truncation([msg])
        assert truncated is True
        assert tokens == 300


# ---------------------------------------------------------------------------
# Config field tests
# ---------------------------------------------------------------------------


class TestTokenContinuationConfig:
    def test_defaults_when_no_config(self) -> None:
        ex = _make_executor(config=None)
        assert ex._token_continuation_enabled() is True
        assert ex._token_continuation_max() == 5
        assert ex._token_continuation_min_delta() == 500

    def test_reads_from_config(self) -> None:
        config = MagicMock()
        config.agent.loop.token_continuation_enabled = False
        config.agent.loop.token_continuation_max = 3
        config.agent.loop.token_continuation_min_delta = 200
        ex = _make_executor(config=config)
        assert ex._token_continuation_enabled() is False
        assert ex._token_continuation_max() == 3
        assert ex._token_continuation_min_delta() == 200

    def test_strange_loop_config_defaults(self) -> None:
        from soothe.config.models import StrangeLoopConfig

        cfg = StrangeLoopConfig()
        assert cfg.token_continuation_enabled is True
        assert cfg.token_continuation_max == 5
        assert cfg.token_continuation_min_delta == 500


# ---------------------------------------------------------------------------
# Continuation loop integration tests
# ---------------------------------------------------------------------------


class TestContinuationLoop:
    """Tests for the continuation loop in `_execute_step_collecting_events`.

    These tests mock `_stream_and_collect` and `_core_agent_astream_with_interrupt_resume`
    to simulate max_tokens truncation and verify continuation behavior. The
    deliverable gate is patched to always complete so the action-retry loop
    doesn't interfere with continuation testing.
    """

    def _patch_deliverable_complete(self, ex: Executor) -> Any:
        """Patch the deliverable gate to always return complete=True."""

        return patch.object(
            ex,
            "_execute_deliverable_assess_mode",
            return_value="never",
        )

    @pytest.mark.asyncio
    async def test_continuation_accumulates_output_across_hops(self) -> None:
        """When max_tokens truncation is detected, the executor re-dispatches
        and accumulates output from each hop until the model finishes normally."""
        ex = _make_executor(config=None)
        call_count = 0

        async def fake_stream_and_collect(
            _stream: Any, **kwargs: Any
        ) -> AsyncIterator[_StreamCollectChunk]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _StreamCollectChunk.finalized(
                    output="Part one of the report.",
                    main_tool_count=0,
                    messages=[
                        _ai_with_stop_reason(
                            "Part one of the report.",
                            stop_reason="max_tokens",
                            output_tokens=4096,
                        ),
                    ],
                    delegate_final="",
                    outcomes=[],
                    has_error=False,
                    subgraph_tool_count=0,
                )
            else:
                yield _StreamCollectChunk.finalized(
                    output=" Part two completes the report.",
                    main_tool_count=0,
                    messages=[
                        _ai_with_stop_reason(
                            "Part two completes the report.",
                            stop_reason="end_turn",
                            output_tokens=500,
                        ),
                    ],
                    delegate_final="",
                    outcomes=[],
                    has_error=False,
                    subgraph_tool_count=0,
                )

        step = _make_step()
        with (
            patch.object(ex, "_stream_and_collect", side_effect=fake_stream_and_collect),
            patch.object(
                ex, "_core_agent_astream_with_interrupt_resume", return_value=_empty_async_gen()
            ),
            patch.object(ex, "_execute_action_retry_max", return_value=0),
        ):
            result = await ex._execute_step_collecting_events(step, "thread-cont")

        assert call_count == 2
        assert "Part one" in (result.output or "")
        assert "Part two" in (result.output or "")

    @pytest.mark.asyncio
    async def test_no_continuation_when_not_truncated(self) -> None:
        """A normal end_turn stop reason does not trigger continuation."""
        ex = _make_executor(config=None)
        call_count = 0

        async def fake_stream_and_collect(
            _stream: Any, **kwargs: Any
        ) -> AsyncIterator[_StreamCollectChunk]:
            nonlocal call_count
            call_count += 1
            yield _StreamCollectChunk.finalized(
                output="Complete response.",
                main_tool_count=1,
                messages=[
                    _ai_with_stop_reason(
                        "Complete response.",
                        stop_reason="end_turn",
                        output_tokens=500,
                    ),
                ],
                delegate_final="",
                outcomes=[{"type": "code_exec", "tool_name": "run_command"}],
                has_error=False,
                subgraph_tool_count=0,
            )

        step = _make_step()
        with (
            patch.object(ex, "_stream_and_collect", side_effect=fake_stream_and_collect),
            patch.object(
                ex, "_core_agent_astream_with_interrupt_resume", return_value=_empty_async_gen()
            ),
            patch.object(ex, "_execute_action_retry_max", return_value=0),
        ):
            result = await ex._execute_step_collecting_events(step, "thread-cont")

        assert call_count == 1
        assert "Complete response" in (result.output or "")

    @pytest.mark.asyncio
    async def test_continuation_stops_at_max_hops(self) -> None:
        """Continuation stops after reaching the configured max hops."""
        config = MagicMock()
        config.agent.loop.token_continuation_enabled = True
        config.agent.loop.token_continuation_max = 2
        config.agent.loop.token_continuation_min_delta = 1
        config.agent.loop.execute_min_answer_chars = 1
        config.agent.loop.execute_deliverable_assess = "never"
        config.agent.loop.execute_action_retry_max = 0
        config.agent.loop.dispatch_retry_max = 0
        config.agent.loop.dispatch_idle_seconds = 240.0
        config.agent.loop.dispatch_idle_backoff_factor = 1.25
        config.agent.loop.dispatch_idle_backoff_cap_seconds = 600.0
        config.agent.loop.max_tool_calls_per_step = 500
        config.agent.loop.max_subagent_tasks_per_wave = 4
        config.agent.loop.max_step_retries = 0
        config.agent.loop.step_brief_hydration_enabled = False
        config.agent.loop.context_window_limit = 200_000
        config.agent.loop.context_overflow_threshold_pct = 0.80
        config.agent.loop.max_consecutive_compact_failures = 3
        config.agent.loop.concurrency.max_parallel_tools = 5
        config.agent.loop.concurrency.global_max_llm_calls = 0
        config.agent.loop.decompose.max_branch_root = 8
        config.router.default = None
        config.agent.middleware.tool_output = None
        ex = _make_executor(config=config)
        call_count = 0

        async def fake_stream_and_collect(
            _stream: Any, **kwargs: Any
        ) -> AsyncIterator[_StreamCollectChunk]:
            nonlocal call_count
            call_count += 1
            yield _StreamCollectChunk.finalized(
                output=f"Hop {call_count} content. ",
                main_tool_count=0,
                messages=[
                    _ai_with_stop_reason(
                        f"Hop {call_count} content.",
                        stop_reason="max_tokens",
                        output_tokens=2000,
                    ),
                ],
                delegate_final="",
                outcomes=[],
                has_error=False,
                subgraph_tool_count=0,
            )

        step = _make_step()
        with (
            patch.object(ex, "_stream_and_collect", side_effect=fake_stream_and_collect),
            patch.object(
                ex, "_core_agent_astream_with_interrupt_resume", return_value=_empty_async_gen()
            ),
        ):
            result = await ex._execute_step_collecting_events(step, "thread-cont")

        # 1 initial + 2 continuations = 3 total calls
        assert call_count == 3
        assert "Hop 1" in (result.output or "")
        assert "Hop 2" in (result.output or "")
        assert "Hop 3" in (result.output or "")

    @pytest.mark.asyncio
    async def test_diminishing_returns_stops_continuation(self) -> None:
        """After 3+ continuations, a hop below min_delta stops continuation."""
        config = MagicMock()
        config.agent.loop.token_continuation_enabled = True
        config.agent.loop.token_continuation_max = 10
        config.agent.loop.token_continuation_min_delta = 500
        config.agent.loop.execute_min_answer_chars = 1
        config.agent.loop.execute_deliverable_assess = "never"
        config.agent.loop.execute_action_retry_max = 0
        config.agent.loop.dispatch_retry_max = 0
        config.agent.loop.dispatch_idle_seconds = 240.0
        config.agent.loop.dispatch_idle_backoff_factor = 1.25
        config.agent.loop.dispatch_idle_backoff_cap_seconds = 600.0
        config.agent.loop.max_tool_calls_per_step = 500
        config.agent.loop.max_subagent_tasks_per_wave = 4
        config.agent.loop.max_step_retries = 0
        config.agent.loop.step_brief_hydration_enabled = False
        config.agent.loop.context_window_limit = 200_000
        config.agent.loop.context_overflow_threshold_pct = 0.80
        config.agent.loop.max_consecutive_compact_failures = 3
        config.agent.loop.concurrency.max_parallel_tools = 5
        config.agent.loop.concurrency.global_max_llm_calls = 0
        config.agent.loop.decompose.max_branch_root = 8
        config.router.default = None
        config.agent.middleware.tool_output = None
        ex = _make_executor(config=config)
        call_count = 0

        async def fake_stream_and_collect(
            _stream: Any, **kwargs: Any
        ) -> AsyncIterator[_StreamCollectChunk]:
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                output_tokens = 2000
            else:
                output_tokens = 100  # Below min_delta=500
            yield _StreamCollectChunk.finalized(
                output=f"Hop {call_count}. ",
                main_tool_count=0,
                messages=[
                    _ai_with_stop_reason(
                        f"Hop {call_count}.",
                        stop_reason="max_tokens",
                        output_tokens=output_tokens,
                    ),
                ],
                delegate_final="",
                outcomes=[],
                has_error=False,
                subgraph_tool_count=0,
            )

        step = _make_step()
        with (
            patch.object(ex, "_stream_and_collect", side_effect=fake_stream_and_collect),
            patch.object(
                ex, "_core_agent_astream_with_interrupt_resume", return_value=_empty_async_gen()
            ),
        ):
            result = await ex._execute_step_collecting_events(step, "thread-cont")

        # Hops 1-4 produce 2000 tokens (above min_delta).
        # Hop 5 (continuation_count=4, which is >= 3) produces 100 tokens
        # (below min_delta=500), so continuation stops.
        # Total calls: 1 initial + 4 continuations = 5
        assert call_count == 5
        assert "Hop 1" in (result.output or "")
        assert "Hop 5" in (result.output or "")

    @pytest.mark.asyncio
    async def test_continuation_disabled_by_config(self) -> None:
        """When token_continuation_enabled is False, no continuation happens."""
        config = MagicMock()
        config.agent.loop.token_continuation_enabled = False
        config.agent.loop.token_continuation_max = 5
        config.agent.loop.token_continuation_min_delta = 500
        config.agent.loop.execute_min_answer_chars = 1
        config.agent.loop.execute_deliverable_assess = "never"
        config.agent.loop.execute_action_retry_max = 0
        config.agent.loop.dispatch_retry_max = 0
        config.agent.loop.dispatch_idle_seconds = 240.0
        config.agent.loop.dispatch_idle_backoff_factor = 1.25
        config.agent.loop.dispatch_idle_backoff_cap_seconds = 600.0
        config.agent.loop.max_tool_calls_per_step = 500
        config.agent.loop.max_subagent_tasks_per_wave = 4
        config.agent.loop.max_step_retries = 0
        config.agent.loop.step_brief_hydration_enabled = False
        config.agent.loop.context_window_limit = 200_000
        config.agent.loop.context_overflow_threshold_pct = 0.80
        config.agent.loop.max_consecutive_compact_failures = 3
        config.agent.loop.concurrency.max_parallel_tools = 5
        config.agent.loop.concurrency.global_max_llm_calls = 0
        config.agent.loop.decompose.max_branch_root = 8
        config.router.default = None
        config.agent.middleware.tool_output = None
        ex = _make_executor(config=config)
        call_count = 0

        async def fake_stream_and_collect(
            _stream: Any, **kwargs: Any
        ) -> AsyncIterator[_StreamCollectChunk]:
            nonlocal call_count
            call_count += 1
            yield _StreamCollectChunk.finalized(
                output="Truncated output.",
                main_tool_count=0,
                messages=[
                    _ai_with_stop_reason(
                        "Truncated output.",
                        stop_reason="max_tokens",
                        output_tokens=4096,
                    ),
                ],
                delegate_final="",
                outcomes=[],
                has_error=False,
                subgraph_tool_count=0,
            )

        step = _make_step()
        with (
            patch.object(ex, "_stream_and_collect", side_effect=fake_stream_and_collect),
            patch.object(
                ex, "_core_agent_astream_with_interrupt_resume", return_value=_empty_async_gen()
            ),
        ):
            result = await ex._execute_step_collecting_events(step, "thread-cont")

        assert call_count == 1
        assert "Truncated output" in (result.output or "")

    @pytest.mark.asyncio
    async def test_continuation_metrics_recorded(self) -> None:
        """Execution metrics include continuation_count and last_delta_tokens."""
        ex = _make_executor(config=None)
        call_count = 0

        async def fake_stream_and_collect(
            _stream: Any, **kwargs: Any
        ) -> AsyncIterator[_StreamCollectChunk]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _StreamCollectChunk.finalized(
                    output="First part.",
                    main_tool_count=0,
                    messages=[
                        _ai_with_stop_reason(
                            "First part.",
                            stop_reason="max_tokens",
                            output_tokens=3000,
                        ),
                    ],
                    delegate_final="",
                    outcomes=[],
                    has_error=False,
                    subgraph_tool_count=0,
                )
            else:
                yield _StreamCollectChunk.finalized(
                    output=" Second part.",
                    main_tool_count=0,
                    messages=[
                        _ai_with_stop_reason(
                            "Second part.",
                            stop_reason="end_turn",
                            output_tokens=800,
                        ),
                    ],
                    delegate_final="",
                    outcomes=[],
                    has_error=False,
                    subgraph_tool_count=0,
                )

        step = _make_step()
        with (
            patch.object(ex, "_stream_and_collect", side_effect=fake_stream_and_collect),
            patch.object(
                ex, "_core_agent_astream_with_interrupt_resume", return_value=_empty_async_gen()
            ),
            patch.object(ex, "_execute_action_retry_max", return_value=0),
        ):
            result = await ex._execute_step_collecting_events(step, "thread-cont")

        assert call_count == 2
        assert result.step_result is not None
        outcome = result.step_result.outcome
        metrics = outcome.get("execution_metrics", {})
        assert metrics.get("token_continuation_count") == 1
        assert metrics.get("token_continuation_last_delta") == 3000
