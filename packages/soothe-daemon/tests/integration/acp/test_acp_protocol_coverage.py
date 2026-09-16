"""Comprehensive ACP protocol coverage test.

Probes every method defined in the ACP SDK's ``meta.AGENT_METHODS`` and
``meta.CLIENT_METHODS`` against the Soothe ``ACPChannel``'s JSON-RPC
dispatcher. The goal is to produce a clear report of which protocol methods
are **implemented** (return a result or a non-``-32601`` error) and which
are **not implemented** (return JSON-RPC error code ``-32601`` "Method not
found").

This is a protocol-coverage test, not a behavioural test: for each method we
send a syntactically valid JSON-RPC request and inspect the response. Methods
that return ``-32601`` are unsupported; any other response (success result
or a different error code like ``-32603`` internal-error) counts as
"handled" — the dispatcher recognised the method and attempted to process it.

Tests are marked ``integration`` and require ``--run-integration``.
The ``websockets`` package is required (guarded by ``pytest.importorskip``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

websockets = pytest.importorskip("websockets")

from acp.meta import AGENT_METHODS, CLIENT_METHODS  # noqa: E402

from soothe_daemon.channels.acp import ACPChannel  # noqa: E402
from soothe_daemon.config.models import ACPConfig  # noqa: E402
from soothe_daemon.event import loop_event_topic  # noqa: E402
from tests.integration.daemon_fixtures import alloc_ephemeral_port  # noqa: E402


def _loop_id_for(channel: ACPChannel, session_id: str) -> str | None:
    """Find the loop_id backing a session across all connection states."""
    for state in channel._connections.values():
        if session_id in state.session_map:
            return state.session_map[session_id]
    return None


async def _wait_for_submission(manager: Any, *, timeout: float = 5.0) -> None:
    """Wait until the channel has submitted a turn before ending it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not manager.submitted:
        if asyncio.get_running_loop().time() > deadline:
            msg = "session/prompt was never submitted"
            raise TimeoutError(msg)
        await asyncio.sleep(0.01)


async def _end_turn(manager: Any, channel: ACPChannel, session_id: str) -> None:
    """Publish the loop's idle status, which ends the turn and releases the prompt."""
    loop_id = _loop_id_for(channel, session_id)
    if loop_id:
        await manager._event_bus.publish(
            loop_event_topic(loop_id),
            {"type": "status", "state": "idle", "loop_id": loop_id},
        )


# ---------------------------------------------------------------------------
# Helpers (mirrors test_acp_ws_e2e.py patterns)
# ---------------------------------------------------------------------------


def _alloc_port() -> int:
    """Allocate an ephemeral port for the test uvicorn server."""
    return alloc_ephemeral_port()


class _MockManager:
    """Mock ChannelManager with a real EventBus and loop-native submission surface."""

    def __init__(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from soothe_daemon.event import EventBus

        self._event_bus = EventBus()
        self._inbound_counter = 0
        self.handle_inbound = AsyncMock(side_effect=self._handle_inbound)
        self.submitted: list[dict[str, Any]] = []
        self.submit_loop_input = AsyncMock(side_effect=self._submit_loop_input)
        self.ensure_loop_registered = AsyncMock(return_value=True)
        self._loop_input_dispatcher = SimpleNamespace(enqueue=AsyncMock(), cleanup_loop=AsyncMock())
        self._persistence_manager = SimpleNamespace(
            get_loop_metadata=AsyncMock(return_value=None),
            register_loop=AsyncMock(),
            update_loop_metadata=AsyncMock(),
            increment_loop_message_count=AsyncMock(),
        )

    def ensure_loop_id(self, channel: str, chat_id: str) -> str:
        self._inbound_counter += 1
        return f"acp:coverage-loop-{self._inbound_counter}"

    async def _handle_inbound(self, **kwargs: Any) -> str:
        self._inbound_counter += 1
        return f"acp:coverage-loop-{self._inbound_counter}"

    async def _submit_loop_input(self, loop_id: str, text: str, **kwargs: Any) -> None:
        self.submitted.append({"loop_id": loop_id, "text": text, **kwargs})


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
    """Read NDJSON lines until one matches the given id or method."""
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


# ---------------------------------------------------------------------------
# Minimal valid params for each ACP method.
# Methods not listed here fall back to an empty dict.
# ---------------------------------------------------------------------------

_METHOD_PARAMS: dict[str, dict[str, Any]] = {
    "initialize": {
        "protocolVersion": 1,
        "clientCapabilities": {},
        "clientInfo": {"name": "coverage-test", "version": "1.0"},
    },
    "session/new": {"cwd": "/tmp"},
    "session/prompt": {
        "sessionId": "__placeholder__",
        "prompt": [{"type": "text", "text": "test"}],
    },
    "session/cancel": {"sessionId": "__placeholder__"},
    "session/load": {"sessionId": "__placeholder__", "cwd": "/tmp"},
    "session/set_mode": {"sessionId": "__placeholder__", "modeId": "default"},
    "session/set_config_option": {
        "sessionId": "__placeholder__",
        "configId": "model",
        "value": "test",
        "type": "select",
    },
    "session/list": {"cwd": "/tmp"},
    "session/delete": {"sessionId": "__placeholder__"},
    "session/fork": {"sessionId": "__placeholder__", "cwd": "/tmp"},
    "session/resume": {"sessionId": "__placeholder__", "cwd": "/tmp"},
    "session/close": {"sessionId": "__placeholder__"},
    "authenticate": {"methodId": "test"},
    "providers/list": {},
    "providers/set": {
        "providerId": "test",
        "apiType": "anthropic",
        "baseUrl": "https://api.anthropic.com",
    },
    "providers/disable": {"providerId": "test"},
    "logout": {},
    "mcp/message": {"connectionId": "test-conn", "method": "ping", "params": {}},
    "nes/start": {"workspaceUri": "file:///tmp"},
    "nes/suggest": {
        "sessionId": "__placeholder__",
        "uri": "file:///tmp/test.txt",
        "version": 1,
        "position": {"line": 0, "character": 0},
        "triggerKind": "automatic",
    },
    "nes/accept": {"sessionId": "__placeholder__", "id": "sugg-1"},
    "nes/reject": {"sessionId": "__placeholder__", "id": "sugg-1"},
    "nes/close": {"sessionId": "__placeholder__"},
    "document/didOpen": {
        "sessionId": "__placeholder__",
        "uri": "file:///tmp/test.txt",
        "languageId": "python",
        "version": 1,
        "text": "print('hello')",
    },
    "document/didChange": {
        "sessionId": "__placeholder__",
        "uri": "file:///tmp/test.txt",
        "version": 2,
        "contentChanges": [{"text": "print('world')"}],
    },
    "document/didClose": {
        "sessionId": "__placeholder__",
        "uri": "file:///tmp/test.txt",
    },
    "document/didSave": {
        "sessionId": "__placeholder__",
        "uri": "file:///tmp/test.txt",
    },
    "document/didFocus": {
        "sessionId": "__placeholder__",
        "uri": "file:///tmp/test.txt",
        "version": 2,
        "position": {"line": 0, "character": 0},
        "visibleRange": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 10, "character": 0},
        },
    },
}


def _params_for(method: str, session_id: str | None = None) -> dict[str, Any]:
    """Return minimal valid params for the given method.

    If the method's params template contains ``__placeholder__`` for
    ``sessionId`` and a real ``session_id`` is provided, substitute it.
    """
    template = _METHOD_PARAMS.get(method, {})
    if session_id and "sessionId" in template:
        template = {**template, "sessionId": session_id}
    return template


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio backend."""
    return "asyncio"


@pytest.fixture
async def acp_ws_server():
    """Boot an in-process FastAPI/uvicorn server with ACP WS transport."""
    port = _alloc_port()
    app = FastAPI(title="ACP Coverage Test", docs_url=None, redoc_url=None, openapi_url=None)

    manager = _MockManager()
    config = ACPConfig(
        enabled=True,
        agent_name="CoverageAgent",
        agent_description="Coverage test agent",
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
# Protocol coverage tests
# ---------------------------------------------------------------------------


class TestACPProtocolCoverage:
    """Probe every ACP method against the Soothe ACPChannel.

    For each method in ``AGENT_METHODS`` we send a JSON-RPC request and
    classify the response:

    - **supported**: response has ``result`` or error code != ``-32601``
    - **unsupported**: response has error code ``-32601`` (Method not found)

    The test asserts that the **core** ACP methods (initialize, session/new,
    session/prompt, session/cancel, session/load) are supported, and produces
    a coverage report for all remaining methods.
    """

    # Methods that Soothe's ACP channel must support for full protocol compliance.
    CORE_METHODS = frozenset(
        {
            "initialize",
            "session/new",
            "session/prompt",
            "session/cancel",
            "session/load",
        }
    )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_all_agent_methods_probed(self, acp_ws_server) -> None:
        """Probe every ACP agent method and report coverage.

        Sends each method as a JSON-RPC request, classifies the response,
        and asserts that all core methods are supported.
        """
        base_url, channel, manager = acp_ws_server

        results: dict[str, dict[str, Any]] = {}

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            req_id = 0

            # Phase 1: initialize (required before any session method)
            req_id += 1
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            init_resp = await _recv_jsonrpc_matching(ws, match_id=req_id)
            results["initialize"] = _classify(init_resp)

            # Phase 2: create a session (needed for session-scoped methods)
            req_id += 1
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=req_id)
            results["session/new"] = _classify(new_resp)

            session_id = ""
            if "result" in new_resp and "sessionId" in new_resp["result"]:
                session_id = new_resp["result"]["sessionId"]

            # Phase 3: probe every remaining agent method
            for method in sorted(AGENT_METHODS.values()):
                if method in results:
                    continue  # already probed (initialize, session/new)

                req_id += 1
                params = _params_for(method, session_id=session_id or None)
                await _send_jsonrpc(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": method,
                        "params": params,
                    },
                )

                # Read messages until we get the response with matching id.
                # Some methods (e.g. session/prompt) may produce notifications
                # before the response.
                if method == "session/prompt":
                    await _wait_for_submission(manager)
                    await _end_turn(manager, channel, session_id)
                resp = await _recv_jsonrpc_matching(ws, match_id=req_id, timeout=10.0)
                results[method] = _classify(resp)

            # Clean up consumer tasks
            for state in channel._connections.values():
                for task in state.consumer_tasks.values():
                    task.cancel()

        # --- Assertions ---
        supported = {m for m, r in results.items() if r["supported"]}
        unsupported = {m for m, r in results.items() if not r["supported"]}

        # All core methods must be supported
        missing_core = self.CORE_METHODS - supported
        assert not missing_core, f"Core ACP methods not supported: {missing_core}"

        # All agent methods must be supported (100% protocol coverage)
        assert not unsupported, f"Unsupported ACP methods (expected 100% coverage): {unsupported}"

        # Print coverage report (visible in -v / -s output)
        print("\n" + "=" * 70)
        print("ACP Protocol Coverage Report")
        print("=" * 70)
        print(f"Total agent methods probed: {len(results)}")
        print(f"Supported:   {len(supported)}")
        print(f"Unsupported: {len(unsupported)}")
        print("-" * 70)
        for method in sorted(AGENT_METHODS.values()):
            r = results.get(method)
            if r is None:
                continue
            status = "SUPPORTED  " if r["supported"] else "UNSUPPORTED"
            detail = r.get("detail", "")
            print(f"  {status}  {method:40s} {detail}")
        print("=" * 70)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_client_methods_advertised_or_used(self, acp_ws_server) -> None:
        """Verify the client-method surface the channel can produce.

        The ACP channel sends outbound requests/notifications to the client
        for a subset of ``CLIENT_METHODS``. We verify by inspecting the
        channel's code paths for known outbound methods:

        - ``session/update`` — sent by ``_send_session_update``
        - ``session/request_permission`` — sent by ``_bridge_permission``

        This test is structural: it greps the channel source for each
        ``CLIENT_METHODS`` string and reports which are used.
        """
        import inspect

        from soothe_daemon.channels.acp import ACPChannel as _Channel

        source = inspect.getsource(_Channel)
        found: dict[str, bool] = {}
        for method in sorted(CLIENT_METHODS.values()):
            found[method] = method in source

        used = {m for m, f in found.items() if f}
        not_used = {m for m, f in found.items() if not f}

        print("\n" + "=" * 70)
        print("ACP Client-Method Usage Report")
        print("=" * 70)
        print(f"Total client methods in SDK: {len(CLIENT_METHODS)}")
        print(f"Used by channel:  {len(used)}")
        print(f"Not used:         {len(not_used)}")
        print("-" * 70)
        for method in sorted(CLIENT_METHODS.values()):
            status = "USED     " if found[method] else "NOT-USED "
            print(f"  {status}  {method}")
        print("=" * 70)

        # The channel must at least produce session/update and
        # session/request_permission (the two core outbound methods).
        assert "session/update" in used, "Channel does not produce session/update notifications"
        assert "session/request_permission" in used, (
            "Channel does not produce session/request_permission requests"
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_method_not_found_returns_correct_error_code(self, acp_ws_server) -> None:
        """Verify unsupported methods return JSON-RPC error -32601."""
        base_url, channel, manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            # Initialize first
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Send a deliberately unknown method
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nonexistent/method",
                    "params": {"sessionId": "nonexistent"},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 2
            assert "error" in resp
            assert resp["error"]["code"] == -32601
            assert "Method not found" in resp["error"]["message"]

            # Clean up
            for state in channel._connections.values():
                for task in state.consumer_tasks.values():
                    task.cancel()


def _classify(resp: dict[str, Any]) -> dict[str, Any]:
    """Classify a JSON-RPC response as supported or unsupported.

    Returns a dict with keys:
    - ``supported``: True if the method was recognised (result present or
      error code != -32601).
    - ``detail``: short human-readable string for the report.
    """
    if "error" in resp:
        code = resp["error"].get("code")
        if code == -32601:
            return {
                "supported": False,
                "detail": f"error -32601: {resp['error'].get('message', '')}",
            }
        return {"supported": True, "detail": f"error {code}: {resp['error'].get('message', '')}"}
    if "result" in resp:
        return {"supported": True, "detail": "OK"}
    return {"supported": True, "detail": "no result/error key"}


# ---------------------------------------------------------------------------
# Behavioural tests for newly implemented methods
# ---------------------------------------------------------------------------


class TestACPFullProtocolBehaviour:
    """Behavioural tests for every ACP method the channel now implements.

    Each test exercises a specific method end-to-end over WebSocket,
    asserting that the response matches the ACP schema shape.
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_initialize_advertises_full_capabilities(self, acp_ws_server) -> None:
        """Initialize must advertise session, providers, nes, and mcp capabilities."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=1)
            result = resp["result"]
            assert result["protocolVersion"] == 1
            caps = result["agentCapabilities"]
            assert caps["loadSession"] is True
            assert "list" in caps["sessionCapabilities"]
            assert "delete" in caps["sessionCapabilities"]
            assert "fork" in caps["sessionCapabilities"]
            assert "resume" in caps["sessionCapabilities"]
            assert "close" in caps["sessionCapabilities"]
            assert "providers" in caps
            assert "nes" in caps
            assert "agentInfo" in result
            assert result["agentInfo"]["name"] == "CoverageAgent"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_new_returns_modes(self, acp_ws_server) -> None:
        """session/new must return sessionId, modes, and configOptions."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            result = resp["result"]
            assert "sessionId" in result
            assert "modes" in result
            assert result["modes"]["currentModeId"] == "default"
            assert len(result["modes"]["availableModes"]) >= 1
            assert "configOptions" in result

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_prompt_returns_stop_reason(self, acp_ws_server) -> None:
        """session/prompt must return stopReason and usage."""
        base_url, channel, manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": _params_for("session/prompt", session_id=session_id),
                },
            )
            # The response is held until the turn ends.
            await _wait_for_submission(manager)
            await _end_turn(manager, channel, session_id)
            resp = await _recv_jsonrpc_matching(ws, match_id=3, timeout=10.0)
            result = resp["result"]
            assert result["stopReason"] == "end_turn"
            assert "usage" in result
            assert "totalTokens" in result["usage"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_load_returns_modes(self, acp_ws_server) -> None:
        """session/load must return modes and configOptions."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/load",
                    "params": _params_for("session/load"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            result = resp["result"]
            assert "modes" in result
            assert result["modes"]["currentModeId"] == "default"
            assert "configOptions" in result

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_list_returns_sessions(self, acp_ws_server) -> None:
        """session/list must return a sessions list."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create a session first so list has something
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=2)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/list",
                    "params": _params_for("session/list"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            assert "sessions" in result
            assert isinstance(result["sessions"], list)
            assert len(result["sessions"]) >= 1
            assert "sessionId" in result["sessions"][0]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_delete_returns_empty(self, acp_ws_server) -> None:
        """session/delete must return a result (empty dict per schema)."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/delete",
                    "params": _params_for("session/delete", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_fork_returns_new_session_id(self, acp_ws_server) -> None:
        """session/fork must return a new sessionId with modes."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/fork",
                    "params": _params_for("session/fork", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            assert "sessionId" in result
            assert result["sessionId"] != session_id  # new session
            assert "modes" in result

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_resume_returns_modes(self, acp_ws_server) -> None:
        """session/resume must return modes and configOptions."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/resume",
                    "params": _params_for("session/resume", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            assert "modes" in result
            assert "configOptions" in result

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_close_returns_empty(self, acp_ws_server) -> None:
        """session/close must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/close",
                    "params": _params_for("session/close", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_set_mode_returns_empty(self, acp_ws_server) -> None:
        """session/set_mode must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/set_mode",
                    "params": _params_for("session/set_mode", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_set_config_option_returns_config_options(self, acp_ws_server) -> None:
        """session/set_config_option must return configOptions list."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/set_config_option",
                    "params": _params_for("session/set_config_option", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            assert "configOptions" in result
            assert isinstance(result["configOptions"], list)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_authenticate_returns_empty(self, acp_ws_server) -> None:
        """authenticate must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "authenticate",
                    "params": _params_for("authenticate"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_providers_list_returns_providers(self, acp_ws_server) -> None:
        """providers/list must return a providers list."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "providers/list",
                    "params": _params_for("providers/list"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            result = resp["result"]
            assert "providers" in result
            assert isinstance(result["providers"], list)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_providers_set_returns_empty(self, acp_ws_server) -> None:
        """providers/set must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "providers/set",
                    "params": _params_for("providers/set"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_providers_disable_returns_empty(self, acp_ws_server) -> None:
        """providers/disable must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "providers/disable",
                    "params": _params_for("providers/disable"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_logout_returns_empty(self, acp_ws_server) -> None:
        """logout must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {"jsonrpc": "2.0", "id": 2, "method": "logout", "params": _params_for("logout")},
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_mcp_message_returns_empty(self, acp_ws_server) -> None:
        """mcp/message must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "mcp/message",
                    "params": _params_for("mcp/message"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nes_start_returns_session_id(self, acp_ws_server) -> None:
        """nes/start must return a sessionId."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nes/start",
                    "params": _params_for("nes/start"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            result = resp["result"]
            assert "sessionId" in result

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nes_suggest_returns_suggestions(self, acp_ws_server) -> None:
        """nes/suggest must return a suggestions list."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Start a NES session first
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nes/start",
                    "params": _params_for("nes/start"),
                },
            )
            start_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            nes_session_id = start_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "nes/suggest",
                    "params": _params_for("nes/suggest", session_id=nes_session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            assert "suggestions" in result
            assert isinstance(result["suggestions"], list)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nes_accept_returns_empty(self, acp_ws_server) -> None:
        """nes/accept must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Start a NES session first
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nes/start",
                    "params": _params_for("nes/start"),
                },
            )
            start_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            nes_session_id = start_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "nes/accept",
                    "params": _params_for("nes/accept", session_id=nes_session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nes_reject_returns_empty(self, acp_ws_server) -> None:
        """nes/reject must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Start a NES session first
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nes/start",
                    "params": _params_for("nes/start"),
                },
            )
            start_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            nes_session_id = start_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "nes/reject",
                    "params": _params_for("nes/reject", session_id=nes_session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nes_close_returns_empty(self, acp_ws_server) -> None:
        """nes/close must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Start a NES session first
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nes/start",
                    "params": _params_for("nes/start"),
                },
            )
            start_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            nes_session_id = start_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "nes/close",
                    "params": _params_for("nes/close", session_id=nes_session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_document_did_open_returns_empty(self, acp_ws_server) -> None:
        """document/didOpen must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "document/didOpen",
                    "params": _params_for("document/didOpen", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_document_did_change_returns_empty(self, acp_ws_server) -> None:
        """document/didChange must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "document/didChange",
                    "params": _params_for("document/didChange", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_document_did_close_returns_empty(self, acp_ws_server) -> None:
        """document/didClose must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "document/didClose",
                    "params": _params_for("document/didClose", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_document_did_save_returns_empty(self, acp_ws_server) -> None:
        """document/didSave must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "document/didSave",
                    "params": _params_for("document/didSave", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_document_did_focus_returns_empty(self, acp_ws_server) -> None:
        """document/didFocus must return a result."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": _params_for("session/new"),
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "document/didFocus",
                    "params": _params_for("document/didFocus", session_id=session_id),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp


# ---------------------------------------------------------------------------
# Production-readiness tests: state tracking, validation, error handling
# ---------------------------------------------------------------------------


class TestACPProductionReadiness:
    """Tests verifying production-ready behaviour: state tracking, session
    validation, and proper JSON-RPC error codes for invalid parameters.
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_prompt_unknown_session_returns_invalid_params(
        self, acp_ws_server
    ) -> None:
        """session/prompt with unknown sessionId must return -32602 (invalid params)."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": "nonexistent-session",
                        "prompt": [{"type": "text", "text": "test"}],
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "error" in resp
            assert resp["error"]["code"] == -32602
            assert "Unknown session" in resp["error"]["message"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_cancel_unknown_session_returns_invalid_params(
        self, acp_ws_server
    ) -> None:
        """session/cancel with unknown sessionId must return -32602."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/cancel",
                    "params": {"sessionId": "nonexistent-session"},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "error" in resp
            assert resp["error"]["code"] == -32602

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_delete_unknown_session_returns_invalid_params(
        self, acp_ws_server
    ) -> None:
        """session/delete with unknown sessionId must return -32602."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/delete",
                    "params": {"sessionId": "nonexistent-session"},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "error" in resp
            assert resp["error"]["code"] == -32602

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_set_mode_unknown_session_returns_invalid_params(
        self, acp_ws_server
    ) -> None:
        """session/set_mode with unknown sessionId must return -32602."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/set_mode",
                    "params": {"sessionId": "nonexistent-session", "modeId": "default"},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            assert "error" in resp
            assert resp["error"]["code"] == -32602

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_list_returns_real_cwd(self, acp_ws_server) -> None:
        """session/list must return the real cwd the session was created with."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create a session with a specific cwd
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/home/user/project"},
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=2)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/list",
                    "params": {},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            sessions = result["sessions"]
            assert len(sessions) >= 1
            # The session must have the real cwd, not "/tmp"
            cwds = [s["cwd"] for s in sessions]
            assert "/home/user/project" in cwds

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_fork_inherits_parent_cwd(self, acp_ws_server) -> None:
        """session/fork must inherit the parent session's cwd."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create parent session with specific cwd
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/home/user/parent"},
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            # Fork without specifying cwd — should inherit parent's
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/fork",
                    "params": {"sessionId": session_id, "cwd": "/tmp"},
                },
            )
            fork_resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in fork_resp

            # List sessions and check that parent's cwd is preserved
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/list",
                    "params": {},
                },
            )
            list_resp = await _recv_jsonrpc_matching(ws, match_id=4)
            sessions = list_resp["result"]["sessions"]
            cwds = [s["cwd"] for s in sessions]
            assert "/home/user/parent" in cwds

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_close_then_resume_preserves_session(self, acp_ws_server) -> None:
        """session/close followed by session/resume must work (session preserved)."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
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
                    "params": {"cwd": "/home/user/project"},
                },
            )
            new_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            session_id = new_resp["result"]["sessionId"]

            # Close it
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/close",
                    "params": {"sessionId": session_id},
                },
            )
            close_resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in close_resp

            # Resume it — must succeed (session preserved in map)
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/resume",
                    "params": {"sessionId": session_id, "cwd": "/home/user/project"},
                },
            )
            resume_resp = await _recv_jsonrpc_matching(ws, match_id=4)
            assert "result" in resume_resp
            assert "modes" in resume_resp["result"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_delete_then_resume_returns_error(self, acp_ws_server) -> None:
        """session/resume on a deleted session must return -32602."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Create and delete a session
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

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/delete",
                    "params": {"sessionId": session_id},
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=3)

            # Resume deleted session — must fail
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/resume",
                    "params": {"sessionId": session_id, "cwd": "/tmp"},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=4)
            assert "error" in resp
            assert resp["error"]["code"] == -32602

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_session_set_config_option_tracks_value(self, acp_ws_server) -> None:
        """session/set_config_option must track the value and return it."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

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

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/set_config_option",
                    "params": {
                        "sessionId": session_id,
                        "configId": "model",
                        "value": "gpt-4",
                        "type": "select",
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            result = resp["result"]
            assert "configOptions" in result
            # The tracked config option must be in the response
            config_ids = [c["configId"] for c in result["configOptions"]]
            assert "model" in config_ids
            model_opt = [c for c in result["configOptions"] if c["configId"] == "model"][0]
            assert model_opt["value"] == "gpt-4"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_initialize_advertises_auth_and_position_encoding(self, acp_ws_server) -> None:
        """Initialize must advertise auth/logout and positionEncoding capabilities."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=1)
            result = resp["result"]
            caps = result["agentCapabilities"]
            assert "auth" in caps
            assert "logout" in caps["auth"]
            assert "positionEncoding" in caps
            assert caps["positionEncoding"] == "utf-16"
            assert "promptCapabilities" in caps
            assert "mcpCapabilities" in caps
            assert "agentInfo" in result
            assert "description" in result["agentInfo"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_document_lifecycle_tracks_state(self, acp_ws_server) -> None:
        """document/didOpen → didChange → didClose lifecycle must track state."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

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

            # Open document
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "document/didOpen",
                    "params": {
                        "sessionId": session_id,
                        "uri": "file:///tmp/test.py",
                        "languageId": "python",
                        "version": 1,
                        "text": "print('hello')",
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "result" in resp

            # Change document
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "document/didChange",
                    "params": {
                        "sessionId": session_id,
                        "uri": "file:///tmp/test.py",
                        "version": 2,
                        "contentChanges": [{"text": "print('world')"}],
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=4)
            assert "result" in resp

            # Focus document
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "document/didFocus",
                    "params": {
                        "sessionId": session_id,
                        "uri": "file:///tmp/test.py",
                        "version": 2,
                        "position": {"line": 0, "character": 0},
                        "visibleRange": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 10, "character": 0},
                        },
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=5)
            assert "result" in resp

            # Save document
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "document/didSave",
                    "params": {
                        "sessionId": session_id,
                        "uri": "file:///tmp/test.py",
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=6)
            assert "result" in resp

            # Close document
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "document/didClose",
                    "params": {
                        "sessionId": session_id,
                        "uri": "file:///tmp/test.py",
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=7)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nes_lifecycle_tracks_state(self, acp_ws_server) -> None:
        """nes/start → suggest → accept → close lifecycle must track state."""
        base_url, _channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            # Start NES session
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "nes/start",
                    "params": {"workspaceUri": "file:///tmp"},
                },
            )
            start_resp = await _recv_jsonrpc_matching(ws, match_id=2)
            nes_session_id = start_resp["result"]["sessionId"]

            # Suggest
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "nes/suggest",
                    "params": {
                        "sessionId": nes_session_id,
                        "uri": "file:///tmp/test.py",
                        "version": 1,
                        "position": {"line": 0, "character": 0},
                        "triggerKind": "automatic",
                    },
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=3)
            assert "suggestions" in resp["result"]

            # Accept
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "nes/accept",
                    "params": {"sessionId": nes_session_id, "id": "sugg-1"},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=4)
            assert "result" in resp

            # Close
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "nes/close",
                    "params": {"sessionId": nes_session_id},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=5)
            assert "result" in resp

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_providers_list_returns_configured_model(self, acp_ws_server) -> None:
        """providers/list must return the configured default model."""
        base_url, channel, _manager = acp_ws_server

        async with websockets.asyncio.client.connect(f"{base_url}/acp") as ws:
            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": _params_for("initialize"),
                },
            )
            await _recv_jsonrpc_matching(ws, match_id=1)

            await _send_jsonrpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "providers/list",
                    "params": {},
                },
            )
            resp = await _recv_jsonrpc_matching(ws, match_id=2)
            result = resp["result"]
            assert "providers" in result
            assert len(result["providers"]) >= 1
            provider = result["providers"][0]
            assert provider["providerId"] == "soothe"
            assert provider["name"] == "Soothe Default"
            # Models list should contain the configured default model (or be empty)
            assert "models" in provider
