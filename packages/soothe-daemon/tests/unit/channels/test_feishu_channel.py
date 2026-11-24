"""Unit tests for the Feishu channel inbound path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe_daemon.channels.feishu import FeishuChannel, FeishuConfig


def _group_message_event(
    *,
    message_id: str = "om_1",
    chat_id: str = "oc_group1",
    root_id: str | None = None,
) -> SimpleNamespace:
    """A minimal stand-in for the lark SDK's im.message.receive_v1 event."""
    sender = SimpleNamespace(
        sender_type="user",
        sender_id=SimpleNamespace(open_id="ou_alice"),
    )
    message = SimpleNamespace(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="group",
        message_type="text",
        content='{"text":"hello"}',
        mentions=[],
        parent_id=None,
        root_id=root_id,
        thread_id=None,
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _p2p_message_event(*, message_id: str = "om_dm1") -> SimpleNamespace:
    """A minimal stand-in for a direct (p2p) message event."""
    sender = SimpleNamespace(
        sender_type="user",
        sender_id=SimpleNamespace(open_id="ou_alice"),
    )
    message = SimpleNamespace(
        message_id=message_id,
        chat_id="oc_dm1",
        chat_type="p2p",
        message_type="text",
        content='{"text":"hi"}',
        mentions=[],
        parent_id=None,
        root_id=None,
        thread_id=None,
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _make_channel(topic_isolation: bool = True) -> tuple[FeishuChannel, MagicMock]:
    manager = MagicMock()
    manager.handle_inbound = AsyncMock(return_value="feishu:oc_group1")
    config = FeishuConfig(
        app_id="app",
        app_secret="secret",
        allow_from=["*"],
        group_policy="open",
        topic_isolation=topic_isolation,
    )
    return FeishuChannel(config, manager), manager


class TestFeishuInboundRouting:
    """The group inbound path must reach the manager with a routing key.

    Regression: the first implementation passed a nonexistent ``session_key=``
    kwarg to ``Channel._handle_message``, whose ``except Exception`` swallowed
    the ``TypeError`` — every Feishu inbound message died silently and the
    topic-isolation key was unreachable.
    """

    @pytest.mark.asyncio
    async def test_group_message_reaches_manager_with_session_key(self):
        channel, manager = _make_channel(topic_isolation=True)

        await channel._on_message(_group_message_event())

        manager.handle_inbound.assert_awaited_once()
        kwargs = manager.handle_inbound.await_args.kwargs
        assert kwargs["metadata"]["session_key"] == "feishu:oc_group1:om_1"

    @pytest.mark.asyncio
    async def test_group_message_with_topic_isolation_keys_on_root(self):
        channel, manager = _make_channel(topic_isolation=True)

        await channel._on_message(_group_message_event(root_id="om_root"))

        metadata = manager.handle_inbound.await_args.kwargs["metadata"]
        assert metadata["session_key"] == "feishu:oc_group1:om_root"

    @pytest.mark.asyncio
    async def test_group_message_without_topic_isolation_keys_on_chat(self):
        channel, manager = _make_channel(topic_isolation=False)

        await channel._on_message(_group_message_event(root_id="om_root"))

        metadata = manager.handle_inbound.await_args.kwargs["metadata"]
        assert metadata["session_key"] == "feishu:oc_group1"

    @pytest.mark.asyncio
    async def test_p2p_message_carries_no_session_key(self):
        channel, manager = _make_channel()

        await channel._on_message(_p2p_message_event())

        metadata = manager.handle_inbound.await_args.kwargs["metadata"]
        assert "session_key" not in metadata


class TestFeishuConfigDefaults:
    """Class-level mutable defaults must not be shared across instances."""

    def test_allow_from_default_is_instance_level(self):
        first = FeishuConfig()
        second = FeishuConfig()

        first.allow_from.append("ou_someone")

        assert second.allow_from == []

    def test_allow_from_config_value_is_kept(self):
        assert FeishuConfig(allow_from=["ou_alice"]).allow_from == ["ou_alice"]
