"""End-to-end integration tests for ACP channel over WebSocket transport.

These tests boot a minimal in-process FastAPI/uvicorn server with an
``ACPChannel`` configured for ``transport="websocket"``, connect to the
``/acp`` endpoint via the ``websockets`` library, and exchange raw NDJSON
JSON-RPC 2.0 frames. No ACP SDK client is needed — ACP is just NDJSON
JSON-RPC over the wire.

Protocol flow tested:

- ``initialize`` handshake → assert ``protocolVersion`` and agent info.
- ``session/new`` → assert ``sessionId`` returned.
- ``session/prompt`` → assert response and ``session/update`` notifications
  (simulated by publishing an ``OUTPUT_TEXT_DELTA`` event on the EventBus).
- Permission bridge: publish a ``__interrupt__`` event with
  ``action_requests`` → assert ``session/request_permission`` request
  received over WS → send allow response → assert resume command published
  on the EventBus.

Tests are marked ``integration`` and require ``--run-integration``.
The ``websockets`` package is required (guarded by
``pytest.importorskip``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi import FastAPI

websockets = pytest.importorskip("websockets")

from soothe_daemon.channels.acp import ACPChannel  # noqa: E402
from soothe_daemon.config.models import ACPConfig  # noqa: E402
from soothe_daemon.event import EventBus, loop_event_topic  # noqa: E402
from soothe_daemon.events.constants import OUTPUT_TEXT_DELTA  # noqa: E402
from tests.integration.daemon_fixtures import alloc_ephemeral_port  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alloc_port() -> int:
    """Allocate an ephemeral port for the test uvicorn server."""
    return alloc_ephemeral_port()


def _find_loop_id(channel: ACPChannel, session_id: str) -> str:
    """Find the loop_id for a session_id by searching all connections.

    In WS mode the server-side WebSocket object differs from the client-side
    connection, so we can't use ``_get_state(ws)`` from the test. Instead we
    search all connection states.
    """
    for state in channel._connections.values():
        if session_id in state.session_map:
            return state.session_map[session_id]
    raise KeyError(f"session_id {session_id} not found in any connection")


class _MockManager:
    """Mock ChannelManager for ACP WS E2E tests.

    Provides a real EventBus and a mock ``handle_inbound`` that returns a
    deterministic loop_id. This lets the ACP channel create sessions and
    subscribe to EventBus topics without needing a full daemon.
    """

    def __init__(self) -> None:
        self._event_bus = EventBus()
        self._inbound_counter = 0
        self.handle_inbound = AsyncMock(side_effect=self._handle_inbound)
        self._message_handler = None
        self._handshake_callback = None

    async def _handle_inbound(self, **kwargs: Any) -> str:
        """Return a deterministic loop_id based on call count."""
        self._inbound_counter += 1
        return f"acp:ws-test-loop-{self._inbound_counter}"


async def _send_jsonrpc(ws: Any, msg: dict[str, Any]) -> None:
    """Send a JSON-RPC dict as a single NDJSON line over WebSocket."""
    await ws.send(json.dumps(msg) + "\n")


async def _recv_jsonrpc(ws: Any, *, timeout: float = 5.0) -> dict[str, Any]:
    """Receive a single NDJSON JSON-RPC line from WebSocket."""
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw.strip())


async def _recv_jsonrpc_matching(
    ws: Any,
    *,
    match_id: int | None = None,
    match_method: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Read NDJSON lines until one matches the given id or method.

    ACP notifications (``session/update``, ``session/request_permission``)
    have no ``id`` field; responses have ``id``. This helper filters.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            msg = "Timed out waiting for JSON-RPC"
            if match_id is not None:
                msg += f" id={match_id}"
            if match_method is not None:
                msg += f" method={match_method}"
            raise TimeoutError(msg)
        msg = await _recv_jsonrpc(ws, timeout=remaining)
        if match_id is not None and msg.get("id") == match_id:
            return msg
        if match_method is not None and msg.get("method") == match_method:
            return msg
        # Keep reading until we find a match.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio backend."""
    return "asyncio"


@pytest.fixture
async def acp_ws_server():
    """Boot an in-process FastAPI/uvicorn server with ACP WS transport.

    Yields ``(base_url, channel, manager)`` where ``base_url`` is the
    ``ws://127.0.0.1:<port>`` URL, ``channel`` is the ``ACPChannel``
    instance, and ``manager`` is the mock manager (with a real EventBus).
    """
    port = _alloc_port()
    app = FastAPI(title="ACP WS Test", docs_url=None, redoc_url=None, openapi_url=None)

    manager = _MockManager()
    config = ACPConfig(
        enabled=True,
        agent_name="TestAgent",
        agent_description="Test agent for WS E2E",
        transport="websocket",
        ws_path="/acp",
    )
    channel = ACPChannel(config, manager, unified_app=app)
    await channel.start()

    uv_cfg = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    server = uvicorn.Server(uv_cfg)
    serve_task = asyncio.create_task(server.serve())
    # Wait for uvicorn to bind — poll the port until it accepts connections.
    import socket as _socket

    for _ in range(50):
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect(("127.0.0.1", port))
                break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task
        raise RuntimeError(f"uvicorn failed to bind on port {port}")

    base_url = f"ws://127.0.0.1:{port}"
    try:
        yield base_url, channel, manager
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)
        await channel.stop()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestACPWebSocketE2E:
    """E2E tests for ACP-over-WebSocket using raw NDJSON JSON-RPC frames."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_initialize_handshake(self, acp_ws_server) -> None:
        """Test initialize handshake over WebSocket transport."""
        base_url, channel, manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            # Send initialize
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "ws-test", "version": "1.0"},
                    },
                },
            )

            response = await _recv_jsonrpc_matching(ws, match_id=1)
            assert response["jsonrpc"] == "2.0"
            assert response["id"] == 1
            assert "result" in response
            assert "error" not in response
            result = response["result"]
            assert "protocolVersion" in result
            assert "capabilities" in result
            assert result["agent"]["name"] == "TestAgent"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_new(self, acp_ws_server) -> None:
        """Test session/new over WebSocket transport."""
        base_url, channel, manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            # Initialize first
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "ws-test", "version": "1.0"},
                    },
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create a new session
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/tmp"},
                },
            )

            response = await _recv_jsonrpc_matching(ws, match_id=2)
            assert response["jsonrpc"] == "2.0"
            assert response["id"] == 2
            assert "result" in response
            assert "sessionId" in response["result"]
            session_id = response["result"]["sessionId"]
            assert len(session_id) > 0

            # Clean up consumer tasks
            for state in channel._connections.values():
                for task in state.consumer_tasks.values():
                    task.cancel()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_prompt_with_update_notification(self, acp_ws_server) -> None:
        """Test session/prompt and session/update notification flow over WS.

        After session/prompt, we publish an ``OUTPUT_TEXT_DELTA`` event on the
        EventBus for the loop. The ACP consumer translates it to a
        ``session/update`` notification and sends it over the WebSocket.
        """
        base_url, channel, manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            # Initialize
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "ws-test", "version": "1.0"},
                    },
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create session
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/tmp"},
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            # Find the loop_id for this session
            loop_id = _find_loop_id(channel, session_id)

            # Send session/prompt
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": "Hello, agent!"}],
                    },
                },
            )

            # Publish an OUTPUT_TEXT_DELTA event on the EventBus for this loop.
            # The consumer task will translate it to a session/update notification.
            delta_event = {
                "type": "event",
                "loop_id": loop_id,
                "data": {
                    "type": OUTPUT_TEXT_DELTA,
                    "content": "Processing your request...",
                },
            }
            topic = loop_event_topic(loop_id)
            await manager._event_bus.publish(topic, delta_event)

            # We should receive the session/update notification
            update_msg = await _recv_jsonrpc_matching(
                ws, match_method="session/update", timeout=5.0
            )
            assert update_msg["jsonrpc"] == "2.0"
            assert update_msg["method"] == "session/update"
            params = update_msg["params"]
            assert params["sessionId"] == session_id
            blocks = params["update"]["blocks"]
            assert len(blocks) >= 1
            assert blocks[0]["type"] == "text"
            assert "Processing" in blocks[0]["text"]

            # Also expect the session/prompt response (id=3)
            prompt_resp = await _recv_jsonrpc_matching(ws, match_id=3, timeout=5.0)
            assert prompt_resp["jsonrpc"] == "2.0"
            assert prompt_resp["id"] == 3
            assert "result" in prompt_resp

            # Clean up consumer tasks
            for state in channel._connections.values():
                for task in state.consumer_tasks.values():
                    task.cancel()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_permission_bridge_flow(self, acp_ws_server) -> None:
        """Test the permission bridge over WebSocket.

        After session/new, we publish a ``__interrupt__`` event with
        ``action_requests`` on the EventBus. The consumer detects it and
        sends a ``session/request_permission`` request over the WS. The test
        reads the request, sends back an allow response, and asserts that a
        resume command is published on the EventBus.
        """
        base_url, channel, manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            # Initialize
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "ws-test", "version": "1.0"},
                    },
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create session
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/tmp"},
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            # Find the loop_id
            loop_id = _find_loop_id(channel, session_id)

            # Publish a __interrupt__ event with action_requests on the EventBus.
            # The consumer will detect it and send session/request_permission.
            interrupt_event = {
                "type": "event",
                "loop_id": loop_id,
                "data": {
                    "__interrupt__": {
                        "interrupt_id": "int-ws-001",
                        "action_requests": [
                            {
                                "tool_call_id": "tc-ws-1",
                                "tool_name": "run_command",
                                "args": {"command": "echo hello"},
                            }
                        ],
                    }
                },
            }
            topic = loop_event_topic(loop_id)
            await manager._event_bus.publish(topic, interrupt_event)

            # Read the session/request_permission request from the WS
            perm_req = await _recv_jsonrpc_matching(
                ws, match_method="session/request_permission", timeout=5.0
            )
            assert perm_req["jsonrpc"] == "2.0"
            assert perm_req["method"] == "session/request_permission"
            perm_params = perm_req["params"]
            assert perm_params["sessionId"] == session_id
            assert perm_params["toolCall"]["toolCallId"] == "tc-ws-1"
            assert len(perm_params["options"]) >= 2

            req_id = perm_req["id"]

            # Send back an allow response
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "outcome": "selected",
                        "optionId": "allow_once",
                    },
                },
            )

            # Wait for the resume command to be published on the EventBus.
            # The _route_permission_response publishes a resume command on the
            # loop topic. We subscribe a second queue to capture it.
            resume_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
            await manager._event_bus.subscribe(topic, resume_queue)

            # Wait for the resume command
            resume_item = await asyncio.wait_for(resume_queue.get(), timeout=5.0)
            # EventBus may deliver 2-tuples (event, meta) or just event
            if isinstance(resume_item, tuple) and len(resume_item) == 2:
                resume_msg = resume_item[0]
            else:
                resume_msg = resume_item

            assert resume_msg["type"] == "command"
            assert resume_msg["command"] == "resume"
            assert "int-ws-001" in resume_msg["resume_payload"]
            assert resume_msg["resume_payload"]["int-ws-001"]["decisions"][0]["type"] == "approve"

            # Unsubscribe the resume queue
            await manager._event_bus.unsubscribe(topic, resume_queue)

            # Clean up consumer tasks
            for state in channel._connections.values():
                for task in state.consumer_tasks.values():
                    task.cancel()
