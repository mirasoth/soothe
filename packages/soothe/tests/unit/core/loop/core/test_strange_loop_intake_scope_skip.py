"""Client-forced ``intake_scope`` maps to routing task complexity."""

from __future__ import annotations

import pytest
from soothe_nano.protocols.routing import TaskComplexity

from soothe.sloop.utils.routing_scope import build_routing_classification, parse_intake_scope


def test_parse_intake_scope_maps_to_task_complexity() -> None:
    assert parse_intake_scope("minimal") == TaskComplexity.MINIMAL
    assert parse_intake_scope(" SIMPLE ") == TaskComplexity.SIMPLE
    assert parse_intake_scope(None) is None
    assert parse_intake_scope("") is None
    assert parse_intake_scope("   ") is None


@pytest.mark.parametrize("raw", ["chitchat", "huge", "5", ["simple"], object()])
def test_parse_intake_scope_rejects_invalid(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_intake_scope(raw)


def test_build_routing_classification_forced_complexity() -> None:
    routing = build_routing_classification(TaskComplexity.SIMPLE, None)
    assert routing is not None
    assert routing.task_complexity == TaskComplexity.SIMPLE
    assert routing.preferred_subagent is None


def test_build_routing_classification_unforced_is_none() -> None:
    assert build_routing_classification(None, None) is None


def test_build_routing_classification_wire_wins() -> None:
    routing = build_routing_classification(None, "deep_research")
    assert routing is not None
    assert routing.preferred_subagent == "deep_research"
    assert routing.routing_hint == "subagent"
    assert routing.task_complexity == TaskComplexity.COMPLEX
