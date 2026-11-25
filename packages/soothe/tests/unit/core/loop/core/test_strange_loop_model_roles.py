"""Tests for loop-phase model role resolution on StrangeLoop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from soothe.config import SootheConfig
from soothe.sloop.strange_loop import StrangeLoop


def test_strange_loop_resolves_goal_synthesis_model_role() -> None:
    cfg = SootheConfig(
        agent={
            "loop": {
                "goal_synthesis_model_role": "fast",
            }
        }
    )
    synthesis_model = MagicMock(name="synthesis")

    with patch.object(
        SootheConfig,
        "create_chat_model",
        side_effect=lambda *args: synthesis_model if args[-1] == "fast" else MagicMock(),
    ):
        loop = StrangeLoop(core_agent=MagicMock(), config=cfg)

    assert loop._goal_synthesis_llm is synthesis_model
    assert loop.goal_synthesis_model() is synthesis_model


def test_goal_synthesis_model_role_defaults_to_fast() -> None:
    """Default synthesis tier is the fast role; the factory falls back to the
    default spec when fast is not configured."""
    assert SootheConfig().agent.loop.goal_synthesis_model_role == "fast"

    fast_model = MagicMock(name="fast-synthesis")
    with patch.object(
        SootheConfig,
        "create_chat_model",
        return_value=fast_model,
    ) as create_mock:
        loop = StrangeLoop(core_agent=MagicMock(), config=SootheConfig())

    assert loop.goal_synthesis_model() is fast_model
    assert create_mock.call_args.args[-1] == "fast"
