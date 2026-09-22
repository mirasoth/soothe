"""Unit tests for ContextWindowManager (RFC-224).

Tests cover:
- estimate_checkpoint_tokens: Token count estimation from checkpoint messages
- estimate_checkpoint_tokens_sync: Sync helper for pre-loaded checkpoints
- should_compact: Threshold comparison logic
- check_and_compact_if_needed: Full flow (estimate → check → compact)
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe.sloop.engine.execute.context_window_manager import (
    ContextCompactionResult,
    ContextWindowManager,
)
from soothe.sloop.state.schemas import LoopState


class MockCheckpoint:
    """Mock checkpoint for testing token estimation."""

    def __init__(self, messages: list) -> None:
        self.channel_values = {"messages": messages}


class MockMessage:
    """Mock message for testing token estimation."""

    def __init__(self, content: str) -> None:
        self.content = content


class MockConfig:
    """Mock SootheConfig for testing threshold settings."""

    def __init__(
        self,
        context_limit: int = 200_000,
        threshold_pct: float = 0.80,
        max_consecutive_compact_failures: int = 3,
    ) -> None:
        self.agent = MagicMock()
        self.agent.loop = MagicMock()
        self.agent.loop.context_window_limit = context_limit
        self.agent.loop.context_overflow_threshold_pct = threshold_pct
        self.agent.loop.max_consecutive_compact_failures = max_consecutive_compact_failures


class TestEstimateCheckpointTokensSync:
    """Tests for estimate_checkpoint_tokens_sync method."""

    def test_empty_checkpoint_returns_0(self) -> None:
        """Empty checkpoint → 0 tokens."""
        manager = ContextWindowManager(None, None)
        checkpoint = MockCheckpoint(messages=[])

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        assert result == 0

    def test_no_channel_values_returns_0(self) -> None:
        """Checkpoint without channel_values → 0 tokens."""
        manager = ContextWindowManager(None, None)
        checkpoint = MagicMock()
        checkpoint.channel_values = None

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        assert result == 0

    def test_string_content_counts_tokens(self) -> None:
        """String message content is counted."""
        manager = ContextWindowManager(None, None)
        messages = [MockMessage(content="Hello world")]
        checkpoint = MockCheckpoint(messages=messages)

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        # "Hello world" → ~2-3 tokens depending on tokenizer
        assert result > 0
        assert result < 10  # Sanity check

    def test_list_content_counts_text_blocks(self) -> None:
        """List content with text blocks is counted."""
        manager = ContextWindowManager(None, None)

        class MockMessageWithList:
            content = [{"text": "First block"}, {"text": "Second block"}]

        checkpoint = MockCheckpoint(messages=[MockMessageWithList()])

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        # Two short text blocks
        assert result > 0
        assert result < 10

    def test_multiple_messages_summed(self) -> None:
        """Multiple messages are summed."""
        manager = ContextWindowManager(None, None)
        messages = [
            MockMessage(content="First message"),
            MockMessage(content="Second message"),
        ]
        checkpoint = MockCheckpoint(messages=messages)

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        # Two messages
        assert result > 0
        assert result >= 4  # At least 2 tokens per short message


class TestShouldCompact:
    """Tests for should_compact method."""

    def test_below_threshold_returns_false(self) -> None:
        """Below threshold → no compaction needed."""
        config = MockConfig(context_limit=200_000, threshold_pct=0.80)
        manager = ContextWindowManager(None, config)

        # 140k tokens < 160k threshold
        result = manager.should_compact(140_000)
        assert result is False

    def test_at_threshold_returns_true(self) -> None:
        """At threshold → compaction needed."""
        config = MockConfig(context_limit=200_000, threshold_pct=0.80)
        manager = ContextWindowManager(None, config)

        # 160k tokens >= 160k threshold (80%)
        result = manager.should_compact(160_000)
        assert result is True

    def test_above_threshold_returns_true(self) -> None:
        """Above threshold → compaction needed."""
        config = MockConfig(context_limit=200_000, threshold_pct=0.80)
        manager = ContextWindowManager(None, config)

        # 180k tokens > 160k threshold
        result = manager.should_compact(180_000)
        assert result is True

    def test_no_config_uses_defaults(self) -> None:
        """No config → use default threshold (80%)."""
        manager = ContextWindowManager(None, None)

        # Default: 200k limit, 80% threshold = 160k
        result = manager.should_compact(160_000)
        assert result is True

        result = manager.should_compact(140_000)
        assert result is False


class TestEstimateCheckpointTokensAsync:
    """Tests for async estimate_checkpoint_tokens method."""

    @pytest.mark.asyncio
    async def test_no_checkpointer_returns_0(self) -> None:
        """No checkpointer → 0 tokens."""
        manager = ContextWindowManager(None, None)

        result = await manager.estimate_checkpoint_tokens("thread1")
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_checkpoint_tuple_returns_0(self) -> None:
        """No checkpoint for thread → 0 tokens."""
        mock_checkpointer = AsyncMock()
        mock_checkpointer.aget_tuple = AsyncMock(return_value=None)
        manager = ContextWindowManager(mock_checkpointer, None)

        result = await manager.estimate_checkpoint_tokens("thread1")
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_sync_estimation(self) -> None:
        """Async method delegates to sync helper."""
        mock_checkpointer = AsyncMock()
        messages = [MockMessage(content="Test message")]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, None)

        result = await manager.estimate_checkpoint_tokens("thread1")
        assert result > 0


class TestCheckAndCompactIfNeeded:
    """Tests for check_and_compact_if_needed method."""

    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self) -> None:
        """Below threshold → None (no compaction)."""
        config = MockConfig(context_limit=200_000, threshold_pct=0.80)
        mock_checkpointer = AsyncMock()
        messages = [MockMessage(content="Short message")]  # ~2 tokens
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_checkpoint_returns_none(self) -> None:
        """Empty checkpoint → None."""
        manager = ContextWindowManager(None, None)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is None

    @pytest.mark.asyncio
    async def test_compaction_pending_returns_none(self) -> None:
        """Compaction not implemented yet → logs warning, returns None.

        Note: Full compaction implementation pending SummarizationMiddleware
        API verification (RFC-224 Phase 3).
        """
        config = MockConfig(context_limit=100, threshold_pct=0.80)
        mock_checkpointer = AsyncMock()
        # Create large message to exceed threshold
        large_content = "x" * 500  # ~125 tokens
        messages = [MockMessage(content=large_content)]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.check_and_compact_if_needed("thread1", state)
        # Compaction placeholder returns None
        assert result is None


class TestCompactCheckpointInplace:
    """Tests for compact_checkpoint_inplace method."""

    @pytest.mark.asyncio
    async def test_no_checkpointer_returns_none(self) -> None:
        """No checkpointer → None."""
        manager = ContextWindowManager(None, None)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.compact_checkpoint_inplace("thread1", state)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_checkpoint_returns_none(self) -> None:
        """No checkpoint for thread → None."""
        mock_checkpointer = AsyncMock()
        mock_checkpointer.aget_tuple = AsyncMock(return_value=None)
        manager = ContextWindowManager(mock_checkpointer, None)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.compact_checkpoint_inplace("thread1", state)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_messages_returns_none(self) -> None:
        """Empty messages → None."""
        mock_checkpointer = AsyncMock()
        checkpoint = MockCheckpoint(messages=[])
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, None)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.compact_checkpoint_inplace("thread1", state)
        assert result is None


class TestContextCompactionResult:
    """Tests for ContextCompactionResult dataclass."""

    def test_basic_result(self) -> None:
        """Basic result with required fields."""
        result = ContextCompactionResult(
            thread_id="thread1",
            tokens_before=180_000,
            tokens_after=120_000,
            messages_removed=50,
        )

        assert result.thread_id == "thread1"
        assert result.tokens_before == 180_000
        assert result.tokens_after == 120_000
        assert result.messages_removed == 50
        assert result.summary_preview is None

    def test_result_with_summary(self) -> None:
        """Result with summary preview."""
        result = ContextCompactionResult(
            thread_id="thread1",
            tokens_before=180_000,
            tokens_after=120_000,
            messages_removed=50,
            summary_preview="Compacted 50 messages...",
        )

        assert result.summary_preview == "Compacted 50 messages..."

    def test_frozen_dataclass(self) -> None:
        """Result is frozen (immutable)."""
        result = ContextCompactionResult(
            thread_id="thread1",
            tokens_before=180_000,
            tokens_after=120_000,
            messages_removed=50,
        )

        with pytest.raises(Exception):  # FrozenInstanceError
            result.tokens_after = 100_000  # type: ignore[misc]


class TestEstimateCheckpointTokensUnifiedAPI:
    """IG-761: estimate_checkpoint_tokens_sync uses the unified estimate_token_usage API."""

    def test_uses_actual_usage_metadata_when_present(self) -> None:
        """When AI messages carry usage_metadata, the real total is used (no estimation)."""
        from langchain_core.messages import AIMessage, HumanMessage

        manager = ContextWindowManager(None, None)
        messages = [
            HumanMessage(content="x" * 100_000),
            AIMessage(
                content="done",
                usage_metadata={"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050},
            ),
        ]
        checkpoint = MockCheckpoint(messages=messages)

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        # Actual-first: 1050, not the much larger estimated prompt.
        assert result == 1050

    def test_estimates_prompt_tokens_on_fallback(self) -> None:
        """Without usage_metadata, prompt tokens are estimated (not just output)."""
        from langchain_core.messages import AIMessage, HumanMessage

        manager = ContextWindowManager(None, None)
        messages = [
            HumanMessage(content="prompt " * 200),
            AIMessage(content="response " * 200),
        ]
        checkpoint = MockCheckpoint(messages=messages)

        result = manager.estimate_checkpoint_tokens_sync(checkpoint)
        # Must exceed an output-only estimate, proving input is counted.
        from soothe_nano.utils.token_counting import count_tokens

        output_only = count_tokens("response " * 200)
        assert result > output_only
        assert result > 0

    def test_passes_model_hint_from_config(self) -> None:
        """When config is present, the model hint is threaded to the estimator."""
        from unittest.mock import patch

        from langchain_core.messages import HumanMessage

        config = MockConfig()
        # MockConfig.agent is a MagicMock; attach a router with a default model.
        config.router = MagicMock()
        config.router.default = "gpt-4o"

        manager = ContextWindowManager(None, config)
        messages = [HumanMessage(content="hello world")]
        checkpoint = MockCheckpoint(messages=messages)

        with patch(
            "soothe_nano.utils.token_usage.estimate_token_usage",
            wraps=__import__(
                "soothe_nano.utils.token_usage", fromlist=["estimate_token_usage"]
            ).estimate_token_usage,
        ) as spy:
            result = manager.estimate_checkpoint_tokens_sync(checkpoint)
            assert spy.called
            _, kwargs = spy.call_args
            assert kwargs.get("model") == "gpt-4o"
        assert result > 0


class TestCompactCircuitBreaker:
    """Tests for the auto-compact circuit breaker (consecutive failure tracking).

    After the configured number of consecutive compaction failures, the
    circuit breaker trips and auto-compact stops attempting, surfacing the
    error to the planner for context reduction.
    """

    @pytest.mark.asyncio
    async def test_failure_counter_starts_at_zero(self) -> None:
        """Fresh manager has zero consecutive failures."""
        manager = ContextWindowManager(None, None)
        assert manager._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_none_result_increments_counter(self) -> None:
        """A None compaction result increments the failure counter."""
        # context_limit=50 → threshold 40 tokens; "x"*500 ≈ 66 tokens > 40.
        config = MockConfig(context_limit=50, threshold_pct=0.80)
        mock_checkpointer = AsyncMock()
        large_content = "x" * 500
        messages = [MockMessage(content=large_content)]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        await manager.check_and_compact_if_needed("thread1", state)
        assert manager._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_exception_increments_counter(self) -> None:
        """An exception during compaction increments the failure counter."""
        config = MockConfig(context_limit=50, threshold_pct=0.80)
        mock_checkpointer = AsyncMock()
        mock_checkpointer.aget_tuple = AsyncMock(side_effect=RuntimeError("checkpoint unavailable"))
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is None
        assert manager._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_after_threshold(self) -> None:
        """After threshold consecutive failures, auto-compact is disabled."""
        config = MockConfig(
            context_limit=50,
            threshold_pct=0.80,
            max_consecutive_compact_failures=3,
        )
        mock_checkpointer = AsyncMock()
        large_content = "x" * 500
        messages = [MockMessage(content=large_content)]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        # Two failures: counter reaches 2 (< threshold 3)
        await manager.check_and_compact_if_needed("thread1", state)
        assert manager._consecutive_failures == 1
        await manager.check_and_compact_if_needed("thread1", state)
        assert manager._consecutive_failures == 2

        # compact_checkpoint_inplace should still be invoked while below threshold
        assert mock_checkpointer.aget_tuple.await_count > 0

        # Third failure reaches threshold
        await manager.check_and_compact_if_needed("thread1", state)
        assert manager._consecutive_failures == 3

        # Fourth call: circuit breaker trips — no compaction attempted
        aget_count_before = mock_checkpointer.aget_tuple.await_count
        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is None
        # No additional checkpoint fetches (breaker returned early)
        assert mock_checkpointer.aget_tuple.await_count == aget_count_before

    @pytest.mark.asyncio
    async def test_success_resets_counter(self) -> None:
        """A successful compaction resets the failure counter to zero."""
        config = MockConfig(
            context_limit=50,
            threshold_pct=0.80,
            max_consecutive_compact_failures=3,
        )
        mock_checkpointer = AsyncMock()
        large_content = "x" * 500
        messages = [MockMessage(content=large_content)]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        # Simulate one prior failure
        manager._consecutive_failures = 1

        # Patch compact_checkpoint_inplace to return a successful result
        success_result = ContextCompactionResult(
            thread_id="thread1",
            tokens_before=500,
            tokens_after=10,
            messages_removed=1,
        )
        manager.compact_checkpoint_inplace = AsyncMock(return_value=success_result)

        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is success_result
        assert manager._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_no_config_uses_default_threshold(self) -> None:
        """Without config, the default circuit-breaker threshold is 3."""
        manager = ContextWindowManager(None, None)
        assert manager._max_consecutive_failures() == 3

    @pytest.mark.asyncio
    async def test_config_threshold_respected(self) -> None:
        """A custom threshold is honored by the circuit breaker."""
        config = MockConfig(
            context_limit=50,
            threshold_pct=0.80,
            max_consecutive_compact_failures=1,
        )
        mock_checkpointer = AsyncMock()
        large_content = "x" * 500
        messages = [MockMessage(content=large_content)]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        # First call: failure increments to 1 (== threshold 1)
        await manager.check_and_compact_if_needed("thread1", state)
        assert manager._consecutive_failures == 1

        # Second call: breaker already tripped, no attempt
        aget_count_before = mock_checkpointer.aget_tuple.await_count
        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is None
        assert mock_checkpointer.aget_tuple.await_count == aget_count_before

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_touch_counter(self) -> None:
        """When compaction is not needed, the failure counter is untouched."""
        config = MockConfig(context_limit=200_000, threshold_pct=0.80)
        mock_checkpointer = AsyncMock()
        messages = [MockMessage(content="short")]
        checkpoint = MockCheckpoint(messages=messages)
        checkpoint_tuple = MagicMock()
        checkpoint_tuple.checkpoint = checkpoint
        mock_checkpointer.aget_tuple = AsyncMock(return_value=checkpoint_tuple)
        manager = ContextWindowManager(mock_checkpointer, config)
        state = LoopState(thread_id="thread1", goal="test goal")

        # Pre-set a failure count to ensure it's not reset by a no-op call
        manager._consecutive_failures = 2
        result = await manager.check_and_compact_if_needed("thread1", state)
        assert result is None
        # Counter unchanged — below-threshold is not a failure nor a recovery
        assert manager._consecutive_failures == 2
