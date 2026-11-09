"""Unit tests for resume topic persistence (TUI /resume picker)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from soothe.sloop.state.resume_topic import (
    derive_resume_topic,
    enforce_topic_word_limit,
    persist_resume_topic_if_needed,
    schedule_resume_topic_persistence,
)


def test_derive_resume_topic_uses_goal_text() -> None:
    assert (
        derive_resume_topic(goal_text="Build the auth module from scratch with tests")
        == "Build the auth module from scratch with tests"
    )


def test_derive_resume_topic_limits_to_ten_words() -> None:
    long_text = " ".join(f"word{i}" for i in range(1, 16))
    topic = derive_resume_topic(goal_text=long_text)
    assert topic is not None
    assert len(topic.split()) == 10
    assert topic == " ".join(f"word{i}" for i in range(1, 11))


def test_enforce_topic_word_limit() -> None:
    assert enforce_topic_word_limit("one two three four five six seven eight nine ten eleven") == (
        "one two three four five six seven eight nine ten"
    )


def test_derive_resume_topic_strips_quotes() -> None:
    assert derive_resume_topic(goal_text='  "Fix auth bug in API"  ') == "Fix auth bug in API"


def test_derive_resume_topic_empty_returns_none() -> None:
    assert derive_resume_topic(goal_text="") is None
    assert derive_resume_topic(goal_text=None) is None


@pytest.mark.asyncio
async def test_persist_resume_topic_if_needed_skips_empty_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager_factory = AsyncMock()
    monkeypatch.setattr(
        "soothe.sloop.checkpoints.manager.StrangeLoopCheckpointPersistenceManager.for_shared_checkpoint_pool",
        manager_factory,
    )

    await persist_resume_topic_if_needed(
        config=SimpleNamespace(),
        loop_id="loop-empty",
        goal_text="",
    )

    manager_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_resume_topic_if_needed_stores_derived_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_once_mock = AsyncMock(return_value=True)
    close_mock = AsyncMock()
    manager = SimpleNamespace(set_resume_topic_once=set_once_mock, close=close_mock)
    manager_factory = AsyncMock(return_value=manager)
    monkeypatch.setattr(
        "soothe.sloop.checkpoints.manager.StrangeLoopCheckpointPersistenceManager.for_shared_checkpoint_pool",
        manager_factory,
    )

    await persist_resume_topic_if_needed(
        config=SimpleNamespace(),
        loop_id="loop-new",
        goal_text="fix failing tests",
    )

    set_once_mock.assert_awaited_once_with("loop-new", "fix failing tests")
    close_mock.assert_awaited_once()


def test_schedule_skips_when_not_first_loop_goal() -> None:
    schedule_resume_topic_persistence(
        config=SimpleNamespace(),
        loop_id="loop-a",
        goal_text="goal text",
        is_first_loop_goal=False,
    )


def test_schedule_skips_when_goal_text_empty() -> None:
    schedule_resume_topic_persistence(
        config=SimpleNamespace(),
        loop_id="loop-b",
        goal_text="",
        is_first_loop_goal=True,
    )
