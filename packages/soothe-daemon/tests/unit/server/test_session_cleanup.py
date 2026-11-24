"""Tests for client-session teardown and subscription bookkeeping hygiene."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from soothe_daemon.event import EventBus
from soothe_daemon.server.session import ClientSessionManager


def _transport() -> MagicMock:
    transport = MagicMock()
    transport.name = "test"
    return transport


@pytest.mark.asyncio
async def test_subscribe_loop_replaces_subscription_ids() -> None:
    """Re-subscribing to a different loop must drop the old subscription id."""
    bus = EventBus()
    manager = ClientSessionManager(bus)
    client_id = await manager.create_session(_transport(), None)

    assert await manager.subscribe_loop(client_id, "loop-a", subscription_id="sub-a") is True
    assert await manager.subscribe_loop(client_id, "loop-b", subscription_id="sub-b") is True

    session = await manager.get_session(client_id)
    assert session is not None
    assert session.subscriptions == {"loop-b"}
    assert session.loop_subscription_ids == {"loop-b": "sub-b"}

    await manager.remove_session(client_id)


@pytest.mark.asyncio
async def test_subscribe_loop_without_id_clears_stale_subscription_id() -> None:
    bus = EventBus()
    manager = ClientSessionManager(bus)
    client_id = await manager.create_session(_transport(), None)

    await manager.subscribe_loop(client_id, "loop-a", subscription_id="sub-a")
    await manager.subscribe_loop(client_id, "loop-a")

    session = await manager.get_session(client_id)
    assert session is not None
    assert session.loop_subscription_ids == {}

    await manager.remove_session(client_id)


@pytest.mark.asyncio
async def test_remove_session_clears_delivery_state_for_all_loops() -> None:
    """Delivery counters from *replaced* loop subscriptions are also cleared."""
    bus = EventBus()
    manager = ClientSessionManager(bus)
    client_id = await manager.create_session(_transport(), None)

    await manager.subscribe_loop(client_id, "loop-a")
    manager.note_delivery_sent("loop-a", client_id, units=3)
    manager.record_delivery_ack(client_id, "loop-a", 3)

    # Replace the subscription — loop-a counters now belong to a gone topic.
    await manager.subscribe_loop(client_id, "loop-b")
    manager.note_delivery_sent("loop-b", client_id, units=1)

    # Simulate a crash between teardown steps: the disconnect path must wipe
    # ALL (loop, client) delivery state, not only still-subscribed loops.
    await manager.remove_session(client_id)

    assert ("loop-a", client_id) not in manager._delivery_sent_seq
    assert ("loop-a", client_id) not in manager._delivery_ack_seq
    assert ("loop-b", client_id) not in manager._delivery_sent_seq
    assert ("loop-b", client_id) not in manager._delivery_ack_seq


@pytest.mark.asyncio
async def test_remove_session_clears_subscription_bookkeeping() -> None:
    bus = EventBus()
    manager = ClientSessionManager(bus)
    client_id = await manager.create_session(_transport(), None)

    await manager.subscribe_loop(client_id, "loop-a", subscription_id="sub-a")
    await manager.remove_session(client_id)

    assert await manager.get_session(client_id) is None
    # No stale (loop, client) delivery counters left behind.
    assert all(k[1] != client_id for k in manager._delivery_sent_seq)
    assert all(k[1] != client_id for k in manager._delivery_ack_seq)
