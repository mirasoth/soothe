"""Emit an LLM call boundary event before each model invocation.

The TUI step card accumulates reasoning from multiple LLM calls within a
single step into one flat string buffer. Without a call-boundary signal,
reasoning from different calls merges into a single line. This middleware
dispatches a ``soothe.cognition.llm.call.boundary`` custom event via the
graph ``stream_writer`` before each ``awrap_model_call`` / ``wrap_model_call``
so the TUI can insert a line break between reasoning from different calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
)

from soothe.events.catalog import LLM_CALL_BOUNDARY


def _emit_boundary() -> None:
    """Dispatch the boundary event via the LangGraph stream writer."""
    try:
        from langgraph.config import get_stream_writer  # type: ignore[import]

        writer = get_stream_writer()
        if callable(writer):
            writer({"type": LLM_CALL_BOUNDARY})
    except Exception:  # noqa: BLE001 — observability only, never fatal
        pass


class ReasoningBoundaryMiddleware(AgentMiddleware):
    """Dispatch an LLM call boundary event before each model invocation."""

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Emit boundary event before the sync model call."""
        _emit_boundary()
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Emit boundary event before the async model call."""
        _emit_boundary()
        return await handler(request)


__all__ = ["ReasoningBoundaryMiddleware"]
