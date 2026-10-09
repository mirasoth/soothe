"""Coverage-audit policy for StrangeLoop Eval steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)

from soothe.prompts import EVAL_POLICY_SYSTEM_ADDENDUM
from soothe.sloop.decompose import runtime as _decompose_runtime
from soothe.sloop.eval.verdict_tool import build_coverage_verdict_tool
from soothe.sloop.utils.config_keys import SOOTHE_EVAL_STEP_ID_KEY

_COVERAGE_VERDICT_TOOL = build_coverage_verdict_tool()


def _tool_name(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if name is None and isinstance(tool, dict):
        name = tool.get("name")
    return str(name or "")


def _strip_decompose_tool(tools: list[Any]) -> list[Any]:
    return [t for t in tools if _tool_name(t) != "decompose_task"]


def _append_system_addendum(request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
    system = request.system_message
    if system is None or not hasattr(system, "content"):
        return request
    content = system.content
    from langchain_core.messages import SystemMessage

    if isinstance(content, str):
        if EVAL_POLICY_SYSTEM_ADDENDUM in content:
            return request
        return request.override(
            system_message=SystemMessage(content=f"{content}\n\n{EVAL_POLICY_SYSTEM_ADDENDUM}")
        )
    if isinstance(content, list):
        return request.override(
            system_message=SystemMessage(
                content=[
                    *content,
                    {"type": "text", "text": f"\n\n{EVAL_POLICY_SYSTEM_ADDENDUM}"},
                ]
            )
        )
    return request


class EvalStepMiddleware(AgentMiddleware):
    """Coverage-audit policy for Eval steps.

    Keeps the full tool surface so the auditor can run a decisive verification
    command when coverage cannot be confirmed from step history alone. The
    coverage-audit system addendum anchors the thread's role: assess quickly,
    run at most one decisive verification, then emit a binding structured
    verdict via the `coverage_verdict` tool rather than performing work inline
    or writing a prose verdict.
    """

    tools = [_COVERAGE_VERDICT_TOOL]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject `coverage_verdict` on Eval threads; strip it elsewhere."""
        configurable = _decompose_runtime.langgraph_configurable()
        is_eval = bool(configurable.get(SOOTHE_EVAL_STEP_ID_KEY))
        tools = list(request.tools or [])
        names = {_tool_name(tool) for tool in tools}
        if not is_eval:
            # coverage_verdict is Eval-only; strip any stray instance so action
            # threads cannot emit a coverage verdict.
            if "coverage_verdict" in names:
                tools = [t for t in tools if _tool_name(t) != "coverage_verdict"]
                return request.override(tools=tools)
            return request
        # decompose_task is subsumed by coverage_verdict on Eval threads; strip
        # any stray instance so the LLM has a single continuation surface.
        if "decompose_task" in names:
            tools = _strip_decompose_tool(tools)
        if "coverage_verdict" not in {_tool_name(tool) for tool in tools}:
            tools.append(_COVERAGE_VERDICT_TOOL)
        request = (
            request.override(tools=tools) if len(tools) != len(request.tools or []) else request
        )
        return _append_system_addendum(request)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Synchronously apply modify_request then call the handler."""
        return handler(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Asynchronously apply modify_request then await the handler."""
        return await handler(self.modify_request(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """All tools are permitted on Eval threads — but the prompt policy
        constrains usage to at most one decisive verification command."""
        return await handler(request)


__all__ = ["EvalStepMiddleware"]
