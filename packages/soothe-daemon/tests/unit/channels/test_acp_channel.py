"""Unit tests for ACPChannel."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soothe_daemon.channels.acp import (
    _ACP_DEFAULT_MODE,
    _ACP_MODE_ORDER,
    _STDIO_SENTINEL,
    ACPChannel,
    _acp_mode_config_option,
    _acp_mode_state,
    _clarification_answers_from_response,
    _clarification_elicitation_params,
    _clarification_turn_text,
    _client_supports_form_elicitation,
    _interaction_mode_for_turn,
    _iter_wire_frames,
    _make_text_block,
    _PlanState,
    _session_update_from_block,
    _tool_kind,
    _ToolProjection,
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
    # session/load and session/resume publish through handle_inbound, so the
    # stub has to be awaitable like the real method.
    manager.handle_inbound = AsyncMock(return_value=loop_id)
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
    async def test_session_load_registers_loop_with_cwd(self):
        """Reopening a session must restore its workspace, not lose it.

        session/load creates the loop when the session is not in this
        connection's map; if the cwd is not attached here the first prompt
        registers the loop with the daemon workspace instead.
        """
        config = ACPConfig(enabled=True)
        manager = make_manager(loop_id="acp:loaded")

        channel = ACPChannel(config, manager)
        result = await channel._handle_session_load(
            {"sessionId": "sess-loaded", "cwd": "/tmp/loaded"}
        )

        assert "configOptions" in result
        manager.ensure_loop_registered.assert_awaited_once_with(
            "acp:loaded", workspace="/tmp/loaded"
        )

        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_session_resume_registers_loop_with_cwd(self):
        """A resumed session carries the cwd the client reopened it with."""
        config = ACPConfig(enabled=True)
        manager = make_manager(loop_id="acp:resumed")

        channel = ACPChannel(config, manager)
        # session/resume requires the session to already be known here.
        channel._get_state().session_map["sess-resumed"] = "acp:resumed"

        await channel._handle_session_resume({"sessionId": "sess-resumed", "cwd": "/tmp/resumed"})

        manager.ensure_loop_registered.assert_awaited_once_with(
            "acp:resumed", workspace="/tmp/resumed"
        )

        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_session_fork_registers_the_new_loop_with_cwd(self):
        """The forked loop is new, so the inherited cwd is attached here."""
        config = ACPConfig(enabled=True)
        manager = make_manager(loop_id="acp:forked")

        channel = ACPChannel(config, manager)
        channel._get_state().session_map["sess-parent"] = "acp:parent"

        result = await channel._handle_session_fork(
            {"sessionId": "sess-parent", "cwd": "/tmp/forked"}
        )

        assert "sessionId" in result
        manager.ensure_loop_registered.assert_awaited_once_with(
            "acp:forked", workspace="/tmp/forked"
        )

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
                "acp:test-session",
                "Hello",
                channel="acp",
                chat_id=session_id,
                # No mode was chosen, so the default travels as None and the
                # turn payload is unchanged from before modes were exposed.
                interaction_mode=None,
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

        blocks = channel._translate_event(messages_frame("Hello world", phase="goal_completion"))

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


def _tool_batch_frame(rows: list[dict]) -> dict:
    """The `mode="custom"` frame the coalescer batches tool invocations into."""
    return {
        "type": "event",
        "namespace": [],
        "mode": "custom",
        "data": {"type": "tool_call_updates_batch", "updates": rows, "count": len(rows)},
    }


def _tool_row(tool_call_id: str, name: str, args: dict) -> dict:
    """One row inside a tool batch, as `tool_call_update_event` builds it."""
    return {
        "type": "soothe.stream.tool_call.update",
        "tool_call_id": tool_call_id,
        "name": name,
        "args": args,
    }


def _tool_result_frame(tool_call_id: str, content: str, status: str = "success") -> dict:
    """The `mode="messages"` frame a tool *result* arrives in.

    This is the only daemon-local signal that a call finished — the invocation
    frame carries no status — so it is what closes a row.
    """
    return {
        "type": "event",
        "namespace": [],
        "mode": "messages",
        "data": (
            {"type": "tool", "content": content, "tool_call_id": tool_call_id, "status": status},
            {},
        ),
    }


def _subagent_frame(event_type: str, **payload: object) -> dict:
    return _plan_frame(event_type, **payload)


class TestToolKindMapping:
    """`kind` is derived from the shape of the args, never the tool's name.

    Inferring semantics from a command name is forbidden by the turn-lifecycle
    contract (invariant I10), and it breaks the moment a tool is renamed or
    supplied by a plugin.
    """

    CASES = [
        ({"command": "ls -la"}, "execute"),
        ({"path": "/a.py", "old_string": "x", "new_string": "y"}, "edit"),
        ({"path": "/a.py", "new_content": "z"}, "edit"),
        ({"query": "needle"}, "search"),
        ({"pattern": "re"}, "search"),
        ({"path": "/a.py"}, "read"),
        ({"url": "https://example.com"}, "fetch"),
        ({"todos": [{"content": "a"}]}, "think"),
        ({}, "other"),
        ({"unrecognised": 1}, "other"),
    ]

    def test_argument_shape_selects_the_kind(self):
        for args, expected in self.CASES:
            assert _tool_kind(args) == expected, args

    def test_kind_ignores_the_tool_name(self):
        """Two tools with different names but the same args classify alike."""
        args = {"command": "echo hi"}
        assert _tool_kind(dict(args)) == _tool_kind(dict(args)) == "execute"

    def test_empty_values_do_not_select_a_kind(self):
        # A streamed-in arg can be present but blank; that is not evidence.
        assert _tool_kind({"command": "", "path": None}) == "other"


class TestToolCallProjection:
    """Invocation frames open a row exactly once; later facts patch it."""

    def test_first_invocation_opens_the_row(self):
        projection = _ToolProjection()
        frames = projection.note_invocation("c1", "run_command", {"command": "pwd"})

        assert len(frames) == 1
        update = frames[0]
        assert update["sessionUpdate"] == "tool_call"
        assert update["toolCallId"] == "c1"
        assert update["title"] == "run_command: pwd"
        assert update["kind"] == "execute"
        assert update["status"] == "in_progress"
        assert update["rawInput"] == {"command": "pwd"}

    def test_repeated_identical_invocation_is_deduped(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})

        assert projection.note_invocation("c1", "run_command", {"command": "pwd"}) == []

    def test_refined_args_emit_a_patch_not_a_second_row(self):
        """`tool_call_update` is the only legal way to amend an open row."""
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})

        frames = projection.note_invocation("c1", "run_command", {"command": "pwd -P"})

        assert len(frames) == 1
        assert frames[0]["sessionUpdate"] == "tool_call_update"
        assert frames[0]["rawInput"] == {"command": "pwd -P"}
        assert frames[0]["title"] == "run_command: pwd -P"

    def test_blank_id_is_ignored(self):
        projection = _ToolProjection()
        assert projection.note_invocation("", "run_command", {"command": "pwd"}) == []

    def test_args_after_completion_do_not_reopen_the_row(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})
        projection.note_completion("c1", "out")

        assert projection.note_invocation("c1", "run_command", {"command": "other"}) == []

    def test_locations_are_attached_for_file_tools(self):
        projection = _ToolProjection()
        frames = projection.note_invocation("c1", "read_file", {"path": "/a.py"})

        assert frames[0]["locations"] == [{"path": "/a.py"}]
        assert frames[0]["kind"] == "read"


class TestToolCallCompletion:
    """A completion closes the row with the result, or a diff for edits."""

    def test_result_becomes_content_and_raw_output(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})

        frames = projection.note_completion("c1", "  /tmp  ")

        assert len(frames) == 1
        update = frames[0]
        assert update["sessionUpdate"] == "tool_call_update"
        assert update["status"] == "completed"
        assert update["content"] == [
            {"type": "content", "content": {"type": "text", "text": "/tmp"}}
        ]
        assert update["rawOutput"] == "/tmp"

    def test_error_status_marks_the_row_failed(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "false"})

        update = projection.note_completion("c1", "boom", error=True)[0]

        assert update["status"] == "failed"
        assert update["_meta"]["soothe"]["outcome"] == "error"

    def test_edit_produces_a_diff_block(self):
        projection = _ToolProjection()
        projection.note_invocation(
            "c1", "edit_file", {"path": "/a.py", "old_string": "old", "new_string": "new"}
        )

        update = projection.note_completion("c1", "updated")[0]

        diff = update["content"][0]
        assert diff == {"type": "diff", "path": "/a.py", "oldText": "old", "newText": "new"}
        # The tool's own message rides along after the diff.
        assert update["content"][1]["type"] == "content"

    def test_write_has_a_null_old_text(self):
        """`oldText: null` is the protocol's "new file", not "unchanged"."""
        projection = _ToolProjection()
        projection.note_invocation("c1", "write_file", {"path": "/b.py", "new_content": "hi"})

        diff = projection.note_completion("c1", "wrote")[0]["content"][0]

        assert diff["oldText"] is None
        assert diff["newText"] == "hi"

    def test_completion_for_an_unannounced_call_is_dropped(self):
        """A client cannot patch a row it never received."""
        projection = _ToolProjection()

        assert projection.note_completion("never-seen", "out") == []

    def test_second_completion_is_ignored(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})
        projection.note_completion("c1", "first")

        assert projection.note_completion("c1", "second") == []

    def test_empty_output_still_closes_the_row(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "write_file", {"path": "/b.py", "new_content": "hi"})

        update = projection.note_completion("c1", "")[0]

        assert update["status"] == "completed"
        assert "rawOutput" not in update


class TestToolSweep:
    """End of turn: no row may be left spinning.

    The coalescer drops a tool result that carries no text, so some calls never
    receive a completion frame at all — the sweep is what stops those rows from
    hanging forever.
    """

    def test_turn_end_closes_open_rows_as_completed(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})

        frames = projection.sweep()

        assert len(frames) == 1
        assert frames[0]["status"] == "completed"

    def test_cancelled_turn_closes_rows_as_failed_with_an_outcome(self):
        """ACP has no cancelled tool status, so the truth rides in `_meta`."""
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "sleep 100"})

        frames = projection.sweep(cancelled=True)

        assert frames[0]["status"] == "failed"
        assert frames[0]["_meta"]["soothe"]["outcome"] == "cancelled"

    def test_settled_rows_are_not_swept_again(self):
        projection = _ToolProjection()
        projection.note_invocation("c1", "run_command", {"command": "pwd"})
        projection.note_completion("c1", "out")

        assert projection.sweep() == []

    def test_sweep_never_invents_a_row(self):
        projection = _ToolProjection()

        assert projection.sweep() == []


class TestSubagentProjection:
    """Subagent lifecycle rides the tool-call channel.

    ACP has no subagent construct, and a client cannot patch a row it never
    received, so a terminal-only frame must still be preceded by `tool_call`.
    """

    STARTED = "soothe.cognition.wired_subagent.started"
    COMPLETED = "soothe.cognition.wired_subagent.completed"
    FAILED = "soothe.cognition.wired_subagent.failed"
    CANCELLED = "soothe.cognition.wired_subagent.cancelled"

    def test_started_opens_a_row_carrying_identity(self):
        projection = _ToolProjection()

        frames = projection.note_subagent(
            self.STARTED,
            {
                "subagent": "researcher",
                "invocation_id": "inv1",
                "step_id": "S1",
                "description": "dig",
            },
        )

        assert len(frames) == 1
        update = frames[0]
        assert update["sessionUpdate"] == "tool_call"
        assert update["toolCallId"] == "inv1"
        assert update["kind"] == "other"
        assert update["title"] == "dig"
        assert update["_meta"]["soothe"]["subagent"] == {
            "id": "inv1",
            "name": "researcher",
            "step_id": "S1",
        }

    def test_terminal_frame_keeps_the_identity_from_the_start(self):
        """The terminal event carries no name; rebuilding it would lose it."""
        projection = _ToolProjection()
        projection.note_subagent(
            self.STARTED, {"subagent": "researcher", "invocation_id": "inv1", "step_id": "S1"}
        )

        update = projection.note_subagent(
            self.COMPLETED, {"invocation_id": "inv1", "summary": "found it", "duration_ms": 1234}
        )[0]

        assert update["status"] == "completed"
        subagent = update["_meta"]["soothe"]["subagent"]
        assert subagent["name"] == "researcher", "identity must survive the terminal frame"
        assert subagent["step_id"] == "S1"
        assert update["_meta"]["soothe"]["outcome"] == "ok"
        assert update["_meta"]["soothe"]["duration_ms"] == 1234

    def test_terminal_only_frame_opens_then_closes(self):
        projection = _ToolProjection()

        frames = projection.note_subagent(
            self.FAILED,
            {"subagent": "coder", "invocation_id": "inv2", "step_id": "S2", "description": "write"},
        )

        assert [f["sessionUpdate"] for f in frames] == ["tool_call", "tool_call_update"]
        assert frames[1]["status"] == "failed"

    def test_cancelled_lands_on_failed_with_the_truth_in_meta(self):
        projection = _ToolProjection()
        projection.note_subagent(
            self.STARTED, {"subagent": "researcher", "invocation_id": "inv1", "step_id": "S1"}
        )

        update = projection.note_subagent(self.CANCELLED, {"invocation_id": "inv1"})[0]

        assert update["status"] == "failed"
        assert update["_meta"]["soothe"]["outcome"] == "cancelled"

    def test_unnamed_terminal_frame_is_ignored(self):
        projection = _ToolProjection()

        assert projection.note_subagent(self.FAILED, {"invocation_id": "inv3"}) == []

    def test_missing_invocation_id_is_ignored(self):
        projection = _ToolProjection()

        assert projection.note_subagent(self.STARTED, {"subagent": "researcher"}) == []

    def test_replayed_start_after_settle_is_ignored(self):
        projection = _ToolProjection()
        projection.note_subagent(
            self.STARTED, {"subagent": "researcher", "invocation_id": "inv1", "step_id": "S1"}
        )
        projection.note_subagent(self.COMPLETED, {"invocation_id": "inv1"})

        assert (
            projection.note_subagent(
                self.STARTED, {"subagent": "researcher", "invocation_id": "inv1"}
            )
            == []
        )


class TestToolEventFrames:
    """The channel routes tool/subagent frames and ignores everything else."""

    def _make_channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), MagicMock())

    def test_batch_frame_opens_one_row_per_member(self):
        channel = self._make_channel()

        frames = channel._apply_tool_event(
            _STDIO_SENTINEL,
            "loop-1",
            _tool_batch_frame(
                [
                    _tool_row("c1", "run_command", {"command": "pwd"}),
                    _tool_row("c2", "read_file", {"path": "/a.py"}),
                ]
            ),
        )

        assert [f["toolCallId"] for f in frames] == ["c1", "c2"]
        assert all(f["sessionUpdate"] == "tool_call" for f in frames)

    def test_single_stream_update_frame_is_treated_as_one_row(self):
        channel = self._make_channel()

        frames = channel._apply_tool_event(
            _STDIO_SENTINEL,
            "loop-1",
            _plan_frame(
                "soothe.stream.tool_call.update",
                tool_call_id="c1",
                name="run_command",
                args={"command": "pwd"},
            ),
        )

        assert len(frames) == 1
        assert frames[0]["sessionUpdate"] == "tool_call"

    def test_tool_result_frame_closes_the_row(self):
        channel = self._make_channel()
        channel._apply_tool_event(
            _STDIO_SENTINEL,
            "loop-1",
            _tool_batch_frame([_tool_row("c1", "run_command", {"command": "pwd"})]),
        )

        frames = channel._apply_tool_event(
            _STDIO_SENTINEL, "loop-1", _tool_result_frame("c1", "/tmp")
        )

        assert len(frames) == 1
        assert frames[0]["status"] == "completed"

    def test_error_result_marks_the_row_failed(self):
        channel = self._make_channel()
        channel._apply_tool_event(
            _STDIO_SENTINEL,
            "loop-1",
            _tool_batch_frame([_tool_row("c1", "run_command", {"command": "x"})]),
        )

        frames = channel._apply_tool_event(
            _STDIO_SENTINEL, "loop-1", _tool_result_frame("c1", "boom", status="error")
        )

        assert frames[0]["status"] == "failed"

    def test_assistant_message_frame_is_not_a_tool_result(self):
        """An AI frame carries `tool_calls`; only a result carries the id."""
        channel = self._make_channel()
        frame = {
            "type": "event",
            "namespace": [],
            "mode": "messages",
            "data": ({"type": "ai", "content": "hello", "tool_calls": [{"id": "c1"}]}, {}),
        }

        assert channel._apply_tool_event(_STDIO_SENTINEL, "loop-1", frame) == []

    def test_subagent_frame_is_routed(self):
        channel = self._make_channel()

        frames = channel._apply_tool_event(
            _STDIO_SENTINEL,
            "loop-1",
            _subagent_frame(
                "soothe.cognition.wired_subagent.started",
                subagent="researcher",
                invocation_id="inv1",
                step_id="S1",
            ),
        )

        assert len(frames) == 1
        assert frames[0]["_meta"]["soothe"]["subagent"]["name"] == "researcher"

    def test_unrelated_frames_are_not_consumed(self):
        channel = self._make_channel()

        assert channel._apply_tool_event(_STDIO_SENTINEL, "loop-1", _plan_frame("other")) == []
        assert (
            channel._apply_tool_event(
                _STDIO_SENTINEL, "loop-1", {"mode": "messages", "data": "not-a-pair"}
            )
            == []
        )

    def test_projection_is_scoped_per_loop(self):
        """Two loops on one connection must not share tool rows."""
        channel = self._make_channel()
        channel._apply_tool_event(
            _STDIO_SENTINEL,
            "loop-a",
            _tool_batch_frame([_tool_row("c1", "run_command", {"command": "pwd"})]),
        )

        # loop-b never saw c1, so it cannot patch it.
        assert (
            channel._apply_tool_event(_STDIO_SENTINEL, "loop-b", _tool_result_frame("c1", "out"))
            == []
        )


class TestPlanCreatedProjection:
    """`plan.created` is the plan's first appearance and uses `step_id`."""

    def _make_channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), MagicMock())

    def test_plan_created_emits_entries(self):
        channel = self._make_channel()

        update = channel._apply_plan_event(
            _STDIO_SENTINEL,
            "loop-1",
            _plan_frame(
                "soothe.cognition.plan.created",
                plan_id="p1",
                goal="ship it",
                steps=[
                    {"step_id": "S1", "description": "Scope"},
                    {"step_id": "S2", "description": "Build"},
                ],
            ),
        )

        assert update is not None
        assert [e["content"] for e in update["entries"]] == ["Scope", "Build"]
        assert [e["_meta"]["soothe"]["step_id"] for e in update["entries"]] == ["S1", "S2"]

    def test_step_id_key_is_accepted_like_id(self):
        """`plan.decision` spells the field `id`; `plan.created` spells it `step_id`."""
        channel = self._make_channel()

        update = channel._apply_plan_event(
            _STDIO_SENTINEL,
            "loop-1",
            _plan_frame(
                "soothe.cognition.plan.created",
                steps=[{"id": "A", "description": "From id"}],
            ),
        )

        assert [e["_meta"]["soothe"]["step_id"] for e in update["entries"]] == ["A"]

    def test_plan_created_without_steps_emits_nothing(self):
        """An empty entry list would clear the client's plan."""
        channel = self._make_channel()

        assert (
            channel._apply_plan_event(
                _STDIO_SENTINEL, "loop-1", _plan_frame("soothe.cognition.plan.created", steps=[])
            )
            is None
        )


class TestAcpModeTable:
    """One table drives both the legacy `modes` field and the config option.

    ACP is retiring `modes` in favour of a `category: "mode"` config option, and
    asks agents to publish both while the old field goes away. They must not
    drift, so both shapes are generated from `_ACP_MODE_ORDER`.
    """

    def test_both_shapes_advertise_the_same_ids_in_the_same_order(self):
        for mode in _ACP_MODE_ORDER:
            legacy = [m["id"] for m in _acp_mode_state(mode)["availableModes"]]
            modern = [o["value"] for o in _acp_mode_config_option(mode)["options"]]
            assert legacy == modern == list(_ACP_MODE_ORDER)

    def test_the_four_soothe_modes_are_exposed(self):
        assert set(_ACP_MODE_ORDER) == {"agent", "bypass", "plan", "ask"}

    def test_every_mode_carries_a_label_and_a_description(self):
        for mode in _ACP_MODE_ORDER:
            entry = next(m for m in _acp_mode_state(mode)["availableModes"] if m["id"] == mode)
            assert entry["name"]
            assert entry["description"]

    def test_config_option_matches_the_client_lookup_contract(self):
        """Backchat finds a mode picker by `category: "mode"` (id `mode` also works)."""
        option = _acp_mode_config_option(_ACP_DEFAULT_MODE)

        assert option["id"] == "mode"
        assert option["category"] == "mode"
        assert option["type"] == "select"
        assert option["currentValue"] == _ACP_DEFAULT_MODE

    def test_both_shapes_report_the_requested_current_mode(self):
        assert _acp_mode_state("plan")["currentModeId"] == "plan"
        assert _acp_mode_config_option("ask")["currentValue"] == "ask"

    def test_unknown_current_mode_falls_back_to_the_default(self):
        """A corrupt id must not be echoed back as if it were selectable."""
        assert _acp_mode_state("nope")["currentModeId"] == _ACP_DEFAULT_MODE
        assert _acp_mode_config_option(None)["currentValue"] == _ACP_DEFAULT_MODE


class TestAcpModeToRunnerMapping:
    """How an ACP mode id becomes the runner's `interaction_mode`."""

    def test_default_mode_sends_no_interaction_mode(self):
        """Auto is spelled as *absent*, so untouched sessions keep old behaviour."""
        assert _interaction_mode_for_turn(_ACP_DEFAULT_MODE) is None

    def test_other_modes_are_forwarded_verbatim(self):
        for mode in ("bypass", "plan", "ask"):
            assert _interaction_mode_for_turn(mode) == mode

    def test_unknown_and_empty_map_to_the_default(self):
        for value in ("", None, "nope"):
            assert _interaction_mode_for_turn(value) is None


class TestAcpModeLifecycle:
    """Every session-lifecycle response advertises the real mode set."""

    def _make_channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), make_manager())

    def _cancel_consumers(self, channel: ACPChannel) -> None:
        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    def _assert_advertises_modes(self, result: dict, expected_mode: str) -> None:
        assert [m["id"] for m in result["modes"]["availableModes"]] == list(_ACP_MODE_ORDER)
        assert result["modes"]["currentModeId"] == expected_mode
        option = result["configOptions"][0]
        assert option["category"] == "mode"
        assert option["currentValue"] == expected_mode

    @pytest.mark.asyncio
    async def test_session_new_advertises_the_modes(self):
        channel = self._make_channel()
        try:
            result = await channel._handle_session_new({})
            self._assert_advertises_modes(result, _ACP_DEFAULT_MODE)
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_session_load_reports_the_mode_already_chosen(self):
        """Reopening must not silently reset the picker to Auto."""
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]
            await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "plan"})

            result = await channel._handle_session_load({"sessionId": session_id, "cwd": "/tmp"})

            self._assert_advertises_modes(result, "plan")
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_session_resume_reports_the_mode_already_chosen(self):
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]
            await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "ask"})

            result = await channel._handle_session_resume({"sessionId": session_id, "cwd": "/tmp"})

            self._assert_advertises_modes(result, "ask")
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_session_fork_inherits_the_parent_mode(self):
        """A fork continues the parent, so it inherits its mode as well as its cwd."""
        channel = self._make_channel()
        try:
            parent = (await channel._handle_session_new({"cwd": "/tmp/parent"}))["sessionId"]
            await channel._handle_session_set_mode({"sessionId": parent, "modeId": "bypass"})

            result = await channel._handle_session_fork({"sessionId": parent, "cwd": "/tmp/fork"})

            assert "sessionId" in result
            self._assert_advertises_modes(result, "bypass")
        finally:
            self._cancel_consumers(channel)


class TestAcpModeSwitching:
    """set_mode and set_config_option write the same state and validate input."""

    def _make_channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), make_manager())

    def _cancel_consumers(self, channel: ACPChannel) -> None:
        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_set_mode_stores_the_mode(self):
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]

            assert (
                await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "plan"})
                == {}
            )

            assert channel._session_mode(session_id) == "plan"
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_set_mode_rejects_an_unknown_mode(self):
        """Silently accepting it would show the user a mode the runner never applies."""
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]

            with pytest.raises(ValueError, match="unknown modeId"):
                await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "turbo"})

            # The rejected value must not have been stored.
            assert channel._session_mode(session_id) == _ACP_DEFAULT_MODE
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_set_config_option_is_equivalent_to_set_mode(self):
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]

            result = await channel._handle_session_set_config_option(
                {"sessionId": session_id, "configId": "mode", "value": "ask"}
            )

            assert channel._session_mode(session_id) == "ask"
            # ACP requires the response to carry the COMPLETE option list.
            assert [m["value"] for m in result["configOptions"][0]["options"]] == list(
                _ACP_MODE_ORDER
            )
            assert result["configOptions"][0]["currentValue"] == "ask"
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_set_config_option_rejects_an_unknown_value(self):
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]

            with pytest.raises(ValueError, match="unknown value"):
                await channel._handle_session_set_config_option(
                    {"sessionId": session_id, "configId": "mode", "value": "turbo"}
                )
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_unknown_config_id_is_ignored_and_not_stored(self):
        """The old handler accumulated junk into a shape no client can render."""
        channel = self._make_channel()
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]

            result = await channel._handle_session_set_config_option(
                {"sessionId": session_id, "configId": "model", "value": "gpt-9"}
            )

            state = channel._get_state().session_states[session_id]
            assert "model" not in state.config_options
            # The real option list comes back untouched.
            assert [o["id"] for o in result["configOptions"]] == ["mode"]
        finally:
            self._cancel_consumers(channel)


class TestAcpModeReachesTheTurn:
    """The mode must reach the runner, not merely the picker."""

    def _make_channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), make_manager())

    def _cancel_consumers(self, channel: ACPChannel) -> None:
        for task in channel._get_state().consumer_tasks.values():
            task.cancel()

    async def _run_one_turn(self, channel: ACPChannel, manager: MagicMock, session_id: str):
        """Submit a prompt and immediately release it, returning the call kwargs."""
        pending = asyncio.create_task(
            channel._handle_session_prompt(
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "Hello"}]}
            )
        )
        try:
            await asyncio.sleep(0)
            call = manager.submit_loop_input.await_args
            channel._resolve_pending_turn(_STDIO_SENTINEL, "acp:test-session", "end_turn")
            await pending
            return call
        finally:
            pending.cancel()

    @pytest.mark.asyncio
    async def test_chosen_mode_is_sent_with_the_turn(self):
        manager = make_manager()
        channel = ACPChannel(ACPConfig(enabled=True), manager)
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]
            await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "plan"})

            call = await self._run_one_turn(channel, manager, session_id)

            assert call.kwargs["interaction_mode"] == "plan"
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_default_mode_sends_none_so_old_behaviour_is_preserved(self):
        manager = make_manager()
        channel = ACPChannel(ACPConfig(enabled=True), manager)
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]

            call = await self._run_one_turn(channel, manager, session_id)

            assert call.kwargs["interaction_mode"] is None
        finally:
            self._cancel_consumers(channel)

    @pytest.mark.asyncio
    async def test_switching_back_to_auto_clears_the_mode(self):
        manager = make_manager()
        channel = ACPChannel(ACPConfig(enabled=True), manager)
        try:
            session_id = (await channel._handle_session_new({}))["sessionId"]
            await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "bypass"})

            call = await self._run_one_turn(channel, manager, session_id)
            assert call.kwargs["interaction_mode"] == "bypass"

            await channel._handle_session_set_mode({"sessionId": session_id, "modeId": "agent"})
            call = await self._run_one_turn(channel, manager, session_id)
            assert call.kwargs["interaction_mode"] is None
        finally:
            self._cancel_consumers(channel)


_PLAN_REVIEW_QUESTION = [
    {
        "question": "Action for this plan: Approve, Reject, or Refine?",
        "header": "Plan review",
        "options": [{"label": "Approve"}, {"label": "Reject"}, {"label": "Refine"}],
    }
]


async def _cancel_clarification_tasks(channel: ACPChannel) -> None:
    """Cancel the answer-waiters a test spawned so nothing outlives the test."""
    for task in list(channel._clarification_tasks):
        task.cancel()
    await asyncio.sleep(0)


def _make_clarification_channel(*, form_elicitation: bool = False) -> tuple[ACPChannel, str, str]:
    """Channel with a registered session and a chosen elicitation capability."""
    manager = MagicMock()
    manager.handle_inbound = AsyncMock(return_value="acp:clar")
    # The answer comes back as a turn, so this has to be awaitable.
    manager.submit_loop_input = AsyncMock()
    manager._event_bus = MagicMock()
    manager._event_bus.subscribe = AsyncMock()
    manager._event_bus.publish = AsyncMock()

    channel = ACPChannel(ACPConfig(enabled=True), manager)
    session_id = "clar-session"
    loop_id = "acp:clar"
    state = channel._get_state()
    state.session_map[session_id] = loop_id
    state.client_supports_form_elicitation = form_elicitation
    return channel, session_id, loop_id


class TestClarificationEventTrigger:
    """Plan mode's question arrives as a custom event, not as an interrupt.

    `await_user` raises a LangGraph interrupt to *suspend* the graph, but the
    runner translates the internal emit into `ClarificationRequestedEvent`
    before it reaches a channel. Matching the raw interrupt instead — the first
    attempt at this — left the event with no consumer, so it fell through the
    block translator and was dropped: the plan appeared with no way to approve,
    reject or refine it, and the turn reported `end_turn`.
    """

    def _channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), MagicMock())

    @staticmethod
    def _frame(**payload: object) -> dict:
        return {
            "type": "event",
            "mode": "custom",
            "data": {
                "type": "soothe.loop.clarification.requested",
                "questions": _PLAN_REVIEW_QUESTION,
                "origin_node": "plan_mode_review",
                "plan_path": "/tmp/plan.md",
                **payload,
            },
        }

    @pytest.mark.asyncio
    async def test_a_clarification_frame_is_claimed_and_asked(self):
        channel, session_id, loop_id = _make_clarification_channel(form_elicitation=True)
        written: list[dict] = []

        async def _capture(msg):
            written.append(msg)

        try:
            with patch.object(channel, "_write_jsonrpc", _capture):
                handled = await channel._apply_clarification_event(
                    session_id, loop_id, self._frame()
                )

            assert handled is True
            assert len(written) == 1
            assert written[0]["method"] == "elicitation/create"
            assert written[0]["params"]["mode"] == "form"
        finally:
            await _cancel_clarification_tasks(channel)

    @pytest.mark.asyncio
    async def test_unrelated_frames_are_not_claimed(self):
        channel, session_id, loop_id = _make_clarification_channel(form_elicitation=True)
        frames = [
            {"mode": "custom", "data": {"type": "soothe.cognition.plan.created"}},
            {"mode": "messages", "data": ({}, {})},
            {"mode": "custom", "data": {"type": "soothe.loop.clarification.requested"}},
            {
                "mode": "custom",
                "data": {
                    "type": "soothe.loop.clarification.requested",
                    "questions": [],
                },
            },
        ]

        for frame in frames:
            assert await channel._apply_clarification_event(session_id, loop_id, frame) is False

    @pytest.mark.asyncio
    async def test_a_tool_approval_clarification_is_left_to_the_permission_bridge(self):
        """Answering it here too would ask the user the same approval twice."""
        channel, session_id, loop_id = _make_clarification_channel(form_elicitation=True)
        written: list[dict] = []

        async def _capture(msg):
            written.append(msg)

        with patch.object(channel, "_write_jsonrpc", _capture):
            handled = await channel._apply_clarification_event(
                session_id, loop_id, self._frame(origin_node="tool_approval")
            )

        assert handled is True
        assert written == []


class TestInterruptClassification:
    """Only tool approval is answered from an interrupt.

    Clarifications take the custom-event path, so claiming the raw
    `interrupt({"type": "clarification"})` as well would double-answer them.
    """

    def _channel(self) -> ACPChannel:
        return ACPChannel(ACPConfig(enabled=True), MagicMock())

    @staticmethod
    def _interrupt_event(payload: dict) -> dict:
        return {"type": "event", "loop_id": "acp:x", "data": {"__interrupt__": payload}}

    def test_tool_approval_interrupt_is_classified(self):
        channel = self._channel()
        event = self._interrupt_event(
            {"interrupt_id": "int-2", "action_requests": [{"tool_name": "write_file"}]}
        )

        assert channel._interrupt_kind(channel._interrupt_payload(event)) == "tool_approval"
        assert channel._is_tool_approval_event(event) is True

    def test_a_clarification_interrupt_is_not_claimed(self):
        """It is answered via the custom event, never from the raw interrupt."""
        channel = self._channel()
        event = self._interrupt_event(
            {"type": "clarification", "interrupt_id": "int-1", "questions": ["q"]}
        )

        assert channel._interrupt_kind(channel._interrupt_payload(event)) is None
        assert channel._is_tool_approval_event(event) is False

    def test_a_nested_envelope_is_unwrapped(self):
        channel = self._channel()
        nested = {
            "data": {
                "data": {
                    "__interrupt__": {
                        "interrupt_id": "int-3",
                        "action_requests": [{"tool_name": "t"}],
                    }
                }
            }
        }

        assert channel._is_tool_approval_event(nested) is True

    def test_an_event_without_an_interrupt_yields_nothing(self):
        channel = self._channel()

        assert channel._interrupt_payload({"mode": "custom", "data": {"type": "other"}}) is None


class TestClarificationElicitationShape:
    """What the client receives must be schema-valid and answerable."""

    def test_an_action_question_becomes_an_enum_plus_a_comment_slot(self):
        """Clients render an enum as a choice, not free text."""
        params = _clarification_elicitation_params("s1", _PLAN_REVIEW_QUESTION)
        schema = params["requestedSchema"]

        assert params["mode"] == "form"
        assert params["sessionId"] == "s1"
        assert schema["properties"]["answer_0"]["enum"] == ["Approve", "Reject", "Refine"]
        # The decoder reads the comment from answers[1], so the form must offer it.
        assert "comment" in schema["properties"]
        assert schema["required"] == ["answer_0"]

    def test_a_plain_question_becomes_free_text(self):
        params = _clarification_elicitation_params("s1", ["What database should I use?"])
        schema = params["requestedSchema"]

        assert "enum" not in schema["properties"]["answer_0"]
        assert schema["properties"]["answer_0"]["title"] == "What database should I use?"
        assert "comment" not in schema["properties"]

    def test_several_questions_each_get_a_field(self):
        params = _clarification_elicitation_params("s1", ["first?", "second?"])
        schema = params["requestedSchema"]

        assert sorted(schema["properties"]) == ["answer_0", "answer_1"]
        assert schema["required"] == ["answer_0", "answer_1"]


class TestClarificationAnswerEncoding:
    """The interrupt's resume value is `[action, comment]`, or one per question."""

    def test_the_action_is_paired_with_a_comment_slot(self):
        """The decoder reads index 1 even when only the action is meaningful."""
        assert _clarification_answers_from_response(
            {"action": "accept", "content": {"answer_0": "Approve"}}, _PLAN_REVIEW_QUESTION
        ) == ["Approve", ""]

    def test_refinement_carries_its_comment(self):
        assert _clarification_answers_from_response(
            {"action": "accept", "content": {"answer_0": "Refine", "comment": "use postgres"}},
            _PLAN_REVIEW_QUESTION,
        ) == ["Refine", "use postgres"]

    def test_decline_cancel_and_other_are_not_answers(self):
        for action in ("decline", "cancel", "other"):
            assert (
                _clarification_answers_from_response({"action": action}, _PLAN_REVIEW_QUESTION)
                is None
            )

    def test_accept_without_the_required_action_is_not_an_answer(self):
        """Never invent an "Approve" — it authorises editing the workspace."""
        assert (
            _clarification_answers_from_response(
                {"action": "accept", "content": {}}, _PLAN_REVIEW_QUESTION
            )
            is None
        )

    def test_plain_questions_answer_in_order(self):
        assert _clarification_answers_from_response(
            {"action": "accept", "content": {"answer_0": "a", "answer_1": "b"}},
            ["first?", "second?"],
        ) == ["a", "b"]

    def test_a_partial_plain_answer_is_not_an_answer(self):
        assert (
            _clarification_answers_from_response(
                {"action": "accept", "content": {"answer_0": "a"}}, ["first?", "second?"]
            )
            is None
        )


class TestClarificationCapabilityGate:
    """A client that cannot render a form must not be sent one."""

    def test_the_backchat_shaped_capability_is_accepted(self):
        assert _client_supports_form_elicitation({"elicitation": {"form": {}}}) is True

    def test_missing_and_url_only_capabilities_are_rejected(self):
        for caps in ({}, {"elicitation": {}}, {"elicitation": {"url": {}}}, None, "nope"):
            assert _client_supports_form_elicitation(caps) is False, caps

    @pytest.mark.asyncio
    async def test_nothing_is_sent_without_the_capability(self):
        channel, session_id, loop_id = _make_clarification_channel()
        written: list[dict] = []

        async def _capture(msg):
            written.append(msg)

        with patch.object(channel, "_write_jsonrpc", _capture):
            handled = await channel._apply_clarification_event(
                session_id,
                loop_id,
                {
                    "mode": "custom",
                    "data": {
                        "type": "soothe.loop.clarification.requested",
                        "questions": _PLAN_REVIEW_QUESTION,
                        "origin_node": "plan_mode_review",
                    },
                },
            )

        # Claimed (so it does not reach the block translator) but never asked,
        # and no answer-turn is queued: there is nobody to answer it.
        assert handled is True
        assert written == [], "an unsupported client must not be asked"
        channel._manager.submit_loop_input.assert_not_called()


class TestClarificationAnswerTurn:
    """The answer returns as an ordinary turn, not an interrupt resume.

    Soothe reads `clarification_answers` from a normal turn — which is how its
    own CLI answers — and the `clarification_answer` flag is what routes that
    turn into the suspended graph instead of starting a new goal.
    """

    @staticmethod
    def _frame(**payload: object) -> dict:
        return {
            "type": "event",
            "mode": "custom",
            "data": {
                "type": "soothe.loop.clarification.requested",
                "questions": _PLAN_REVIEW_QUESTION,
                "origin_node": "plan_mode_review",
                "plan_path": "/tmp/plan.md",
                **payload,
            },
        }

    async def _ask_then_answer(self, answer: dict | None) -> ACPChannel:
        channel, _, _ = _make_clarification_channel(form_elicitation=True)
        written: list[dict] = []

        async def _capture(msg):
            written.append(msg)

        # Answer the elicitation as soon as it is asked; the handler awaits it
        # on a detached task so the running turn is not blocked.
        with patch.object(channel, "_write_jsonrpc", _capture):
            spawner = asyncio.create_task(
                channel._apply_clarification_event("clar-session", "acp:clar", self._frame())
            )
            for _ in range(50):
                await asyncio.sleep(0.01)
                if written:
                    break
            assert written, "the elicitation was never sent"
            if answer is not None:
                channel._get_state().pending_permissions[written[0]["id"]].set_result(answer)
            await asyncio.wait_for(spawner, timeout=5.0)
            for task in list(channel._clarification_tasks):
                await asyncio.wait_for(task, timeout=5.0)
        return channel

    @pytest.mark.asyncio
    async def test_approve_submits_a_clarification_turn(self):
        channel = await self._ask_then_answer(
            {"action": "accept", "content": {"answer_0": "Approve"}}
        )

        channel._manager.submit_loop_input.assert_awaited_once_with(
            "acp:clar",
            "Plan review: Approve",
            channel="acp",
            chat_id="clar-session",
            clarification_answers=["Approve", ""],
            # An approval must carry the artifact it was shown, or the runner has
            # nothing to execute.
            approved_plan_path="/tmp/plan.md",
        )

    @pytest.mark.asyncio
    async def test_refinement_carries_its_comment(self):
        channel = await self._ask_then_answer(
            {"action": "accept", "content": {"answer_0": "Refine", "comment": "use postgres"}}
        )

        call = channel._manager.submit_loop_input.await_args
        assert call.args[1] == "Plan review: Refine — use postgres"
        assert call.kwargs["clarification_answers"] == ["Refine", "use postgres"]

    @pytest.mark.asyncio
    async def test_a_rejection_still_answers_without_a_plan_path(self):
        channel = await self._ask_then_answer(
            {"action": "accept", "content": {"answer_0": "Reject"}}
        )

        call = channel._manager.submit_loop_input.await_args
        assert call.kwargs["clarification_answers"] == ["Reject", ""]
        # Nothing to execute, so the runner must not be pointed at the artifact.
        assert call.kwargs["approved_plan_path"] is None

    @pytest.mark.asyncio
    async def test_a_dismissal_submits_nothing(self):
        channel = await self._ask_then_answer({"action": "decline"})

        channel._manager.submit_loop_input.assert_not_called()


class TestClarificationTurnText:
    """The turn text leads with a stable `<origin>: <action>` header.

    A bare action string would be classified as a fresh task if the
    `clarification_answer` flag were ever dropped in transit.
    """

    def test_selector_actions_are_prefixed(self):
        assert _clarification_turn_text(["Approve", ""], "plan_mode_review") == (
            "Plan review: Approve"
        )

    def test_refinement_is_appended_to_the_header(self):
        assert _clarification_turn_text(["Refine", "use postgres"], "plan_mode_review") == (
            "Plan review: Refine — use postgres"
        )

    def test_a_single_free_form_answer_passes_through(self):
        assert _clarification_turn_text(["postgres"], "execute") == "postgres"

    def test_multiple_answers_are_numbered(self):
        assert _clarification_turn_text(["a", "b"], "execute") == "A1: a | A2: b"

    def test_no_answers_yields_no_text(self):
        assert _clarification_turn_text([], "plan_mode_review") == ""
