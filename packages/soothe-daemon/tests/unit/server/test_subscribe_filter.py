"""Tests that subscribe_loop accepts all loop_ids without autopilot filtering.

The autopilot worker prefix filter (RFC-222) has been removed along with the
``autopilot_subscribed`` bypass. Subscriptions to any loop_id — including
``autopilot__*`` prefixed IDs — are now handled by the normal subscription
path without special gating.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from soothe_daemon.event import EventBus
from soothe_daemon.server.session import ClientSessionManager


@pytest.mark.asyncio
async def test_subscribe_allows_autopilot_worker_loop_id() -> None:
    """autopilot__* IDs are no longer rejected; they subscribe like any loop."""
    bus = EventBus()
    manager = ClientSessionManager(bus)
    transport = MagicMock()
    transport.name = "test"
    client_id = await manager.create_session(transport, None)

    result = await manager.subscribe_loop(client_id, "autopilot__w001")

    assert result is True
    session = await manager.get_session(client_id)
    assert session is not None
    assert "autopilot__w001" in session.subscriptions

    await manager.remove_session(client_id)


@pytest.mark.asyncio
async def test_subscribe_allows_normal_loop_ids() -> None:
    bus = EventBus()
    manager = ClientSessionManager(bus)
    transport = MagicMock()
    transport.name = "test"
    client_id = await manager.create_session(transport, None)

    result = await manager.subscribe_loop(client_id, "user-loop-123")

    assert result is True
    session = await manager.get_session(client_id)
    assert session is not None
    assert "user-loop-123" in session.subscriptions

    await manager.remove_session(client_id)


@pytest.mark.asyncio
async def test_subscribe_allows_any_autopilot_prefix_variant() -> None:
    """All autopilot__* variants subscribe successfully (filter removed)."""
    bus = EventBus()
    manager = ClientSessionManager(bus)
    transport = MagicMock()
    transport.name = "test"
    client_id = await manager.create_session(transport, None)

    for loop_id in ("autopilot__w999", "autopilot__w042"):
        assert await manager.subscribe_loop(client_id, loop_id) is True

    await manager.remove_session(client_id)
