"""Tests for ReasoningBoundaryMiddleware."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from soothe.events.catalog import LLM_CALL_BOUNDARY
from soothe.sloop.middleware import ReasoningBoundaryMiddleware


def _make_request() -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="sys"),
        tools=[],
        state={},
    )


def _run_awrap(middleware: ReasoningBoundaryMiddleware, request: ModelRequest) -> str:
    """Drive awrap_model_call and return the handler result."""

    async def handler(req: ModelRequest) -> str:
        return "response"

    result = asyncio.run(middleware.awrap_model_call(request, handler))
    return result  # type: ignore[return-value]


def test_boundary_event_dispatched_before_handler() -> None:
    """The boundary event must be dispatched before the handler runs."""
    events: list[dict] = []
    call_order: list[str] = []

    def writer(data: dict) -> None:
        events.append(data)
        call_order.append("event")

    middleware = ReasoningBoundaryMiddleware()
    request = _make_request()

    async def handler(req: ModelRequest) -> str:
        call_order.append("handler")
        return "response"

    with patch("langgraph.config.get_stream_writer", return_value=writer):
        asyncio.run(middleware.awrap_model_call(request, handler))

    assert len(events) == 1
    assert events[0]["type"] == LLM_CALL_BOUNDARY
    assert call_order == ["event", "handler"]


def test_boundary_no_writer_does_not_raise() -> None:
    """Missing stream writer context should be silently skipped."""
    middleware = ReasoningBoundaryMiddleware()
    request = _make_request()

    with patch("langgraph.config.get_stream_writer", side_effect=RuntimeError("no context")):
        result = _run_awrap(middleware, request)
    assert result == "response"


def test_boundary_non_callable_writer_does_not_raise() -> None:
    """A non-callable writer should be silently skipped."""
    middleware = ReasoningBoundaryMiddleware()
    request = _make_request()

    with patch("langgraph.config.get_stream_writer", return_value=None):
        result = _run_awrap(middleware, request)
    assert result == "response"


def test_wrap_model_call_sync_dispatches_event() -> None:
    """The sync wrap_model_call also dispatches the boundary event."""
    events: list[dict] = []

    def writer(data: dict) -> None:
        events.append(data)

    middleware = ReasoningBoundaryMiddleware()
    request = _make_request()

    def handler(req: ModelRequest) -> str:
        return "sync-response"

    with patch("langgraph.config.get_stream_writer", return_value=writer):
        result = middleware.wrap_model_call(request, handler)

    assert result == "sync-response"
    assert len(events) == 1
    assert events[0]["type"] == LLM_CALL_BOUNDARY
