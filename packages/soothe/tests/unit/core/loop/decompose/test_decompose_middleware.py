"""Tests for DecomposeTaskMiddleware tool injection."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from soothe.prompts import (
    CONTEXT_PRESSURE_SPLIT_HINT,
    PARALLEL_NUDGE_ADDENDUM,
    SPLIT_CANDIDATE_ADDENDUM,
)
from soothe.sloop.decompose.middleware import reset_decompose_injection_log_state
from soothe.sloop.decompose.runtime import bind_decompose_runtime, reset_decompose_runtime
from soothe.sloop.middleware import DecomposeTaskMiddleware
from soothe.sloop.utils.config_keys import (
    SOOTHE_DECOMPOSE_STEP_ID_KEY,
    SOOTHE_INTERACTION_MODE_KEY,
    SOOTHE_IS_DAG_ROOT_KEY,
    SOOTHE_STEP_DESCRIPTION_KEY,
    SOOTHE_TASK_COMPLEXITY_KEY,
    SOOTHE_USER_GOAL_KEY,
)

_CONFIGURABLE = "soothe.sloop.decompose.runtime.langgraph_configurable"
_TYPESAFE = "soothe.sloop.decompose.decompose_typesafe.decide_finish_vs_split_typesafe"


def _request() -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        messages=[HumanMessage(content="do the work")],
        system_message=SystemMessage(content="orig"),
        tools=[SimpleNamespace(name="read_file")],
        state={},
    )


def _tool_names(request: ModelRequest) -> list[str]:
    return [getattr(t, "name", None) for t in (request.tools or [])]


def _run_through_hook(middleware: DecomposeTaskMiddleware, request: ModelRequest) -> ModelRequest:
    """Drive the real langchain hook so a dead hook name fails the test."""
    seen: dict[str, ModelRequest] = {}

    async def handler(req: ModelRequest) -> str:
        seen["request"] = req
        return "response"

    asyncio.run(middleware.awrap_model_call(request, handler))
    return seen["request"]


def test_awrap_model_call_injects_decompose_tool() -> None:
    sink: list = []
    tokens = bind_decompose_runtime(step_id="AAA-01", sink=sink)
    try:
        with patch(_CONFIGURABLE, return_value={}):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    finally:
        reset_decompose_runtime(tokens)

    assert "decompose_task" in _tool_names(forwarded)
    content = forwarded.system_message.content
    assert "This thread: finish vs split" in content
    assert "write_todos (this thread only)" in content


def test_injection_uses_configurable_step_id_without_contextvar() -> None:
    conf = {SOOTHE_DECOMPOSE_STEP_ID_KEY: "BBB-02"}
    with patch(_CONFIGURABLE, return_value=conf):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())

    assert "decompose_task" in _tool_names(forwarded)


def test_no_injection_without_step_binding() -> None:
    with patch(_CONFIGURABLE, return_value={}):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())

    assert "decompose_task" not in _tool_names(forwarded)


def test_registered_tool_is_hidden_on_ungated_threads() -> None:
    """Registered middleware tools reach every request; step gate must hide them."""
    base = _request()
    request = base.override(tools=[*(base.tools or []), *DecomposeTaskMiddleware.tools])

    with patch(_CONFIGURABLE, return_value={}):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), request)

    assert "decompose_task" not in _tool_names(forwarded)


def test_tool_is_registered_for_the_agent_tool_node() -> None:
    """Without registration the tool node rejects the call at execution time."""
    assert [getattr(t, "name", None) for t in DecomposeTaskMiddleware.tools] == ["decompose_task"]


# ── Soft parallelization nudge (complex root steps) ─────────────────────────


def _root_conf(task_complexity: str | None = "complex") -> dict:
    """Configurable for a root step with the given task complexity."""
    conf: dict = {SOOTHE_DECOMPOSE_STEP_ID_KEY: "ROOT-01", SOOTHE_IS_DAG_ROOT_KEY: True}
    if task_complexity is not None:
        conf[SOOTHE_TASK_COMPLEXITY_KEY] = task_complexity
    return conf


def test_complex_root_step_gets_parallel_nudge() -> None:
    with patch(_CONFIGURABLE, return_value=_root_conf("complex")):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM in forwarded.system_message.content


def test_simple_root_step_no_nudge() -> None:
    with patch(_CONFIGURABLE, return_value=_root_conf("simple")):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content


def test_minimal_root_step_no_nudge() -> None:
    with patch(_CONFIGURABLE, return_value=_root_conf("minimal")):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content


def test_child_step_no_nudge_even_if_complex() -> None:
    """is_dag_root guard: child steps must not get the nudge (no recursion)."""
    conf = _root_conf("complex")
    conf[SOOTHE_IS_DAG_ROOT_KEY] = False
    with patch(_CONFIGURABLE, return_value=conf):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content


def test_root_step_without_task_complexity_no_nudge() -> None:
    with patch(_CONFIGURABLE, return_value=_root_conf(None)):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content


def test_plan_mode_suppresses_nudge_even_if_complex_root() -> None:
    conf = _root_conf("complex")
    conf[SOOTHE_INTERACTION_MODE_KEY] = "plan"
    with patch(_CONFIGURABLE, return_value=conf):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content


def test_ask_mode_suppresses_nudge_even_if_complex_root() -> None:
    conf = _root_conf("complex")
    conf[SOOTHE_INTERACTION_MODE_KEY] = "ask"
    with patch(_CONFIGURABLE, return_value=conf):
        forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
    assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content


def test_nudge_prefers_fanout_but_allows_finish_exception() -> None:
    """Nudge prefers parallel fan-out (dominant signal) with a narrow finish
    exception. Regression guard: must NOT use unconditional mandatory language
    ('must' / 'before doing any work') that caused the DECOMPOSE_FIRST_HINT
    pass-through chain; instead it frames fan-out as preferred with an
    explicit cohesion escape hatch."""
    nudge = PARALLEL_NUDGE_ADDENDUM
    # Dominant signal: fan-out is the preferred path.
    assert "prefer" in nudge.lower()
    assert "decompose_task" in nudge
    # Escape hatch: finish-in-thread is allowed for cohesive work.
    assert "cohesive" in nudge.lower() or "single" in nudge.lower()
    # Regression guard: no unconditional mandatory split language.
    assert "must" not in nudge.lower()
    # 'NOW' is allowed only in the conditional fan-out branch, not as an
    # unconditional order — verify it sits inside a "when you see" clause.
    if "now" in nudge.lower():
        assert "when" in nudge.lower() or "if" in nudge.lower()
    # Evidence-first: the nudge must require gathering evidence before fan-out
    # (self-hygiene — decomposing without grounding wastes child-thread budget
    # when the model fabricates non-existent areas).
    assert any(word in nudge.lower() for word in ("evidence", "confirm", "ls", "glob", "grep")), (
        "nudge must require gathering evidence before decompose"
    )


def test_nudge_idempotent_on_repeat_hook() -> None:
    """Running the hook twice on the same request must not duplicate the nudge."""
    with patch(_CONFIGURABLE, return_value=_root_conf("complex")):
        once = _run_through_hook(DecomposeTaskMiddleware(), _request())
        twice = _run_through_hook(DecomposeTaskMiddleware(), once)
    assert twice.system_message.content.count(PARALLEL_NUDGE_ADDENDUM) == 1


# ── Injection log dedup (one DEBUG line per step) ───────────────────────────


def test_injection_log_fires_once_per_step(caplog: pytest.LogCaptureFixture) -> None:
    """``modify_request`` runs on every model call, so without per-step dedup
    a single step floods the DEBUG log with identical "injecting
    decompose_task" lines (1549× observed in one loop). The injection must
    still happen every call (tool stays reachable) but the log fires once.
    """
    reset_decompose_injection_log_state()
    caplog.set_level(logging.DEBUG, logger="soothe.sloop.decompose.middleware")
    middleware = DecomposeTaskMiddleware()
    conf = {SOOTHE_DECOMPOSE_STEP_ID_KEY: "DEDUP-01"}
    with patch(_CONFIGURABLE, return_value=conf):
        for _ in range(5):
            forwarded = _run_through_hook(middleware, _request())
            # Tool stays reachable on every call.
            assert "decompose_task" in _tool_names(forwarded)

    injection_logs = [r for r in caplog.records if "injecting decompose_task" in r.getMessage()]
    assert len(injection_logs) == 1, (
        f"expected one injection log per step, got {len(injection_logs)}"
    )


def test_injection_log_fires_again_for_a_new_step(caplog: pytest.LogCaptureFixture) -> None:
    """A different step_id logs its injection again (dedup is per-step, not global)."""
    reset_decompose_injection_log_state()
    caplog.set_level(logging.DEBUG, logger="soothe.sloop.decompose.middleware")
    middleware = DecomposeTaskMiddleware()
    with patch(_CONFIGURABLE, return_value={SOOTHE_DECOMPOSE_STEP_ID_KEY: "STEP-A"}):
        _run_through_hook(middleware, _request())
        _run_through_hook(middleware, _request())
    with patch(_CONFIGURABLE, return_value={SOOTHE_DECOMPOSE_STEP_ID_KEY: "STEP-B"}):
        _run_through_hook(middleware, _request())

    injection_logs = [r for r in caplog.records if "injecting decompose_task" in r.getMessage()]
    assert len(injection_logs) == 2


# ── RFC-SLP-006: root split nudge, evidence re-check, pressure hint ────────


def _stub_soothe_config(**decompose_overrides: object) -> object:
    """Minimal soothe_config stub exposing agent.loop.decompose knobs."""
    from types import SimpleNamespace

    fields: dict[str, object] = {
        "recheck_tool_calls": 10,
        "max_rechecks_per_step": 2,
        "split_hint_threshold_pct": 0.60,
        "root_split_nudge": True,
    }
    fields.update(decompose_overrides)
    decompose = SimpleNamespace(**fields)
    return SimpleNamespace(
        agent=SimpleNamespace(
            loop=SimpleNamespace(decompose=decompose, context_window_limit=200_000)
        )
    )


def _jev_conf(**extra: object) -> dict:
    conf: dict = {
        SOOTHE_DECOMPOSE_STEP_ID_KEY: "ROOT-01",
        SOOTHE_STEP_DESCRIPTION_KEY: "build the thing",
        SOOTHE_USER_GOAL_KEY: "build",
    }
    conf.update(extra)
    return conf


def _stub_soothe_config(**decompose_overrides: object) -> object:
    """Minimal soothe_config stub exposing agent.loop.decompose knobs."""
    fields: dict[str, object] = {
        "recheck_tool_calls": 10,
        "max_rechecks_per_step": 2,
        "split_hint_threshold_pct": 0.60,
        "root_split_nudge": True,
    }
    fields.update(decompose_overrides)
    decompose = SimpleNamespace(**fields)
    return SimpleNamespace(
        agent=SimpleNamespace(
            loop=SimpleNamespace(decompose=decompose, context_window_limit=200_000)
        )
    )


def test_root_trusted_split_verdict_gets_parallel_nudge() -> None:
    """A trusted root split verdict earns the nudge (organic path)."""
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(**{SOOTHE_IS_DAG_ROOT_KEY: True})
        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(return_value="split")),
        ):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
        assert PARALLEL_NUDGE_ADDENDUM in forwarded.system_message.content
        assert "decompose_task" in _tool_names(forwarded)
    finally:
        reset_decompose_runtime(tokens)


def test_root_untrusted_split_verdict_no_nudge() -> None:
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(**{SOOTHE_IS_DAG_ROOT_KEY: True})
        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(return_value=None)),
        ):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
        assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content
    finally:
        reset_decompose_runtime(tokens)


def test_non_root_split_verdict_no_nudge() -> None:
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="CHILD-01", sink=sink)
    try:
        conf = _jev_conf()
        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(return_value="split")),
        ):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
        assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content
        assert "decompose_task" in _tool_names(forwarded)
    finally:
        reset_decompose_runtime(tokens)


def test_root_split_nudge_disabled_by_config() -> None:
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(
            **{
                SOOTHE_IS_DAG_ROOT_KEY: True,
                "soothe_config": _stub_soothe_config(root_split_nudge=False),
            }
        )
        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(return_value="split")),
        ):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
        assert PARALLEL_NUDGE_ADDENDUM not in forwarded.system_message.content
    finally:
        reset_decompose_runtime(tokens)


def test_verdict_evaluated_once_across_hops() -> None:
    """The holder caches across hop context copies — one classifier call per step.

    Regression guard: a bare-ContextVar cache re-evaluated on every hop
    (loop f353 showed seven evaluations for seven hops).
    """

    async def scenario() -> int:
        calls = 0

        async def verdict(**_: object) -> str | None:
            nonlocal calls
            calls += 1
            return "finish"

        middleware = DecomposeTaskMiddleware()
        conf = _jev_conf()

        async def handler(req: ModelRequest) -> str:
            return "response"

        with patch(_CONFIGURABLE, return_value=conf), patch(_TYPESAFE, new=verdict):
            # Each asyncio.run executes in a fresh context copy, mirroring
            # LangGraph per-hop contexts; the bound holder is shared.
            for _ in range(3):
                await middleware.awrap_model_call(_request(), handler)
        return calls

    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        assert asyncio.run(scenario()) == 1
    finally:
        reset_decompose_runtime(tokens)


def test_recheck_state_survives_hop_context_copies() -> None:
    """Tool completions in one hop arm a re-check observed in the next hop.

    Each asyncio.run below is a fresh context copy (LangGraph semantics);
    the runtime-bound holder carries the counter and verdict across them.
    """

    async def model_call(middleware: DecomposeTaskMiddleware) -> ModelRequest:
        seen: dict[str, ModelRequest] = {}

        async def handler(req: ModelRequest) -> str:
            seen["req"] = req
            return "response"

        await middleware.awrap_model_call(_request(), handler)
        return seen["req"]

    async def tool_call(middleware: DecomposeTaskMiddleware) -> None:
        async def handler(req: Any) -> str:
            return "tool output"

        await middleware.awrap_tool_call(
            SimpleNamespace(tool_call={"name": "read_file", "id": "t1"}), handler
        )

    verdicts = iter(["finish", "split"])
    middleware = DecomposeTaskMiddleware()
    conf = _jev_conf(**{"soothe_config": _stub_soothe_config(recheck_tool_calls=1)})
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(side_effect=lambda **_: next(verdicts))),
        ):
            first = asyncio.run(model_call(middleware))
            asyncio.run(tool_call(middleware))
            second = asyncio.run(model_call(middleware))
    finally:
        reset_decompose_runtime(tokens)

    assert "decompose_task" not in _tool_names(first)
    assert "decompose_task" in _tool_names(second)
    assert SPLIT_CANDIDATE_ADDENDUM in second.system_message.content
    assert SPLIT_CANDIDATE_ADDENDUM not in first.system_message.content


def test_rechecks_bounded_by_max_per_step() -> None:
    """With max_rechecks_per_step=1 the second milestone never re-arms."""

    async def scenario() -> int:
        calls = 0

        async def verdict(**_: object) -> str | None:
            nonlocal calls
            calls += 1
            return "split"

        middleware = DecomposeTaskMiddleware()
        conf = _jev_conf(
            **{"soothe_config": _stub_soothe_config(recheck_tool_calls=1, max_rechecks_per_step=1)}
        )

        async def model_handler(req: ModelRequest) -> str:
            return "response"

        async def tool_handler(req: Any) -> str:
            return "out"

        with patch(_CONFIGURABLE, return_value=conf), patch(_TYPESAFE, new=verdict):
            await middleware.awrap_model_call(_request(), model_handler)
            await middleware.awrap_tool_call(
                SimpleNamespace(tool_call={"name": "read_file", "id": "t1"}), tool_handler
            )
            await middleware.awrap_model_call(_request(), model_handler)
            await middleware.awrap_tool_call(
                SimpleNamespace(tool_call={"name": "read_file", "id": "t2"}), tool_handler
            )
            await middleware.awrap_model_call(_request(), model_handler)
        return calls

    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        assert asyncio.run(scenario()) == 2
    finally:
        reset_decompose_runtime(tokens)


def test_recheck_untrusted_keeps_strip_sticky() -> None:
    """An untrusted re-check preserves the t=0 finish surface."""

    async def scenario() -> ModelRequest:
        verdicts = iter(["finish", None])
        middleware = DecomposeTaskMiddleware()
        conf = _jev_conf(**{"soothe_config": _stub_soothe_config(recheck_tool_calls=1)})
        seen: dict[str, ModelRequest] = {}

        async def model_handler(key: str):
            async def handler(req: ModelRequest) -> str:
                seen[key] = req
                return "response"

            return handler

        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(side_effect=lambda **_: next(verdicts))),
        ):
            await middleware.awrap_model_call(_request(), await model_handler("first"))

            async def tool_handler(req: Any) -> str:
                return "out"

            await middleware.awrap_tool_call(
                SimpleNamespace(tool_call={"name": "grep", "id": "t1"}), tool_handler
            )
            await middleware.awrap_model_call(_request(), await model_handler("second"))
        return seen["second"]

    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        second = asyncio.run(scenario())
        assert "decompose_task" not in _tool_names(second)
    finally:
        reset_decompose_runtime(tokens)


def _big_request() -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        messages=[HumanMessage(content="x" * 600_000)],
        system_message=SystemMessage(content="orig"),
        tools=[SimpleNamespace(name="read_file")],
        state={},
    )


def test_context_pressure_hint_fires_at_threshold() -> None:
    """The coarse estimate at 0.60 of the context limit fires the hint."""
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(**{"soothe_config": _stub_soothe_config()})
        with patch(_CONFIGURABLE, return_value=conf):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _big_request())
        assert CONTEXT_PRESSURE_SPLIT_HINT in forwarded.system_message.content
    finally:
        reset_decompose_runtime(tokens)


def test_context_pressure_hint_silent_below_threshold() -> None:
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(**{"soothe_config": _stub_soothe_config()})
        with patch(_CONFIGURABLE, return_value=conf):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _request())
        assert CONTEXT_PRESSURE_SPLIT_HINT not in forwarded.system_message.content
    finally:
        reset_decompose_runtime(tokens)


def test_context_pressure_hint_absent_when_tool_stripped() -> None:
    """No hint when a finish verdict stripped the tool — nothing to act on."""
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(**{"soothe_config": _stub_soothe_config()})
        with (
            patch(_CONFIGURABLE, return_value=conf),
            patch(_TYPESAFE, new=AsyncMock(return_value="finish")),
        ):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _big_request())
        assert "decompose_task" not in _tool_names(forwarded)
        assert CONTEXT_PRESSURE_SPLIT_HINT not in forwarded.system_message.content
    finally:
        reset_decompose_runtime(tokens)


def test_recheck_and_hint_never_fire_in_plan_mode() -> None:
    reset_decompose_injection_log_state()
    sink: list = []
    tokens = bind_decompose_runtime(step_id="ROOT-01", sink=sink)
    try:
        conf = _jev_conf(
            **{
                SOOTHE_INTERACTION_MODE_KEY: "plan",
                "soothe_config": _stub_soothe_config(recheck_tool_calls=1),
            }
        )
        with patch(_CONFIGURABLE, return_value=conf):
            forwarded = _run_through_hook(DecomposeTaskMiddleware(), _big_request())
        assert "decompose_task" not in _tool_names(forwarded)
        assert CONTEXT_PRESSURE_SPLIT_HINT not in forwarded.system_message.content
    finally:
        reset_decompose_runtime(tokens)
