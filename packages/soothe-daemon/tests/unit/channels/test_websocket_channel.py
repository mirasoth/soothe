"""Unit tests for WebSocketChannel (RFC-620)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from soothe_daemon.channels.websocket import WebSocketChannel


class TestWebSocketChannelAttributes:
    """Tests for WebSocketChannel attributes."""

    def test_channel_metadata(self):
        """Test channel name and display name."""
        assert WebSocketChannel.name == "websocket"
        assert WebSocketChannel.display_name == "WebSocket"

    def test_capability_flags(self):
        """Test capability flags are correct."""
        assert WebSocketChannel.supports_inbound is True
        assert WebSocketChannel.supports_outbound is True
        assert WebSocketChannel.supports_streaming is True

    def test_inherits_from_channel(self):
        """Test inherits from Channel base class."""
        from soothe_daemon.channels.base import Channel

        assert issubclass(WebSocketChannel, Channel)


class TestWebSocketChannelMethods:
    """Tests for WebSocketChannel methods."""

    def test_channel_name(self):
        """Test channel name identifier."""
        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        assert channel.name == "websocket"


class MockWebSocketConfig:
    """Mock WebSocket config."""

    enabled = True
    host = "127.0.0.1"
    port = 8765
    tls_enabled = False
    tls_cert = None
    tls_key = None
    cors_origins = ["http://localhost:*"]
    max_frame_size = 10485760


class MockManager:
    """Mock manager for testing."""

    _message_handler = None
    _handshake_callback = None


class TestWebSocketChannelInit:
    """Tests for WebSocketChannel initialization."""

    def test_basic_init(self):
        """Test basic initialization."""
        config = MockWebSocketConfig()
        manager = MockManager()

        channel = WebSocketChannel(config, manager)

        assert channel.name == "websocket"
        assert channel._ws_config == config
        assert channel._manager == manager
        assert channel.is_running is False

    def test_init_with_session_manager(self):
        """Test initialization with session manager."""
        config = MockWebSocketConfig()
        manager = MockManager()
        session_manager = object()

        channel = WebSocketChannel(
            config,
            manager,
            session_manager=session_manager,
        )

        assert channel._session_manager == session_manager

    def test_init_with_unified_app(self):
        """Test initialization with unified app."""
        from fastapi import FastAPI

        config = MockWebSocketConfig()
        manager = MockManager()
        app = FastAPI()

        channel = WebSocketChannel(
            config,
            manager,
            unified_app=app,
        )

        assert channel._unified_parent_app == app


class TestWebSocketChannelStreaming:
    """Tests for WebSocketChannel streaming capabilities."""

    def test_supports_streaming_is_true(self):
        """Test streaming capability."""
        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        assert channel.supports_streaming is True

    def test_send_delta_method_exists(self):
        """Test send_delta method exists."""
        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        assert hasattr(channel, "send_delta")
        assert callable(channel.send_delta)


class TestWebSocketChannelMessageConversion:
    """Tests for message conversion."""

    def test_channel_message_to_wire(self):
        """Test ChannelMessage to wire format conversion."""
        from soothe_daemon.channels.message import ChannelMessage

        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        msg = ChannelMessage(
            channel="websocket",
            chat_id="loop-abc",
            content="Hello world",
            metadata={"_progress": True},
        )

        wire = channel._channel_message_to_wire(msg)

        assert wire["type"] == "event"
        assert wire["loop_id"] == "loop-abc"
        assert wire["data"]["type"] == "soothe.output.text.complete"
        assert wire["data"]["content"] == "Hello world"
        assert wire["data"]["_progress"] is True

    def test_channel_message_to_wire_with_streaming(self):
        """Test streaming message conversion."""
        from soothe_daemon.channels.message import META_STREAM_DELTA, ChannelMessage

        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        msg = ChannelMessage(
            channel="websocket",
            chat_id="loop-xyz",
            content="partial",
            metadata={META_STREAM_DELTA: True, "_stream_id": "s1"},
        )

        wire = channel._channel_message_to_wire(msg)

        assert wire["data"]["_stream_delta"] is True
        assert wire["data"]["_stream_id"] == "s1"


class TestWebSocketChannelCORS:
    """Tests for CORS validation."""

    def test_empty_origin_allowed(self):
        """Test empty origin is allowed."""
        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        assert channel._validate_cors(None) is True

    def test_matching_origin_allowed(self):
        """Test matching origin pattern is allowed."""
        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        assert channel._validate_cors("http://localhost:3000") is True
        assert channel._validate_cors("http://localhost:8080") is True

    def test_non_matching_origin_denied(self):
        """Test non-matching origin is denied."""
        config = MockWebSocketConfig()
        manager = MockManager()
        channel = WebSocketChannel(config, manager)

        assert channel._validate_cors("http://example.com") is False
        assert channel._validate_cors("https://evil.com") is False


class FakeSessionManager:
    """Session manager double returning a fixed session (or None)."""

    def __init__(self, session=None):
        self._session = session
        self.get_session_calls: list[str] = []

    async def get_session(self, client_id):
        self.get_session_calls.append(client_id)
        return self._session


class FakeCronService:
    """Cron service double for command dispatch."""

    async def list_jobs(self, user_id, status=None):
        return []


class FakeWebSocket:
    """WebSocket double that records sent wire frames."""

    def __init__(self, fail_sends: bool = False):
        self.sent: list[dict] = []
        self._fail_sends = fail_sends

    async def send_text(self, text: str) -> None:
        if self._fail_sends:
            raise RuntimeError("send failed")
        self.sent.append(json.loads(text))


class TestCommandAuthGate:
    """Command dispatch honors the identity_enabled auth gate."""

    def _channel(self, identity_enabled: bool, session) -> WebSocketChannel:
        config = MockWebSocketConfig()
        manager = MockManager()
        session_manager = FakeSessionManager(session)
        return WebSocketChannel(
            config,
            manager,
            session_manager=session_manager,
            cron_service=FakeCronService(),
            identity_enabled=identity_enabled,
        )

    async def test_identity_enabled_unauthenticated_session_rejected(self):
        session = SimpleNamespace(authenticated=False)
        channel = self._channel(identity_enabled=True, session=session)
        websocket = FakeWebSocket()

        await channel._handle_command_message(
            websocket,
            {"type": "command", "command": "cron_list_jobs", "request_id": "r1", "payload": {}},
            "client-1",
        )

        assert len(websocket.sent) == 1
        response = websocket.sent[0]
        assert response["type"] == "command_response"
        assert response["request_id"] == "r1"
        assert response["error"] == "AUTH_REQUIRED"
        assert response["result"] is None

    async def test_identity_enabled_missing_session_rejected(self):
        channel = self._channel(identity_enabled=True, session=None)
        websocket = FakeWebSocket()

        await channel._handle_command_message(
            websocket,
            {"type": "command", "command": "cron_list_jobs", "request_id": "r2", "payload": {}},
            "client-1",
        )

        assert websocket.sent[0]["error"] == "AUTH_REQUIRED"

    async def test_identity_enabled_authenticated_session_dispatches(self):
        session = SimpleNamespace(authenticated=True)
        channel = self._channel(identity_enabled=True, session=session)
        websocket = FakeWebSocket()

        await channel._handle_command_message(
            websocket,
            {"type": "command", "command": "cron_list_jobs", "request_id": "r3", "payload": {}},
            "client-1",
        )

        response = websocket.sent[0]
        assert response["error"] is None
        assert response["result"] == {"jobs": [], "source": "cron_service"}

    async def test_identity_disabled_dispatches_without_session(self):
        channel = self._channel(identity_enabled=False, session=None)
        websocket = FakeWebSocket()

        await channel._handle_command_message(
            websocket,
            {"type": "command", "command": "cron_list_jobs", "request_id": "r4", "payload": {}},
            "client-1",
        )

        response = websocket.sent[0]
        assert response["error"] is None
        assert response["result"] == {"jobs": [], "source": "cron_service"}


class TestBroadcast:
    """broadcast zips send results against a single client snapshot."""

    async def test_failed_clients_removed_and_healthy_kept(self):
        config = MockWebSocketConfig()
        channel = WebSocketChannel(config, MockManager())

        healthy = FakeWebSocket()
        broken = FakeWebSocket(fail_sends=True)
        channel._clients[healthy] = {"client_id": "healthy"}
        channel._clients[broken] = {"client_id": "broken"}

        await channel.broadcast({"type": "ping"})

        assert healthy.sent == [{"type": "ping"}]
        assert broken not in channel._clients
        assert healthy in channel._clients
