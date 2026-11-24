"""Unit tests for DingTalk channel (permission gate ordering + Channel surface)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from soothe_daemon.channels.base import Channel
from soothe_daemon.channels.dingtalk import (
    AckMessage,
    DingTalkChannel,
    SootheDingTalkHandler,
)


class RecordingManager:
    """Minimal manager double that records routed inbound messages."""

    def __init__(self) -> None:
        self.inbound: list[dict] = []

    async def handle_inbound(self, **kwargs):
        self.inbound.append(kwargs)
        return "loop-1"


def _make_channel(allow_from: list[str]) -> tuple[DingTalkChannel, list]:
    """Build a DingTalkChannel with a recording download hook."""
    manager = RecordingManager()
    channel = DingTalkChannel(
        {
            "enabled": True,
            "client_id": "id",
            "client_secret": "sec",
            "allow_from": allow_from,
        },
        manager,
    )
    downloads: list[tuple[str, str, str]] = []

    async def _record_download(download_code, filename, sender_id):
        downloads.append((download_code, filename, sender_id))
        return "/tmp/fake-file"

    channel._download_dingtalk_file = _record_download  # type: ignore[method-assign]
    return channel, downloads


def _picture_update(sender: str) -> SimpleNamespace:
    """Build a fake stream callback message carrying an image download."""
    return SimpleNamespace(
        data={
            "msgtype": "picture",
            "content": {"downloadCode": "dc-1"},
            "senderStaffId": sender,
            "senderId": sender,
            "senderNick": "Nick",
            "conversationType": "1",
            "conversationId": "cid-1",
        }
    )


def _run_process(channel: DingTalkChannel, message: SimpleNamespace):
    """Run handler.process and flush the spawned _on_message task."""

    async def _scenario():
        handler = SootheDingTalkHandler(channel)
        result = await handler.process(message)
        tasks = list(channel._background_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        return result

    return asyncio.run(_scenario())


class TestPreAuthPermissionGate:
    """The allow_from check must run before any media download."""

    def test_unauthorized_sender_does_not_trigger_download(self):
        channel, downloads = _make_channel(allow_from=["staff-1"])
        status, _reply = _run_process(channel, _picture_update("intruder"))

        assert downloads == []
        assert channel._manager.inbound == []
        assert status == AckMessage.STATUS_OK

    def test_authorized_sender_downloads_then_publishes(self):
        channel, downloads = _make_channel(allow_from=["staff-1"])
        _run_process(channel, _picture_update("staff-1"))

        assert downloads == [("dc-1", "image.jpg", "staff-1")]
        assert len(channel._manager.inbound) == 1
        assert channel._manager.inbound[0]["chat_id"] == "staff-1"
        assert "[Image]" in channel._manager.inbound[0]["content"]


class TestChannelSurface:
    """DingTalkChannel must be a regular Channel subclass for the registry."""

    def test_inherits_from_channel(self):
        assert issubclass(DingTalkChannel, Channel)

    def test_is_allowed_uses_base_allow_list(self):
        channel, _downloads = _make_channel(allow_from=["staff-1", "*"])
        assert channel.is_allowed("staff-1") is True
        assert channel.is_allowed("anyone") is True  # wildcard entry

        channel_strict, _downloads = _make_channel(allow_from=["staff-1"])
        assert channel_strict.is_allowed("staff-1") is True
        assert channel_strict.is_allowed("intruder") is False
