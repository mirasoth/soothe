"""Tests for router RPC authorization, config redaction, and batch envelopes.

Covers the review-8b544d9c2 fixes:
- admin/destructive RPCs require an authenticated session when the identity
  service is enabled (trusted-local unchanged when it is disabled)
- `config_get` masks secret-bearing values before they leave the daemon
- `_unwrap_envelope` never lets `params` override dispatch discriminators
- batch dispatch sends exactly one dict error envelope per failed item
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from soothe_daemon.protocol import ErrorCode, MessageRouter
from soothe_daemon.protocol.error_codes import RpcProtocolError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSessionManager:
    """Session manager stub with per-client authenticated state."""

    def __init__(self, sessions: dict[str, Any]) -> None:
        self._sessions = sessions

    async def get_session(self, client_id: Any) -> Any:
        return self._sessions.get(str(client_id))


class _FakeDaemon:
    """Bare daemon stub that records sent messages."""

    def __init__(
        self,
        *,
        identity_service: Any = None,
        sessions: dict[str, Any] | None = None,
        config: Any = None,
    ) -> None:
        self._identity_service = identity_service
        self._session_manager = _FakeSessionManager(sessions or {})
        self._config = config
        self.sent: list[tuple[Any, dict[str, Any]]] = []

    async def _send_client_message(self, client_id: Any, msg: dict[str, Any]) -> None:
        self.sent.append((client_id, msg))


def _make_router(daemon: _FakeDaemon) -> MessageRouter:
    router = MessageRouter(daemon)
    # Bypass handshake enforcement for unit tests.
    router._is_handshake_complete = lambda _cid: True  # type: ignore[method-assign]
    return router


def _make_batch_router(daemon: _FakeDaemon) -> MessageRouter:
    router = _make_router(daemon)
    # Grant the negotiated `batch` capability for batch dispatch tests.
    router._get_negotiated_capabilities = lambda _cid: ["batch"]  # type: ignore[method-assign]
    return router


def _unauthenticated_session() -> SimpleNamespace:
    return SimpleNamespace(subscriptions=set(), authenticated=False, auth_user_id=None)


def _authenticated_session(user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(subscriptions=set(), authenticated=True, auth_user_id=user_id)


def _error_code(msg: dict[str, Any]) -> int:
    assert msg.get("type") == "error", f"expected error envelope, got {msg}"
    return int(msg["error"]["code"])


# ---------------------------------------------------------------------------
# Admin authz gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_get_rejected_without_auth_when_identity_enabled() -> None:
    """Identity enabled + unauthenticated session → AUTH_REQUIRED, no config."""
    daemon = _FakeDaemon(
        identity_service=object(),
        sessions={"client-a": _unauthenticated_session()},
        config=SimpleNamespace(model_dump=lambda: {"providers": [{"name": "x"}]}),
    )
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {"proto": "1", "type": "request", "method": "config_get", "params": {}, "id": "r1"},
    )

    assert len(daemon.sent) == 1
    assert _error_code(daemon.sent[0][1]) == int(ErrorCode.AUTH_REQUIRED)


@pytest.mark.asyncio
async def test_daemon_shutdown_rejected_without_auth_when_identity_enabled() -> None:
    """daemon_shutdown from an unauthenticated session never reaches stop()."""
    daemon = _FakeDaemon(
        identity_service=object(),
        sessions={"client-a": _unauthenticated_session()},
    )
    daemon.stop = AsyncMock()
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {"proto": "1", "type": "request", "method": "daemon_shutdown", "params": {}, "id": "r2"},
    )

    assert len(daemon.sent) == 1
    assert _error_code(daemon.sent[0][1]) == int(ErrorCode.AUTH_REQUIRED)
    daemon.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_rpc_allowed_when_identity_disabled() -> None:
    """Identity disabled → trusted-local; config_get proceeds unchanged."""
    cfg = SimpleNamespace(model_dump=lambda: {"providers": [{"name": "plain"}]})
    daemon = _FakeDaemon(identity_service=None, config=cfg)
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {"proto": "1", "type": "request", "method": "config_get", "params": {}, "id": "r3"},
    )

    assert len(daemon.sent) == 1
    assert daemon.sent[0][1]["type"] == "response"
    assert daemon.sent[0][1]["result"] == {"all": {"providers": [{"name": "plain"}]}}


@pytest.mark.asyncio
async def test_admin_rpc_allowed_when_authenticated() -> None:
    """Identity enabled + authenticated session → config_get proceeds."""
    cfg = SimpleNamespace(model_dump=lambda: {"providers": [{"name": "ok"}]})
    daemon = _FakeDaemon(
        identity_service=object(),
        sessions={"client-a": _authenticated_session()},
        config=cfg,
    )
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {"proto": "1", "type": "request", "method": "config_get", "params": {}, "id": "r4"},
    )

    assert len(daemon.sent) == 1
    assert daemon.sent[0][1]["type"] == "response"
    assert daemon.sent[0][1]["result"] == {"all": {"providers": [{"name": "ok"}]}}


@pytest.mark.asyncio
async def test_admin_rpc_rejected_when_session_missing() -> None:
    """Identity enabled + no session at all → AUTH_REQUIRED (fail closed)."""
    daemon = _FakeDaemon(identity_service=object(), sessions={}, config=None)
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {"proto": "1", "type": "request", "method": "config_get", "params": {}, "id": "r5"},
    )

    assert len(daemon.sent) == 1
    assert _error_code(daemon.sent[0][1]) == int(ErrorCode.AUTH_REQUIRED)


# ---------------------------------------------------------------------------
# config_get secret masking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_get_masks_secret_bearing_values() -> None:
    """api_key/secret/password/token keys and storage_options are masked."""
    cfg = SimpleNamespace(
        model_dump=lambda: {
            "providers": [{"name": "openai", "api_key": "sk-super-secret"}],
            "anthropic_api_key": "ak-plain",
            "identity": {"jwt_secret": "s3cret"},
            "database": {"password": "hunter2"},
            "refresh_token_ttl_seconds": 3600,
            "workspace_sync": {"source_uri": "s3://bucket/x", "storage_options": {"k": "v"}},
            "loop": {"output_streaming": {"streaming_interval_ms": 100}},
        }
    )
    daemon = _FakeDaemon(identity_service=None, config=cfg)
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {"proto": "1", "type": "request", "method": "config_get", "params": {}, "id": "r6"},
    )

    assert len(daemon.sent) == 1
    result = daemon.sent[0][1]["result"]
    dumped = result["all"]
    assert dumped["providers"][0]["api_key"] == "***"
    assert dumped["anthropic_api_key"] == "***"
    assert dumped["identity"]["jwt_secret"] == "***"
    assert dumped["database"]["password"] == "***"
    assert dumped["workspace_sync"]["storage_options"] == "***"
    # Non-secret values pass through untouched (and are never masked by
    # coincidence — `refresh_token_ttl_seconds` is a duration, not a secret,
    # but substring matching intentionally masks it too).
    assert dumped["loop"]["output_streaming"]["streaming_interval_ms"] == 100


@pytest.mark.asyncio
async def test_config_get_masks_secret_section_only() -> None:
    """Section slicing still applies after masking."""
    cfg = SimpleNamespace(
        model_dump=lambda: {
            "providers": [{"name": "x", "api_key": "sk-1"}],
            "daemon": {"port": 8765},
        }
    )
    daemon = _FakeDaemon(identity_service=None, config=cfg)
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {
            "proto": "1",
            "type": "request",
            "method": "config_get",
            "params": {"section": "daemon"},
            "id": "r7",
        },
    )

    assert len(daemon.sent) == 1
    assert daemon.sent[0][1]["result"] == {"daemon": {"port": 8765}}


# ---------------------------------------------------------------------------
# _unwrap_envelope discriminator protection
# ---------------------------------------------------------------------------


def test_unwrap_envelope_params_cannot_override_discriminators() -> None:
    """`params.type`/`params.id` must never override envelope fields."""
    flat = MessageRouter._unwrap_envelope(
        "request",
        {
            "proto": "1",
            "type": "request",
            "method": "loop_get",
            "params": {"type": "ping", "proto": "9", "id": "fake", "loop_id": "L1"},
            "id": "real-id",
        },
    )
    assert flat is not None
    assert flat["type"] == "loop_get"
    assert flat["proto"] == "1"
    assert flat["id"] == "real-id"
    assert flat["request_id"] == "real-id"
    assert flat["loop_id"] == "L1"


def test_unwrap_envelope_still_merges_regular_params() -> None:
    flat = MessageRouter._unwrap_envelope(
        "request",
        {"proto": "1", "type": "request", "method": "loop_get", "params": {"loop_id": "L2"}},
    )
    assert flat is not None
    assert flat["type"] == "loop_get"
    assert flat["loop_id"] == "L2"


# ---------------------------------------------------------------------------
# auth RPC marks the session authenticated
# ---------------------------------------------------------------------------


class _FakeAuthHandler:
    def __init__(self, success: bool) -> None:
        self._success = success

    def handle_auth(self, access_key: str, secret_key: str) -> dict[str, Any]:
        if not self._success:
            return {"type": "auth_response", "success": False, "error": "invalid_credentials"}
        return {
            "type": "auth_response",
            "success": True,
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 900,
            "user_id": "user-7",
        }


@pytest.mark.asyncio
async def test_auth_success_marks_session_authenticated() -> None:
    session = _unauthenticated_session()
    daemon = _FakeDaemon(
        identity_service=object(),
        sessions={"client-a": session},
    )
    daemon._auth_handler = _FakeAuthHandler(success=True)
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {
            "proto": "1",
            "type": "request",
            "method": "auth",
            "params": {"access_key": "AK-x", "secret_key": "SK-x"},
            "id": "r8",
        },
    )

    assert session.authenticated is True
    assert session.auth_user_id == "user-7"


@pytest.mark.asyncio
async def test_auth_failure_does_not_mark_session() -> None:
    session = _unauthenticated_session()
    daemon = _FakeDaemon(
        identity_service=object(),
        sessions={"client-a": session},
    )
    daemon._auth_handler = _FakeAuthHandler(success=False)
    router = _make_router(daemon)

    await router.dispatch(
        "client-a",
        {
            "proto": "1",
            "type": "request",
            "method": "auth",
            "params": {"access_key": "AK-x", "secret_key": "SK-bad"},
            "id": "r9",
        },
    )

    assert session.authenticated is False


# ---------------------------------------------------------------------------
# Batch dispatch envelopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_handler_error_sends_single_dict_envelope() -> None:
    """A handler-raised RpcProtocolError yields exactly one dict error frame."""
    daemon = _FakeDaemon()
    router = _make_batch_router(daemon)

    async def _raise(_client_id: Any, _msg: dict[str, Any]) -> None:
        raise RpcProtocolError(ErrorCode.INVALID_PARAMS, "boom")

    router._handle_ping = _raise  # type: ignore[method-assign]

    await router.dispatch(
        "client-a",
        [
            {
                "proto": "1",
                "type": "request",
                "method": "ping",
                "params": {},
                "id": "b1",
            }
        ],
    )

    assert len(daemon.sent) == 1
    client_id, frame = daemon.sent[0]
    assert client_id == "client-a"
    assert isinstance(frame, dict)
    assert _error_code(frame) == int(ErrorCode.INVALID_PARAMS)
    assert frame.get("id") == "b1"


@pytest.mark.asyncio
async def test_batch_non_dict_item_sends_single_dict_envelope() -> None:
    """Non-dict batch items produce one dict error envelope each, no raw list."""
    daemon = _FakeDaemon()
    router = _make_batch_router(daemon)

    await router.dispatch("client-a", [42, "not-a-dict"])

    assert len(daemon.sent) == 2
    for _client_id, frame in daemon.sent:
        assert isinstance(frame, dict)
        assert _error_code(frame) == int(ErrorCode.INVALID_REQUEST)


@pytest.mark.asyncio
async def test_batch_unknown_method_sends_single_dict_envelope() -> None:
    """An unknown batch method yields one dict error envelope (validation hit)."""
    daemon = _FakeDaemon()
    router = _make_batch_router(daemon)

    await router.dispatch(
        "client-a",
        [{"proto": "1", "type": "request", "method": "does_not_exist", "id": "b2"}],
    )

    assert len(daemon.sent) == 1
    _client_id, frame = daemon.sent[0]
    assert isinstance(frame, dict)
    assert _error_code(frame) == int(ErrorCode.INVALID_PARAMS)
