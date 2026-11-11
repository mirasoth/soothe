"""Tests that live goal tree state stays off the message transcript."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe_cli.tui.textual_adapter import TextualUIAdapter, _ensure_goal_tree_message


@pytest.mark.asyncio
async def test_ensure_goal_tree_message_does_not_mount_to_messages() -> None:
    """Goal/plan aggregate is kept on the adapter for Ctrl+t only."""
    mount = AsyncMock()
    adapter = TextualUIAdapter(mount_message=mount, update_status=MagicMock())

    tree = await _ensure_goal_tree_message(adapter, goal="Ship the feature")

    mount.assert_not_called()
    assert adapter._goal_tree_message is tree
    assert tree._goal_text == "Ship the feature"
    assert not getattr(tree, "is_mounted", False)
