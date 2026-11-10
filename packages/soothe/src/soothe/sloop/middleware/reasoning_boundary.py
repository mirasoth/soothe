"""Emit an LLM call boundary event before each model invocation.

The TUI step card accumulates reasoning from multiple LLM calls within a
single step into one flat string buffer. Without a call-boundary signal,
reasoning from different calls merges into a single line. This middleware
dispatches a ``soothe.cognition.llm.call.boundary`` custom event via the
graph ``stream_writer`` before each ``awrap_model_call`` / ``wrap_model_call``
so the TUI can insert a line break between reasoning from different calls.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
)

from soothe.events.catalog import LLM_CALL_BOUNDARY

logger = logging.getLogger(__name__)


def _emit_boundary(request: ModelRequest[Any]) -> None:
    """Dispatch the boundary event via ``request.runtime.stream_writer``."""
    stream_writer = getattr(getattr(request, "runtime", None), "stream_writer", None)
    if not callable(stream_writer):
        return
    thread_id = ""
    try:
        config = getattr(getattr(request, "runtime", None), "config", None)
        if isinstance(config, dict):
            configurable = config.get("configurable")
            if isinstance(configurable, dict):
                thread_id = str(configurable.get("thread_id") or "")
    except Exception:  # noqa: BLE001 — observability only
        pass
    try:
        stream_writer({"type": LLM_CALL_BOUNDARY, "thread_id": thread_id})
    except Exception:  # noqa: BLE001 — observability only, never fatal
        logger.debug("Failed to emit LLM call boundary event", exc_info=True)


class ReasoningBoundaryMiddleware(AgentMiddleware):
    """Dispatch an LLM call boundary event before each model invocation."""

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Emit boundary event before the sync model call."""
        _emit_boundary(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Emit boundary event before the async model call."""
        _emit_boundary(request)
        return await handler(request)


__all__ = ["ReasoningBoundaryMiddleware"]
