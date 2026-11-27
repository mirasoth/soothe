"""Tests for the `daemon_info` RPC handler (IG-DAEM-004)."""

from __future__ import annotations

from typing import Any

import pytest

from soothe_daemon.protocol import MessageRouter


class _FakeDaemon:
    """Bare daemon stub exposing `daemon_info` and recording sent messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, dict[str, Any]]] = []
        self._started_at = "2026-11-27T00:00:00+00:00"
        self._readiness_state = "ready"

    async def _send_client_message(self, client_id: Any, msg: dict[str, Any]) -> None:
        self.sent.append((client_id, msg))

    def daemon_info(self) -> dict[str, Any]:
        import os

        from soothe_daemon import __version__

        return {
            "daemon_id": f"{os.getpid()}:{self._started_at}",
            "pid": os.getpid(),
            "started_at": self._started_at,
            "version": __version__,
            "readiness_state": self._readiness_state,
        }


@pytest.mark.asyncio
async def test_daemon_info_returns_stable_per_process_identity() -> None:
    daemon = _FakeDaemon()
    router = MessageRouter(daemon)

    await router._handle_daemon_info("client-1", {"request_id": "req-1"})

    client_id, envelope = daemon.sent[0]
    assert client_id == "client-1"
    assert envelope["type"] == "response"
    assert envelope["id"] == "req-1"
    result = envelope["result"]
    assert result["daemon_id"] == f"{result['pid']}:{daemon._started_at}"
    assert result["started_at"] == daemon._started_at
    assert result["readiness_state"] == "ready"
    assert isinstance(result["version"], str)


@pytest.mark.asyncio
async def test_daemon_info_identity_differs_across_restarts() -> None:
    first = _FakeDaemon()
    second = _FakeDaemon()
    second._started_at = "2026-11-27T01:00:00+00:00"

    assert first.daemon_info()["daemon_id"] != second.daemon_info()["daemon_id"]
