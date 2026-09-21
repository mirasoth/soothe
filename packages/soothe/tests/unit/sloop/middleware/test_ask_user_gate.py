"""Unit tests for the RFC-635 AskUserGateMiddleware inline veritas fast path."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from soothe.sloop.clarification.protocol import LoopStateView
from soothe.sloop.middleware import AskUserGateMiddleware
from soothe.sloop.middleware import ask_user_gate as gate_mod
from soothe.subagents.veritas.schemas import VeritasAnswerSchema


def _view() -> LoopStateView:
    return LoopStateView(
        goal_id="g",
        goal_description="",
        user_request="",
        iteration=0,
        intent_classification=None,
        plan_summary=None,
        recent_step_outputs=(),
        workspace_summary=None,
        active_skills=(),
        active_mcp_servers=(),
    )


class _Runtime:
    def __init__(self, configurable: dict[str, Any] | None = None) -> None:
        self.config = {"configurable": configurable or {}}


def _tc(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"type": "tool_call", "name": name, "args": args, "id": call_id}


def _ask_tc(questions: list[str], call_id: str = "c1") -> dict[str, Any]:
    return _tc(
        "ask_user",
        {
            "questions": [
                {
                    "question": q,
                    "header": "Q",
                    "options": [
                        {"label": "Yes", "description": "y"},
                        {"label": "No", "description": "n"},
                    ],
                }
                for q in questions
            ]
        },
        call_id=call_id,
    )


def _state(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {"messages": [AIMessage(content="", tool_calls=list(tool_calls))]}


def _schema(
    answers: list[str] | None = None,
    *,
    confidence: float = 0.9,
    defer: bool = False,
    rationale: str = "ok",
) -> VeritasAnswerSchema:
    return VeritasAnswerSchema(
        answers=answers if answers is not None else [],
        confidence=confidence,
        defer=defer,
        rationale=rationale,
    )


class _Veritas:
    """Stub veritas callable recording requests."""

    def __init__(self, result: VeritasAnswerSchema | Exception) -> None:
        self._result = result
        self.requests: list[Any] = []

    async def __call__(self, request: Any, *, thread_id: Any = None, loop_id: Any = None) -> Any:
        self.requests.append(request)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _gate(veritas: _Veritas, **kwargs: Any) -> AskUserGateMiddleware:
    defaults: dict[str, Any] = {
        "veritas_answer": veritas,
        "min_confidence": 0.4,
        "default_clarification_mode": "auto",
    }
    defaults.update(kwargs)
    return AskUserGateMiddleware(**defaults)


def _runtime(human: bool = False) -> _Runtime:
    return _Runtime(
        {
            "soothe_clarification_mode": "auto",
            "soothe_human_attached": human,
            "soothe_veritas_loop_view": _view(),
            "thread_id": "t1",
        }
    )


def _stub_interrupt(monkeypatch: pytest.MonkeyPatch, return_value: Any) -> list[Any]:
    captured: list[Any] = []

    def _fake(payload: Any) -> Any:
        captured.append(payload)
        return return_value

    monkeypatch.setattr(gate_mod, "interrupt", _fake)
    return captured


def _stub_writer(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []

    class _Writer:
        def __call__(self, data: Any) -> None:
            captured.append(data)

    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: _Writer(), raising=False)
    return captured


# ---------------------------------------------------------------------------
# Confident inline answer
# ---------------------------------------------------------------------------


class TestInlineAnswer:
    @pytest.mark.asyncio
    async def test_confident_answer_inlined_no_interrupt(self, monkeypatch) -> None:
        """High-confidence answer strips the call and injects the Q&A block."""
        captured = _stub_interrupt(monkeypatch, {"answers": []})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(["soothe"], confidence=0.9))
        state = _state([_ask_tc(["Which package first?"])])
        result = await _gate(veritas).aafter_model(state, _runtime())
        assert captured == []  # no interrupt
        assert result is not None
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "User answered:" in tool_msg.content
        assert "soothe" in tool_msg.content
        assert result["messages"][0].tool_calls == []

    @pytest.mark.asyncio
    async def test_multi_question_answer_rendered(self, monkeypatch) -> None:
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(["a1", "a2"], confidence=0.8))
        state = _state([_ask_tc(["q1?", "q2?"])])
        result = await _gate(veritas).aafter_model(state, _runtime())
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "Q: q1?" in tool_msg.content
        assert "A: a1" in tool_msg.content
        assert "Q: q2?" in tool_msg.content
        assert "A: a2" in tool_msg.content

    @pytest.mark.asyncio
    async def test_inline_answer_emits_history_event(self, monkeypatch) -> None:
        captured = _stub_writer(monkeypatch)
        _stub_interrupt(monkeypatch, {"answers": []})
        veritas = _Veritas(_schema(["yes"], confidence=0.9))
        state = _state([_ask_tc(["Proceed?"])])
        await _gate(veritas).aafter_model(state, _runtime())
        assert captured and captured[0]["type"] == "soothe.internal.clarification.auto_answered"
        assert captured[0]["answers"] == ["yes"]
        assert captured[0]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Defer / failure paths
# ---------------------------------------------------------------------------


class TestDeferPaths:
    @pytest.mark.asyncio
    async def test_defer_human_attached_interrupts_with_marker(self, monkeypatch) -> None:
        """Defer with a human: gate strips + interrupts with the ask_user
        shape and the gate_deferred marker (station skips veritas)."""
        captured = _stub_interrupt(monkeypatch, {"answers": ["human says X"]})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(defer=True, rationale="no evidence"))
        state = _state([_ask_tc(["What DB?"])])
        result = await _gate(veritas).aafter_model(state, _runtime(human=True))
        assert len(captured) == 1
        payload = captured[0]
        assert payload["type"] == "ask_user"
        assert payload["gate_deferred"] is True
        assert payload["gate_deferred_kind"] == "explicit"
        assert payload["questions"]
        # Human answer distributed back as a ToolMessage.
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "human says X" in tool_msg.content

    @pytest.mark.asyncio
    async def test_defer_autopilot_retry_inline(self, monkeypatch) -> None:
        """Defer without a human + retry enabled: (retry) sentinel inline,
        no interrupt."""
        captured = _stub_interrupt(monkeypatch, {"answers": []})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(defer=True, rationale="no evidence"))
        state = _state([_ask_tc(["What DB?"])])
        result = await _gate(veritas).aafter_model(state, _runtime(human=False))
        assert captured == []
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "(retry)" in tool_msg.content

    @pytest.mark.asyncio
    async def test_defer_autopilot_retry_disabled_interrupts(self, monkeypatch) -> None:
        """Defer without a human + retry disabled: gate interrupt + marker
        (station parks the goal — seven-day hard defer)."""
        captured = _stub_interrupt(monkeypatch, {"answers": []})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(defer=True, rationale="no evidence"))
        state = _state([_ask_tc(["What DB?"])])
        result = await _gate(veritas, autopilot_retry_on_fail=False).aafter_model(
            state, _runtime(human=False)
        )
        assert len(captured) == 1
        assert captured[0]["gate_deferred"] is True
        # Dismissed (no answers) → dismissal message parity with the tool.
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "dismissed" in tool_msg.content

    @pytest.mark.asyncio
    async def test_low_confidence_defers_to_human(self, monkeypatch) -> None:
        captured = _stub_interrupt(monkeypatch, {"answers": ["pick B"]})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(["maybe"], confidence=0.2))
        state = _state([_ask_tc(["Which option?"])])
        await _gate(veritas).aafter_model(state, _runtime(human=True))
        assert len(captured) == 1
        assert captured[0]["gate_deferred_kind"] == "low_confidence"

    @pytest.mark.asyncio
    async def test_veritas_exception_autopilot_retry(self, monkeypatch) -> None:
        """Gate exception under autopilot: retry sentinel inline (parity with
        the station's structured_output_failed ladder)."""
        captured = _stub_interrupt(monkeypatch, {"answers": []})
        _stub_writer(monkeypatch)
        veritas = _Veritas(RuntimeError("llm down"))
        state = _state([_ask_tc(["Q?"])])
        result = await _gate(veritas).aafter_model(state, _runtime(human=False))
        assert captured == []
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "(retry)" in tool_msg.content

    @pytest.mark.asyncio
    async def test_veritas_exception_human_interrupts(self, monkeypatch) -> None:
        captured = _stub_interrupt(monkeypatch, {"answers": ["x"]})
        _stub_writer(monkeypatch)
        veritas = _Veritas(RuntimeError("llm down"))
        state = _state([_ask_tc(["Q?"])])
        await _gate(veritas).aafter_model(state, _runtime(human=True))
        assert len(captured) == 1


# ---------------------------------------------------------------------------
# Fail-safes and scope
# ---------------------------------------------------------------------------


class TestFailSafes:
    @pytest.mark.asyncio
    async def test_manual_mode_noop(self, monkeypatch) -> None:
        """Manual mode: the gate leaves the call (tool interrupts, station
        routes to the interactive policy — today's flow)."""
        veritas = _Veritas(_schema(["x"]))
        state = _state([_ask_tc(["Q?"])])
        runtime = _Runtime({"soothe_clarification_mode": "manual"})
        assert await _gate(veritas).aafter_model(state, runtime) is None
        assert veritas.requests == []

    @pytest.mark.asyncio
    async def test_missing_loop_view_noop(self, monkeypatch) -> None:
        veritas = _Veritas(_schema(["x"]))
        state = _state([_ask_tc(["Q?"])])
        runtime = _Runtime({"soothe_clarification_mode": "auto"})
        assert await _gate(veritas).aafter_model(state, runtime) is None
        assert veritas.requests == []

    @pytest.mark.asyncio
    async def test_no_ask_user_calls_noop(self, monkeypatch) -> None:
        veritas = _Veritas(_schema(["x"]))
        state = _state([_tc("read_file", {"file_path": "/w/y"})])
        assert await _gate(veritas).aafter_model(state, _runtime()) is None

    @pytest.mark.asyncio
    async def test_malformed_args_pass_through(self, monkeypatch) -> None:
        """Unparseable ask_user args: leave the call for the tool itself."""
        veritas = _Veritas(_schema(["x"]))
        state = _state([_tc("ask_user", {"questions": 42})])
        assert await _gate(veritas).aafter_model(state, _runtime()) is None
        assert veritas.requests == []

    @pytest.mark.asyncio
    async def test_empty_state_noop(self, monkeypatch) -> None:
        veritas = _Veritas(_schema(["x"]))
        assert await _gate(veritas).aafter_model({}, _runtime()) is None
        assert await _gate(veritas).aafter_model({"messages": []}, _runtime()) is None


# ---------------------------------------------------------------------------
# Batch mixing
# ---------------------------------------------------------------------------


class TestBatchMix:
    @pytest.mark.asyncio
    async def test_mixed_confident_and_deferred(self, monkeypatch) -> None:
        """Confident calls inline-answer; deferred ones bundle into one
        interrupt; non-ask_user calls untouched."""
        results = [
            _schema(["pkg-a"], confidence=0.9),
            _schema(defer=True, rationale="no evidence"),
        ]

        class _SeqVeritas:
            requests: list[Any] = []

            async def __call__(
                self, request: Any, *, thread_id: Any = None, loop_id: Any = None
            ) -> Any:
                _SeqVeritas.requests.append(request)
                return results[len(_SeqVeritas.requests) - 1]

        captured = _stub_interrupt(monkeypatch, {"answers": ["human pick"]})
        _stub_writer(monkeypatch)
        state = _state(
            [
                _ask_tc(["Which package?"], call_id="c1"),
                _ask_tc(["What DB?"], call_id="c2"),
                _tc("read_file", {"file_path": "/w/y"}, call_id="c3"),
            ]
        )
        result = await _gate(_SeqVeritas()).aafter_model(state, _runtime(human=True))
        # Only the deferred question reached the human.
        assert len(captured) == 1
        assert [q["question"] for q in captured[0]["questions"]] == ["What DB?"]
        ai = result["messages"][0]
        assert [tc["id"] for tc in ai.tool_calls] == ["c3"]
        answers = {
            m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)
        }
        assert "pkg-a" in answers["c1"]
        assert "human pick" in answers["c2"]

    @pytest.mark.asyncio
    async def test_multi_question_defer_distribution(self, monkeypatch) -> None:
        """One deferred call with two questions: answers distribute in
        order (station broadcast/padding semantics)."""
        _stub_interrupt(monkeypatch, {"answers": ["first", "second"]})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(defer=True, rationale="no"))
        state = _state([_ask_tc(["q1?", "q2?"])])
        result = await _gate(veritas).aafter_model(state, _runtime(human=True))
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "A: first" in tool_msg.content
        assert "A: second" in tool_msg.content

    @pytest.mark.asyncio
    async def test_single_answer_broadcast_across_questions(self, monkeypatch) -> None:
        _stub_interrupt(monkeypatch, {"answers": ["only"]})
        _stub_writer(monkeypatch)
        veritas = _Veritas(_schema(defer=True, rationale="no"))
        state = _state([_ask_tc(["q1?", "q2?"])])
        result = await _gate(veritas).aafter_model(state, _runtime(human=True))
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.content.count("A: only") == 2


# ---------------------------------------------------------------------------
# Veritas request construction
# ---------------------------------------------------------------------------


class TestRequestConstruction:
    @pytest.mark.asyncio
    async def test_veritas_receives_loop_view_and_questions(self, monkeypatch) -> None:
        _stub_writer(monkeypatch)
        _stub_interrupt(monkeypatch, {"answers": []})
        veritas = _Veritas(_schema(["x"], confidence=0.9))
        state = _state([_ask_tc(["Q?"])])
        await _gate(veritas).aafter_model(state, _runtime())
        assert len(veritas.requests) == 1
        request = veritas.requests[0]
        assert request.origin_node == "execute"
        assert request.loop_state.goal_id == "g"
        assert request.questions


# ---------------------------------------------------------------------------
# Policy marker branch (station skips veritas)
# ---------------------------------------------------------------------------


class TestPolicyGateDeferredBranch:
    @pytest.mark.asyncio
    async def test_gate_deferred_skips_veritas_routes_human(self) -> None:
        from dataclasses import replace as dc_replace

        from soothe.sloop.clarification.auto import AutoClarificationPolicy
        from soothe.sloop.clarification.protocol import ClarificationAnswer

        async def _veritas(_req: Any) -> VeritasAnswerSchema:
            raise AssertionError("station must not re-run veritas for gate-deferred requests")

        fallback_answer = ClarificationAnswer(answers=("human X",), source="human")

        class _Fallback:
            async def answer_as_manual_fallback(
                self, request: Any, *, announce: bool = True
            ) -> Any:
                return fallback_answer

        policy = AutoClarificationPolicy(_veritas, interactive_fallback=_Fallback())
        base = _gate_request()
        request = dc_replace(base, metadata={**base.metadata, "gate_deferred": True})
        answer = await policy.answer(request)
        assert answer is fallback_answer

    @pytest.mark.asyncio
    async def test_gate_deferred_autopilot_defer_parks(self) -> None:
        from dataclasses import replace as dc_replace

        from soothe.sloop.clarification.auto import AutoClarificationPolicy
        from soothe.sloop.clarification.protocol import ClarificationDeferredError

        async def _veritas(_req: Any) -> VeritasAnswerSchema:
            raise AssertionError("station must not re-run veritas for gate-deferred requests")

        policy = AutoClarificationPolicy(_veritas, autopilot_retry_on_fail=False)
        base = _gate_request()
        request = dc_replace(
            base,
            metadata={
                **base.metadata,
                "gate_deferred": True,
                "gate_deferred_kind": "low_confidence",
            },
        )
        with pytest.raises(ClarificationDeferredError) as exc_info:
            await policy.answer(request)
        assert exc_info.value.kind == "low_confidence"

    @pytest.mark.asyncio
    async def test_gate_deferred_autopilot_retry_sentinel(self) -> None:
        from dataclasses import replace as dc_replace

        from soothe.sloop.clarification.auto import AutoClarificationPolicy

        async def _veritas(_req: Any) -> VeritasAnswerSchema:
            raise AssertionError("station must not re-run veritas for gate-deferred requests")

        policy = AutoClarificationPolicy(_veritas)
        base = _gate_request()
        request = dc_replace(base, metadata={**base.metadata, "gate_deferred": True})
        answer = await policy.answer(request)
        assert answer.source == "retry"
        assert answer.answers == ("(retry)",)


def _gate_request() -> Any:
    from soothe.sloop.clarification.origins import ORIGIN_EXECUTE
    from soothe.sloop.clarification.protocol import ClarificationRequest

    return ClarificationRequest(
        questions=("Q?",),
        origin_node=ORIGIN_EXECUTE,
        origin_interrupt_id="i1",
        loop_state=_view(),
    )


# ---------------------------------------------------------------------------
# Detector marker passthrough
# ---------------------------------------------------------------------------


class TestDetectorMarker:
    def test_gate_deferred_marker_into_metadata(self) -> None:
        from soothe.sloop.clarification.detector import ClarificationDetector

        detector = ClarificationDetector()
        request = detector.from_interrupt(
            {
                "type": "ask_user",
                "questions": [{"question": "Q?", "header": "Q"}],
                "gate_deferred": True,
                "gate_deferred_kind": "low_confidence",
            },
            interrupt_id="i1",
            origin_node="execute",
            loop_state=_view(),
        )
        assert request is not None
        assert request.metadata.get("gate_deferred") is True
        assert request.metadata.get("gate_deferred_kind") == "low_confidence"

    def test_no_marker_without_gate_flag(self) -> None:
        from soothe.sloop.clarification.detector import ClarificationDetector

        detector = ClarificationDetector()
        request = detector.from_interrupt(
            {"type": "ask_user", "questions": [{"question": "Q?", "header": "Q"}]},
            interrupt_id="i1",
            origin_node="execute",
            loop_state=_view(),
        )
        assert request is not None
        assert not request.metadata


# ---------------------------------------------------------------------------
# Builder wiring
# ---------------------------------------------------------------------------


class TestBuilderWiring:
    def test_build_ask_user_gate_from_config(self) -> None:
        from types import SimpleNamespace

        from soothe.config.models import AskUserGateConfig
        from soothe.coreagent.builder import AgentBuilder

        class _Config:
            agent = SimpleNamespace(
                clarification=SimpleNamespace(
                    ask_user_gate=AskUserGateConfig(enabled=True),
                    auto_min_confidence=0.5,
                    default_mode="auto",
                    autopilot_retry_on_fail=True,
                ),
                veritas=SimpleNamespace(
                    model_role="think",
                    max_context_steps=8,
                    max_retries=2,
                    retry_backoff_seconds=2.0,
                    coerced_confidence=0.7,
                ),
            )

            def create_chat_model(self, role: str) -> Any:
                return object()

        builder = AgentBuilder.__new__(AgentBuilder)
        builder._identity_runtime = None
        builder._intake_only_specs = []
        builder._config = _Config()
        gate = builder._build_ask_user_gate()
        assert isinstance(gate, AskUserGateMiddleware)

    def test_gate_none_when_disabled(self) -> None:
        from types import SimpleNamespace

        from soothe.config.models import AskUserGateConfig
        from soothe.coreagent.builder import AgentBuilder

        builder = AgentBuilder.__new__(AgentBuilder)
        builder._config = SimpleNamespace(
            agent=SimpleNamespace(
                clarification=SimpleNamespace(ask_user_gate=AskUserGateConfig(enabled=False)),
                veritas=SimpleNamespace(model_role="think"),
            )
        )
        assert builder._build_ask_user_gate() is None


# ---------------------------------------------------------------------------
# node_execute history recording
# ---------------------------------------------------------------------------


class TestHistoryRecording:
    def test_maybe_record_gate_answer_appends_history(self) -> None:
        from soothe.sloop.stations.execute.execute import _maybe_record_gate_answer

        state = SimpleNamespaceState()
        chunk = (
            (),
            "custom",
            {
                "type": "soothe.internal.clarification.auto_answered",
                "questions": [{"question": "Q?"}],
                "answers": ["yes"],
                "confidence": 0.9,
                "source": "veritas",
            },
        )
        assert _maybe_record_gate_answer(chunk, state) is True
        assert len(state.clarification_history) == 1
        entry = state.clarification_history[0]
        assert entry["answers"] == ["yes"]
        assert entry["source"] == "veritas"

    def test_other_chunks_not_consumed(self) -> None:
        from soothe.sloop.stations.execute.execute import _maybe_record_gate_answer

        state = SimpleNamespaceState()
        assert _maybe_record_gate_answer(((), "custom", {"type": "other"}), state) is False
        assert _maybe_record_gate_answer(((), "messages", {"x": 1}), state) is False
        assert _maybe_record_gate_answer("not-a-tuple", state) is False
        assert state.clarification_history == []


class SimpleNamespaceState:
    """Minimal loop state with a clarification_history list."""

    def __init__(self) -> None:
        self.clarification_history: list[dict[str, Any]] = []
