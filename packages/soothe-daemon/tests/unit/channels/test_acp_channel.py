"""Unit tests for ACPChannel."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soothe_daemon.channels.acp import (
    _STDIO_SENTINEL,
    ACPChannel,
    _iter_wire_frames,
    _make_text_block,
    _PlanState,
    _session_update_from_block,
)
from soothe_daemon.channels.base import Channel
from soothe_daemon.config.models import ACPConfig
from soothe_daemon.events.constants import OUTPUT_TEXT_DELTA


def make_manager(loop_id: str = "acp:test-session") -> MagicMock:
    """Manager stub carrying the loop-native submission deps the channel needs."""
    manager = MagicMock()
    manager.ensure_loop_id = MagicMock(return_value=loop_id)
    manager.ensure_loop_registered = AsyncMock(return_value=True)
    manager.submit_loop_input = AsyncMock()
    manager._event_bus = MagicMock()
    manager._event_bus.subscribe = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.enqueue = AsyncMock()
    dispatcher.cleanup_loop = AsyncMock()
    manager._loop_input_dispatcher = dispatcher
    return manager


def messages_frame(
    text: str,
    *,
    namespace: list[str] | None = None,
    phase: str | None = None,
    message_type: str = "AIMessageChunk",
) -> dict:
    """A frame shaped as the engine broadcasts assistant text (``mode="messages"``)."""
    message: dict = {"type": message_type, "content": text}
    if phase is not None:
        message["phase"] = phase
    return {
        "type": "event",
        "namespace": namespace or [],
        "mode": "messages",
        "data": (message, {"lc_source": "agent"}),
    }


class TestACPChannelAttributes:
    """Tests for ACPChannel class-level attributes."""

    def test_channel_metadata(self):
        """Test channel name and display name."""
        assert ACPChannel.name == "acp"
        assert ACPChannel.display_name == "ACP"

    def test_capability_flags(self):
        """Test capability flags are correct."""
        assert ACPChannel.supports_inbound is True
        assert ACPChannel.supports_outbound is True
        assert ACPChannel.supports_streaming is True

    def test_inherits_from_channel(self):
        """Test inherits from Channel base class."""
        assert issubclass(ACPChannel, Channel)


class TestACPConfig:
    """Tests for ACPConfig model."""

    def test_default_config(self):
        """Test default config values."""
        cfg = ACPConfig()
        assert cfg.enabled is True
        assert cfg.agent_name == "Soothe"
        assert cfg.agent_description == "Soothe autonomous agent"
        assert cfg.default_model is None
        assert cfg.session_timeout_seconds == 3600

    def test_enabled_config(self):
        """Test enabled config."""
        cfg = ACPConfig(enabled=True, agent_name="Test", agent_description="desc")
        assert cfg.enabled is True
        assert cfg.agent_name == "Test"
        assert cfg.agent_description == "desc"

    def test_custom_model(self):
        """Test custom default_model."""
        cfg = ACPConfig(enabled=True, default_model="gpt-4o")
        assert cfg.default_model == "gpt-4o"


class MockManager:
    """Mock manager for testing."""

    _message_handler = None
    _handshake_callback = None
    _event_bus = None


class TestACPChannelInit:
    """Tests for ACPChannel initialization."""

    def test_basic_init(self):
        """Test basic initialization."""
        config = ACPConfig(enabled=True)
        manager = MockManager()

        channel = ACPChannel(config, manager)

        assert channel.name == "acp"
        assert channel.config == config
        assert channel._manager == manager
        assert channel.is_running is False
        assert channel._get_state().session_map == {}
        assert channel._get_state().pending_permissions == {}

    def test_client_count_zero(self):
        """Test client_count is zero initially."""
        config = ACPConfig(enabled=True)
        manager = MockManager()

        channel = ACPChannel(config, manager)

        assert channel.client_count == 0


class TestACPChannelSession:
    """Tests for ACPChannel session management."""

    @pytest.mark.asyncio
    async def test_session_new_registers_loop_with_cwd(self):
        """session/new registers the loop so a later turn can run on it."""
        config = ACPConfig(enabled=True)
        manager = make_manager(loop_id="acp:test-session-id")

        channel = ACPChannel(config, manager)
        result = await channel._handle_session_new({"cwd": "/tmp/workspace"})

        assert "sessionId" in result
        session_id = result["sessionId"]
        assert session_id in channel._get_state().session_map
        assert channel._get_state().session_map[session_id] == "acp:test-session-id"
        assert channel.client_count == 1

        # The cwd must be persisted as the loop workspace — that is what the
        # runner resolves the agent's working directory from.
        manager.ensure_loop_registered.assert_awaited_once_with(
            "acp:test-session-id", workspace="/tmp/workspace"
        )

        # Clean up consumer task
        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_session_prompt_submits_a_turn_and_waits_for_it(self):
        """session/prompt submits a real turn and answers when the loop goes idle."""
        config = ACPConfig(enabled=True)
        manager = make_manager()
        channel = ACPChannel(config, manager)

        session_id = (await channel._handle_session_new({}))["sessionId"]

        pending = asyncio.create_task(
            channel._handle_session_prompt(
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "Hello"}]}
            )
        )
        try:
            await asyncio.sleep(0)
            # Held open until the turn finishes.
            assert not pending.done()

            manager.submit_loop_input.assert_awaited_once_with(
                "acp:test-session", "Hello", channel="acp", chat_id=session_id
            )
            # Nothing consumes a ChannelMessageReceived event, so the channel
            # must not fall back to publishing one.
            manager.handle_inbound.assert_not_called()

            channel._resolve_pending_turn(_STDIO_SENTINEL, "acp:test-session", "end_turn")
            assert (await pending)["stopReason"] == "end_turn"
        finally:
            pending.cancel()
            for task in channel._get_state().consumer_tasks.values():
                task.cancel()

    @pytest.mark.asyncio
    async def test_session_cancel_stops_the_loop_and_reports_cancelled(self):
        """session/cancel aborts through the dispatcher and releases the prompt."""
        config = ACPConfig(enabled=True)
        manager = make_manager(loop_id="acp:cancel-test")
        channel = ACPChannel(config, manager)

        session_id = (await channel._handle_session_new({}))["sessionId"]

        pending = asyncio.create_task(
            channel._handle_session_prompt(
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "Hello"}]}
            )
        )
        try:
            await asyncio.sleep(0)
            await channel._handle_session_cancel({"sessionId": session_id})

            # The running turn is released as `cancelled`, per the spec.
            assert (await pending)["stopReason"] == "cancelled"

            # And the loop gets the cancel command it actually understands;
            # publishing a `command: cancel` event reached nothing.
            manager._loop_input_dispatcher.enqueue.assert_awaited_once_with(
                "acp:cancel-test",
                {"type": "command", "cmd": "/cancel", "client_id": None},
            )
        finally:
            pending.cancel()
            for task in channel._get_state().consumer_tasks.values():
                task.cancel()


class TestACPChannelEventTranslation:
    """Tests for daemon wire event → ACP block translation."""

    def test_translate_engine_messages_frame(self):
        """A loop-tagged `mode="messages"` frame yields the assistant text."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        blocks = channel._translate_event(
            messages_frame("Hello world", phase="goal_completion")
        )

        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "Hello world"

    def test_translate_messages_frame_with_content_blocks(self):
        """Text is read from `content_blocks` too, and non-text blocks skipped."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        frame = messages_frame("", phase="goal_completion")
        frame["data"] = (
            {
                "type": "ai",
                "phase": "goal_completion",
                "content_blocks": [
                    {"type": "reasoning", "text": "thinking"},
                    {"type": "text", "text": "the answer"},
                ],
            },
            {},
        )

        blocks = channel._translate_event(frame)
        assert [b["text"] for b in blocks] == ["the answer"]

    def test_drops_untagged_execute_wave_narration(self):
        """Only loop-tagged finals are user-facing.

        Regression: a live run forwarded the step executor's own narration
        (`phase=None`) alongside the goal-completion summary, so the answer read
        as a step report followed by a goal report concatenated together.
        Untagged prose outnumbered the real finals 265:157 in that run.
        """
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        assert channel._translate_event(messages_frame("Step completed successfully")) == []

    def test_keeps_other_loop_tagged_finals(self):
        """Chitchat / plan-only turns are still answers, not narration."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        for phase in ("chitchat", "plan_direct", "goal_interrupted"):
            blocks = channel._translate_event(messages_frame("an answer", phase=phase))
            assert [b["text"] for b in blocks] == ["an answer"], phase

    def test_drops_tagged_frames_without_text(self):
        """A tagged frame carrying no text must not emit an empty chunk."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        assert channel._translate_event(messages_frame("", phase="goal_completion")) == []

    def test_ignores_plain_tool_message_frames(self):
        """Tool/system messages ride the same mode and must not render as prose."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        assert channel._translate_event(messages_frame("ls -la", message_type="tool")) == []

    def test_ignores_subgraph_prose_unless_goal_completion(self):
        """Mirrors the TUI: only root-graph prose is user-facing."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        assert (
            channel._translate_event(messages_frame("subagent chatter", namespace=["execute:abc"]))
            == []
        )
        assert (
            channel._translate_event(
                messages_frame("final answer", namespace=["execute:abc"], phase="goal_completion")
            )
            != []
        )

    def test_translate_custom_text_delta(self):
        """`mode="custom"` frames are still translated by their inner type."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        event = {
            "type": "event",
            "namespace": [],
            "mode": "custom",
            "data": {"type": OUTPUT_TEXT_DELTA, "content": "Hello world"},
        }

        blocks = channel._translate_event(event)
        assert len(blocks) == 1
        assert blocks[0]["text"] == "Hello world"

    def test_translate_unknown_event_returns_empty(self):
        """Test that unknown event types return empty list."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        event = {"type": "event", "data": {"type": "unknown_type"}}
        blocks = channel._translate_event(event)
        assert blocks == []

    def test_make_text_block(self):
        """Test text block construction."""
        block = _make_text_block("test content")
        assert block["type"] == "text"
        assert block["text"] == "test content"


class TestACPChannelSessionUpdateShape:
    """The update payload must be a conformant ACP `SessionUpdate`."""

    def test_text_block_becomes_agent_message_chunk(self):
        update = _session_update_from_block({"type": "text", "text": "hi"})

        assert update is not None
        assert update["sessionUpdate"] == "agent_message_chunk"
        assert update["content"] == {"type": "text", "text": "hi"}

    def test_reasoning_and_progress_become_thought_chunks(self):
        for block_type in ("reasoning", "progress"):
            update = _session_update_from_block({"type": block_type, "text": "hmm"})
            assert update is not None
            assert update["sessionUpdate"] == "agent_thought_chunk"
            assert update["content"] == {"type": "text", "text": "hmm"}

    def test_empty_or_unknown_blocks_are_dropped(self):
        assert _session_update_from_block({"type": "text", "text": ""}) is None
        assert _session_update_from_block({"type": "mystery", "text": "x"}) is None

    @pytest.mark.asyncio
    async def test_sends_one_notification_per_block(self):
        """`session/update` carries exactly one SessionUpdate, so blocks fan out."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())
        writes: list[dict] = []

        with patch.object(channel, "_write_jsonrpc", AsyncMock(side_effect=writes.append)):
            await channel._send_session_update(
                "sess-1",
                [
                    {"type": "text", "text": "one"},
                    {"type": "reasoning", "text": "two"},
                ],
            )

        assert [w["params"]["update"]["sessionUpdate"] for w in writes] == [
            "agent_message_chunk",
            "agent_thought_chunk",
        ]
        assert all(w["method"] == "session/update" for w in writes)
        assert all(w["params"]["sessionId"] == "sess-1" for w in writes)


class TestWireFrameIteration:
    """Batched broadcast frames must not be dropped."""

    def test_single_frame_passes_through(self):
        frame = {"type": "event", "mode": "custom", "data": {}}
        assert _iter_wire_frames(frame) == [frame]

    def test_event_batch_is_expanded(self):
        inner = [{"type": "event", "mode": "custom", "data": {}}, {"type": "event"}]
        assert _iter_wire_frames({"type": "event_batch", "events": inner}) == inner

    def test_malformed_batch_yields_nothing(self):
        assert _iter_wire_frames({"type": "event_batch", "events": "nope"}) == []


class TestACPChannelOutput:
    """Tests for ACPChannel stdout output."""

    @pytest.mark.asyncio
    async def test_write_jsonrpc_writes_to_stdout(self):
        """Test that _write_jsonrpc writes NDJSON to stdout."""
        config = ACPConfig(enabled=True)
        channel = ACPChannel(config, MockManager())

        msg = {"jsonrpc": "2.0", "method": "test", "params": {}}
        with patch("soothe_daemon.channels.acp._write_stdout") as mock_write:
            await channel._write_jsonrpc(msg)
            mock_write.assert_called_once()
            written = mock_write.call_args.args[0]
            assert json.loads(written) == msg
            assert written.endswith("\n")


class TestACPPermissionBridge:
    """Tests for the permission model bridge (session/request_permission)."""

    def _make_channel_with_session(self) -> tuple[ACPChannel, str, str]:
        """Create a channel with a registered session for testing.

        Returns:
            (channel, session_id, loop_id) tuple.
        """
        config = ACPConfig(enabled=True)
        manager = MagicMock()
        manager.handle_inbound = AsyncMock(return_value="acp:perm-test")
        manager._event_bus = MagicMock()
        manager._event_bus.subscribe = AsyncMock()
        manager._event_bus.publish = AsyncMock()

        channel = ACPChannel(config, manager)
        # Manually register a session without starting consumer task
        session_id = "test-session-123"
        loop_id = "acp:perm-test"
        channel._get_state().session_map[session_id] = loop_id
        return channel, session_id, loop_id

    def test_is_tool_approval_event_positive(self):
        """Test that __interrupt__ with action_requests is detected."""
        channel, _, _ = self._make_channel_with_session()

        event = {
            "type": "event",
            "loop_id": "acp:perm-test",
            "data": {
                "__interrupt__": {
                    "interrupt_id": "int-001",
                    "action_requests": [
                        {
                            "tool_call_id": "tc-1",
                            "tool_name": "write_file",
                            "args": {"path": "/etc/passwd"},
                        }
                    ],
                }
            },
        }
        assert channel._is_tool_approval_event(event) is True

    def test_is_tool_approval_event_negative(self):
        """Test that non-interrupt events are not detected as tool-approval."""
        channel, _, _ = self._make_channel_with_session()

        event = {
            "type": "event",
            "data": {"type": "soothe.output.text.delta", "content": "hello"},
        }
        assert channel._is_tool_approval_event(event) is False

    def test_is_tool_approval_event_no_action_requests(self):
        """Test __interrupt__ without action_requests is not tool-approval."""
        channel, _, _ = self._make_channel_with_session()

        event = {
            "data": {"__interrupt__": {"type": "ask_user", "questions": []}},
        }
        assert channel._is_tool_approval_event(event) is False

    @pytest.mark.asyncio
    async def test_permission_request_sends_to_client(self):
        """Test that a permission request is sent to the ACP client via stdout."""
        channel, session_id, loop_id = self._make_channel_with_session()

        event = {
            "type": "event",
            "loop_id": loop_id,
            "data": {
                "__interrupt__": {
                    "interrupt_id": "int-001",
                    "action_requests": [
                        {
                            "tool_call_id": "tc-1",
                            "tool_name": "write_file",
                            "args": {"path": "/etc/passwd"},
                        }
                    ],
                }
            },
        }

        # Patch _write_jsonrpc to capture the outbound request
        written_messages: list[dict] = []

        async def _capture_write(msg):
            written_messages.append(msg)

        with patch.object(channel, "_write_jsonrpc", _capture_write):
            # Start the bridge in a task so we can control the future
            bridge_task = asyncio.create_task(
                channel._bridge_permission_request(session_id, loop_id, event)
            )
            await asyncio.sleep(0.1)

            # Verify the permission request was written
            assert len(written_messages) == 1
            req = written_messages[0]
            assert req["method"] == "session/request_permission"
            assert req["params"]["sessionId"] == session_id
            assert req["params"]["toolCall"]["toolCallId"] == "tc-1"
            assert len(req["params"]["options"]) == 4

            # Resolve the pending future with "allow"
            req_id = req["id"]
            fut = channel._get_state().pending_permissions[req_id]
            fut.set_result({"outcome": "selected", "optionId": "allow_once"})

            # Wait for the bridge to complete
            await asyncio.wait_for(bridge_task, timeout=5.0)

        # Verify resume was published on the EventBus
        channel._manager._event_bus.publish.assert_called()
        resume_msg = channel._manager._event_bus.publish.call_args.args[1]
        assert resume_msg["type"] == "command"
        assert resume_msg["command"] == "resume"
        assert "int-001" in resume_msg["resume_payload"]
        assert resume_msg["resume_payload"]["int-001"]["decisions"][0]["type"] == "approve"

        # Clean up
        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_permission_deny_routes_reject(self):
        """Test that a deny response routes a reject decision."""
        channel, session_id, loop_id = self._make_channel_with_session()

        event = {
            "type": "event",
            "loop_id": loop_id,
            "data": {
                "__interrupt__": {
                    "interrupt_id": "int-002",
                    "action_requests": [
                        {
                            "tool_call_id": "tc-2",
                            "tool_name": "run_command",
                            "args": {"command": "rm -rf /"},
                        }
                    ],
                }
            },
        }

        written_messages: list[dict] = []

        async def _capture_write(msg):
            written_messages.append(msg)

        with patch.object(channel, "_write_jsonrpc", _capture_write):
            bridge_task = asyncio.create_task(
                channel._bridge_permission_request(session_id, loop_id, event)
            )
            await asyncio.sleep(0.1)

            req_id = written_messages[0]["id"]
            fut = channel._get_state().pending_permissions[req_id]
            fut.set_result({"outcome": "selected", "optionId": "reject_once"})

            await asyncio.wait_for(bridge_task, timeout=5.0)

        resume_msg = channel._manager._event_bus.publish.call_args.args[1]
        assert resume_msg["resume_payload"]["int-002"]["decisions"][0]["type"] == "reject"

        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_permission_timeout_routes_reject(self):
        """Test that a timeout routes a reject decision."""
        channel, session_id, loop_id = self._make_channel_with_session()

        event = {
            "type": "event",
            "loop_id": loop_id,
            "data": {
                "__interrupt__": {
                    "interrupt_id": "int-003",
                    "action_requests": [
                        {"tool_call_id": "tc-3", "tool_name": "delete", "args": {}}
                    ],
                }
            },
        }

        written_messages: list[dict] = []

        async def _capture_write(msg):
            written_messages.append(msg)

        # Use a very short timeout to make the test fast
        with (
            patch.object(channel, "_write_jsonrpc", _capture_write),
            patch("soothe_daemon.channels.acp._PERMISSION_TIMEOUT_S", 0.2),
        ):
            bridge_task = asyncio.create_task(
                channel._bridge_permission_request(session_id, loop_id, event)
            )
            await asyncio.wait_for(bridge_task, timeout=5.0)

        # Verify reject was routed
        resume_msg = channel._manager._event_bus.publish.call_args.args[1]
        assert resume_msg["resume_payload"]["int-003"]["decisions"][0]["type"] == "reject"

        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_handle_response_resolves_pending_future(self):
        """Test that _handle_response resolves a pending permission future."""
        channel, session_id, loop_id = self._make_channel_with_session()

        # Create a pending permission
        req_id = 42
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        channel._get_state().pending_permissions[req_id] = fut

        # Simulate a response from the ACP client
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"outcome": "selected", "optionId": "allow_always"},
        }
        await channel._handle_response(response)

        assert fut.done()
        result = fut.result()
        assert result["outcome"] == "selected"
        assert result["optionId"] == "allow_always"
        assert req_id not in channel._get_state().pending_permissions

    @pytest.mark.asyncio
    async def test_handle_response_with_error_resolves_cancelled(self):
        """Test that an error response resolves as cancelled."""
        channel, _, _ = self._make_channel_with_session()

        req_id = 99
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        channel._get_state().pending_permissions[req_id] = fut

        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -1, "message": "denied"},
        }
        await channel._handle_response(response)

        assert fut.done()
        assert fut.result()["outcome"] == "cancelled"

    @pytest.mark.asyncio
    async def test_handle_response_unknown_id(self):
        """Test that a response with unknown request id is ignored."""
        channel, _, _ = self._make_channel_with_session()

        response = {"jsonrpc": "2.0", "id": 999, "result": {}}
        # Should not raise
        await channel._handle_response(response)
        assert len(channel._get_state().pending_permissions) == 0

    @pytest.mark.asyncio
    async def test_dispatch_request_routes_response(self):
        """Test that _dispatch_request routes a response (no method) to _handle_response."""
        channel, _, _ = self._make_channel_with_session()

        req_id = 55
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        channel._get_state().pending_permissions[req_id] = fut

        # A response has no "method" key but has "id"
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"outcome": "selected", "optionId": "allow_once"},
        }
        await channel._dispatch_request(response)

        assert fut.done()
        assert fut.result()["optionId"] == "allow_once"

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_permissions(self):
        """Test that stop() cancels pending permission futures."""
        config = ACPConfig(enabled=True)
        manager = MagicMock()
        manager._event_bus = MagicMock()

        channel = ACPChannel(config, manager)
        channel._running = True

        req_id = 77
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        channel._get_state().pending_permissions[req_id] = fut

        await channel.stop()

        assert fut.cancelled() or fut.done()
        assert len(channel._get_state().pending_permissions) == 0


def _plan_frame(event_type: str, **payload: object) -> dict:
    """Build the `mode="custom"` wire frame a cognition event arrives in."""
    return {
        "type": "event",
        "namespace": [],
        "mode": "custom",
        "data": {"type": event_type, **payload},
    }


class TestPlanProjection:
    """Soothe strange-loop steps -> ACP `plan` entries.

    ACP accepts exactly `pending | in_progress | completed` and requires
    `priority`; the client SDK silently DROPS an entry that falls outside
    those enums. These tests pin the whitelist and the `_meta` fallbacks.
    """

    # ACP `PlanEntryStatus` / `PlanEntryPriority`, mirrored from the protocol.
    ACP_STATUSES = frozenset({"pending", "in_progress", "completed"})
    ACP_PRIORITIES = frozenset({"high", "medium", "low"})

    def _assert_frame_is_legal(self, update: dict) -> None:
        """Every emitted entry must survive ACP schema validation."""
        assert update["sessionUpdate"] == "plan"
        for entry in update["entries"]:
            assert entry["status"] in self.ACP_STATUSES, entry
            assert entry["priority"] in self.ACP_PRIORITIES, entry
            assert isinstance(entry["content"], str) and entry["content"], entry
            assert "soothe" in entry["_meta"]
        assert "soothe" in update["_meta"]

    def test_plan_decision_creates_entries(self):
        state = _PlanState()
        state.sync_plan(
            [
                {"id": "S1", "description": "Scope"},
                {"id": "S2", "description": "Inspect", "dependencies": ["S1"]},
            ],
            0,
        )

        update = state.update()

        assert update is not None
        self._assert_frame_is_legal(update)
        assert [e["content"] for e in update["entries"]] == ["Scope", "Inspect"]
        assert [e["status"] for e in update["entries"]] == ["pending", "pending"]
        assert update["entries"][1]["_meta"]["soothe"]["depends_on"] == ["S1"]

    def test_settled_steps_survive_a_later_replan(self):
        """The plan is cumulative across iterations (mirrors the TUI)."""
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "One"}], 0)
        state.settle("S1", True)
        state.sync_plan([{"id": "S2", "description": "Two"}], 1)

        update = state.update()

        assert update is not None
        assert [e["content"] for e in update["entries"]] == ["One", "Two"]

    def test_replan_drops_a_never_started_pending_step(self):
        state = _PlanState()
        state.sync_plan(
            [{"id": "S1", "description": "Keep"}, {"id": "S2", "description": "Drop"}],
            0,
        )
        state.sync_plan([{"id": "S1", "description": "Keep"}], 1)

        update = state.update()

        assert update is not None
        assert [e["content"] for e in update["entries"]] == ["Keep"]

    def test_failed_step_keeps_its_entry_and_carries_outcome(self):
        """ACP has no failed status, so the truth rides in `_meta`."""
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Boom"}], 0)
        state.settle("S1", False, summary="kaboom", duration_ms=12, tool_call_count=2)

        entry = state.update()["entries"][0]

        assert entry["status"] == "completed"
        assert entry["_meta"]["soothe"]["outcome"] == "error"
        assert entry["_meta"]["soothe"]["summary"] == "kaboom"
        assert entry["_meta"]["soothe"]["duration_ms"] == 12
        assert entry["_meta"]["soothe"]["tool_call_count"] == 2

    def test_successful_step_carries_ok_outcome(self):
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Fine"}], 0)
        state.settle("S1", True, summary="done")

        meta = state.update()["entries"][0]["_meta"]["soothe"]

        assert meta["outcome"] == "ok"

    def test_unknown_phase_still_emits_a_legal_status(self):
        """An unmapped phase must never reach the wire verbatim."""
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Future"}], 0)
        state.mark("S1", "some_future_phase")

        update = state.update()

        self._assert_frame_is_legal(update)
        entry = update["entries"][0]
        assert entry["status"] == "pending"
        assert entry["_meta"]["soothe"]["phase"] == "some_future_phase"

    def test_settled_step_does_not_regress(self):
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Done"}], 0)
        state.settle("S1", True)
        state.mark("S1", "running")

        assert state.update()["entries"][0]["status"] == "completed"

    def test_step_completed_preserves_the_description(self):
        """`step.completed` carries no description, so it must not clobber it."""
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Original"}], 0)
        state.settle("S1", True, summary="x")

        assert state.update()["entries"][0]["content"] == "Original"

    def test_empty_plan_sends_nothing(self):
        """An empty `entries` list reads as "clear the plan" to clients."""
        assert _PlanState().update() is None

    def test_close_open_steps_settles_them_as_errors(self):
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Open"}], 0)
        state.mark("S1", "running")
        state.close_open_steps()

        entry = state.update()["entries"][0]

        assert entry["status"] == "completed"
        assert entry["_meta"]["soothe"]["outcome"] == "error"

    def test_summary_is_bounded(self):
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "Long"}], 0)
        state.settle("S1", False, summary="x" * 5000)

        assert len(state.update()["entries"][0]["_meta"]["soothe"]["summary"]) <= 512


class TestPlanProjectionFrames:
    """The channel folds cognition frames and emits one full plan per change."""

    def _make_channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), MagicMock())

    def test_non_plan_frames_are_not_consumed(self):
        channel = self._make_channel()

        assert channel._apply_plan_event(_STDIO_SENTINEL, "loop-1", _plan_frame("other")) is None
        assert channel._apply_plan_event(_STDIO_SENTINEL, "loop-1", {"mode": "messages"}) is None

    def test_frame_sequence_produces_full_plan_each_time(self):
        channel = self._make_channel()
        loop_id = "loop-1"

        first = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.plan.decision",
                iteration=0,
                steps=[
                    {"id": "S1", "description": "One"},
                    {"id": "S2", "description": "Two"},
                ],
                total_steps=2,
                done_steps=0,
            ),
        )
        assert first is not None
        assert [e["status"] for e in first["entries"]] == ["pending", "pending"]
        assert first["_meta"]["soothe"] == {"iteration": 0, "total_steps": 2, "done_steps": 0}

        started = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.started", step_id="S1", description="One"
            ),
        )
        # Every update carries the COMPLETE list, never just the changed step.
        assert len(started["entries"]) == 2
        assert started["entries"][0]["status"] == "in_progress"
        assert started["entries"][1]["status"] == "pending"

        completed = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.completed",
                step_id="S1",
                success=True,
                summary="done",
                duration_ms=5,
                tool_call_count=1,
                total_tokens_used=7,
            ),
        )
        assert completed["entries"][0]["status"] == "completed"
        assert completed["entries"][0]["_meta"]["soothe"]["outcome"] == "ok"
        assert completed["entries"][0]["content"] == "One"
        assert len(completed["entries"]) == 2

    def test_queued_step_maps_to_pending_with_phase_kept(self):
        channel = self._make_channel()
        loop_id = "loop-2"
        channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.plan.decision",
                steps=[{"id": "S1", "description": "Wait"}],
            ),
        )

        update = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.queued", step_id="S1", description="Wait"
            ),
        )

        assert update["entries"][0]["status"] == "pending"
        assert update["entries"][0]["_meta"]["soothe"]["phase"] == "queued"

    def test_loop_completed_converges_open_steps(self):
        channel = self._make_channel()
        loop_id = "loop-3"
        channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.plan.decision",
                steps=[{"id": "S1", "description": "Stuck"}],
            ),
        )

        update = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame("soothe.cognition.strange_loop.completed", status="done"),
        )

        assert update["entries"][0]["status"] == "completed"
        assert update["entries"][0]["_meta"]["soothe"]["outcome"] == "error"

    def test_loop_completed_without_a_plan_sends_nothing(self):
        channel = self._make_channel()

        update = channel._apply_plan_event(
            _STDIO_SENTINEL,
            "loop-4",
            _plan_frame("soothe.cognition.strange_loop.completed", status="done"),
        )

        assert update is None

    def test_plan_state_is_isolated_per_loop(self):
        channel = self._make_channel()
        channel._apply_plan_event(
            _STDIO_SENTINEL,
            "loop-a",
            _plan_frame(
                "soothe.cognition.strange_loop.plan.decision",
                steps=[{"id": "A", "description": "A"}],
            ),
        )
        channel._apply_plan_event(
            _STDIO_SENTINEL,
            "loop-b",
            _plan_frame(
                "soothe.cognition.strange_loop.plan.decision",
                steps=[{"id": "B", "description": "B"}],
            ),
        )

        assert [e["content"] for e in channel._plan_state(_STDIO_SENTINEL, "loop-a").entries()] == [
            "A"
        ]
        assert [e["content"] for e in channel._plan_state(_STDIO_SENTINEL, "loop-b").entries()] == [
            "B"
        ]

    @pytest.mark.asyncio
    async def test_send_plan_update_emits_one_notification(self):
        channel = self._make_channel()
        writes: list[dict] = []
        update = {"sessionUpdate": "plan", "entries": [], "_meta": {"soothe": {}}}

        with patch.object(channel, "_write_jsonrpc", AsyncMock(side_effect=writes.append)):
            await channel._send_plan_update("sess-9", update)

        assert writes == [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": "sess-9", "update": update},
            }
        ]


class TestPlanProgressCounters:
    """`total_steps`/`done_steps` are derived, not passed through.

    A live run showed the source's `plan.decision` counters going stale the
    moment a step settled (`done_steps` stayed 0 after the step finished), and
    the counters describe a different population than the client-visible
    `entries`. Both must therefore be computed from the plan being sent.
    """

    def test_done_steps_counts_settled_entries(self):
        state = _PlanState()
        state.sync_plan(
            [
                {"id": "S1", "description": "One"},
                {"id": "S2", "description": "Two"},
                {"id": "S3", "description": "Three"},
            ],
            0,
        )
        assert state.update()["_meta"]["soothe"]["done_steps"] == 0

        state.settle("S1", True)

        meta = state.update()["_meta"]["soothe"]
        assert meta["done_steps"] == 1
        assert meta["total_steps"] == 3

    def test_failed_step_still_counts_as_done(self):
        """`done` tracks "reached a result", matching the source's step_results."""
        state = _PlanState()
        state.sync_plan([{"id": "S1", "description": "One"}], 0)
        state.settle("S1", False)

        assert state.update()["_meta"]["soothe"]["done_steps"] == 1

    def test_total_steps_matches_the_emitted_entries(self):
        """A replan that drops a pending step must shrink the total too."""
        state = _PlanState()
        state.sync_plan(
            [{"id": "S1", "description": "Keep"}, {"id": "S2", "description": "Drop"}],
            0,
        )
        state.settle("S1", True)
        state.sync_plan([{"id": "S1", "description": "Keep"}], 1)

        update = state.update()
        assert update["_meta"]["soothe"]["total_steps"] == len(update["entries"]) == 1
        assert update["_meta"]["soothe"]["done_steps"] == 1


class TestPlanFrameDeduplication:
    """Distinct source events can serialize to the same plan.

    Observed live: `step.completed` settled the last step, then
    `strange_loop.completed` converged nothing and re-sent a byte-identical
    frame. Unchanged full plans must not reach the wire.
    """

    def _channel_with_one_step(self) -> tuple[ACPChannel, str]:
        channel = ACPChannel(ACPConfig(enabled=True), MagicMock())
        loop_id = "loop-dedup"
        channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.plan.decision",
                steps=[{"id": "S1", "description": "Only"}],
            ),
        )
        return channel, loop_id

    def test_identical_followup_frame_is_suppressed(self):
        channel, loop_id = self._channel_with_one_step()
        settled = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.completed",
                step_id="S1",
                success=True,
                summary="done",
            ),
        )
        assert settled is not None

        # `strange_loop.completed` has nothing left to converge.
        again = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame("soothe.cognition.strange_loop.completed", status="done"),
        )

        assert again is None

    def test_a_real_change_still_emits(self):
        channel, loop_id = self._channel_with_one_step()

        started = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.started",
                step_id="S1",
                description="Only",
            ),
        )

        assert started is not None
        assert started["entries"][0]["status"] == "in_progress"

    def test_a_repeat_of_an_old_state_is_not_suppressed_after_a_change(self):
        """Dedupe compares against the last SENT frame, not a history."""
        channel, loop_id = self._channel_with_one_step()

        first = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.started",
                step_id="S1",
                description="Only",
            ),
        )
        assert first is not None
        # Same frame again -> suppressed.
        duplicate = channel._apply_plan_event(
            _STDIO_SENTINEL,
            loop_id,
            _plan_frame(
                "soothe.cognition.strange_loop.step.started",
                step_id="S1",
                description="Only",
            ),
        )
        assert duplicate is None
        # A settled step changes the frame, so it must emit.
        assert (
            channel._apply_plan_event(
                _STDIO_SENTINEL,
                loop_id,
                _plan_frame(
                    "soothe.cognition.strange_loop.step.completed",
                    step_id="S1",
                    success=True,
                ),
            )
            is not None
        )
