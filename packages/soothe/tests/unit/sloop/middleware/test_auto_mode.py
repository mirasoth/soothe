"""Unit tests for the AutoModeMiddleware inline tool-approval gate."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from soothe.config.models import ToolApprovalConfig
from soothe.sloop.middleware import AutoModeMiddleware
from soothe.sloop.middleware import auto_mode as auto_mode_mod
from soothe.sloop.middleware.auto_mode_context import AutoModeContext


def _pipeline(config: ToolApprovalConfig | None = None) -> Any:
    from soothe.sloop.clarification.tool_approval_pipeline import ToolApprovalPipeline

    return ToolApprovalPipeline(config or ToolApprovalConfig())


class _Runtime:
    """Minimal runtime carrying a LangGraph-style config."""

    def __init__(self, configurable: dict[str, Any] | None = None) -> None:
        self.config = {"configurable": configurable or {}}


def _state(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "messages": [
            AIMessage(content="", tool_calls=list(tool_calls)),
        ]
    }


def _tc(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"type": "tool_call", "name": name, "args": args, "id": call_id}


def _gate(**kwargs: Any) -> AutoModeMiddleware:
    defaults: dict[str, Any] = {"pipeline": _pipeline()}
    defaults.update(kwargs)
    return AutoModeMiddleware(**defaults)


def _stub_interrupt(monkeypatch: pytest.MonkeyPatch, return_value: Any) -> list[Any]:
    captured: list[Any] = []

    def _fake(payload: Any) -> Any:
        captured.append(payload)
        return return_value

    monkeypatch.setattr(auto_mode_mod, "interrupt", _fake)
    return captured


# ---------------------------------------------------------------------------
# Pass-through / gating scope
# ---------------------------------------------------------------------------


def test_ungated_tool_passes_through_untouched() -> None:
    """Non-gated tools never evaluate — verdict None, no message change."""
    state = _state([_tc("read_file", {"file_path": "/workspace/x"})])
    result = _gate().after_model(state, _Runtime({}))
    assert result is None


def test_no_tool_calls_noop() -> None:
    state = {"messages": [AIMessage(content="done")]}
    assert _gate().after_model(state, _Runtime({})) is None


def test_empty_state_noop() -> None:
    assert _gate().after_model({}, _Runtime({})) is None
    assert _gate().after_model({"messages": []}, _Runtime({})) is None


def test_gate_respects_tools_filter() -> None:
    """A gate scoped to run_command ignores edit_file calls."""
    state = _state([_tc("edit_file", {"file_path": "/etc/hosts"})])
    result = _gate(tools=("run_command",)).after_model(state, _Runtime({}))
    assert result is None


# ---------------------------------------------------------------------------
# Decision table: deny rules (inline reject, no interrupt)
# ---------------------------------------------------------------------------


class TestDenyRuleReject:
    """User-configured deny rules (the host extension point — RFC-LOOP-030
    moved the built-ins into nano's severity-tiered registry)."""

    @staticmethod
    def _deny_pipeline() -> Any:
        from soothe.config.models import ToolApprovalRule

        cfg = ToolApprovalConfig(deny_rules=[ToolApprovalRule(tool="run_command", pattern="apt *")])
        return _pipeline(cfg)

    def test_deny_rule_inline_reject(self) -> None:
        state = _state([_tc("run_command", {"command": "apt install foo"})])
        result = _gate(pipeline=self._deny_pipeline()).after_model(state, _Runtime({}))
        assert result is not None
        messages = result["messages"]
        tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
        assert tool_msg.status == "error"
        assert "deny rule" in tool_msg.content
        # The tool call is stripped from the AI message.
        ai = messages[0]
        assert ai.tool_calls == []

    def test_deny_rule_rejects_even_in_bypass(self) -> None:
        """active_in_bypass=True: deny rules stay absolute in bypass mode."""
        state = _state([_tc("run_command", {"command": "apt install foo"})])
        runtime = _Runtime({"soothe_interaction_mode": "bypass"})
        result = _gate(pipeline=self._deny_pipeline(), active_in_bypass=True).after_model(
            state, runtime
        )
        assert result is not None
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.status == "error"

    def test_deny_rule_inactive_in_bypass_when_disabled(self) -> None:
        """active_in_bypass=False: all evaluation skipped in bypass."""
        state = _state([_tc("run_command", {"command": "apt install foo"})])
        runtime = _Runtime({"soothe_interaction_mode": "bypass"})
        result = _gate(pipeline=self._deny_pipeline(), active_in_bypass=False).after_model(
            state, runtime
        )
        assert result is None

    def test_bypass_permits_all_tool_calls_by_default(self) -> None:
        """Config default (active_in_bypass=False): all tool calls permitted
        in bypass mode."""
        from soothe.config.models import InlineGateConfig

        cfg = InlineGateConfig()
        assert cfg.active_in_bypass is False
        state = _state([_tc("run_command", {"command": "apt install foo"})])
        runtime = _Runtime({"soothe_interaction_mode": "bypass"})
        result = _gate(pipeline=self._deny_pipeline(), active_in_bypass=False).after_model(
            state, runtime
        )
        assert result is None

    def test_builtin_deny_rules_migrated_to_nano(self) -> None:
        """RFC-LOOP-030: the built-in rule list is empty by default — the
        built-ins live in nano's severity registry."""
        assert ToolApprovalConfig().deny_rules == []


# ---------------------------------------------------------------------------
# Decision table: safety escalation
# ---------------------------------------------------------------------------


class TestSafetyEscalation:
    def test_safety_hit_autopilot_inline_reject(self) -> None:
        """No human attached: safety escalate degrades to an instructive
        reject (parity with the old station degrade-to-reject path)."""
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        result = _gate().after_model(state, _Runtime({}))
        assert result is not None
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.status == "error"
        assert "blocked" in tool_msg.content

    def test_safety_hit_human_attached_interrupts(self, monkeypatch) -> None:
        """Human attached: a critical safety escalate emits the standard
        action_requests interrupt (durable, routed through the station)."""
        captured = _stub_interrupt(
            monkeypatch,
            {"decisions": [{"type": "approve"}]},
        )
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate().after_model(state, runtime)
        assert len(captured) == 1
        payload = captured[0]
        assert payload["action_requests"][0]["name"] == "run_command"
        assert payload["escalated_rule_id"]
        # Approved by the human → tool call kept, no rewrite needed.
        assert result is None

    def test_safety_hit_allowlisted_signature_allows(self) -> None:
        """Prior exact-signature approval executes silently (no interrupt)."""
        allowlist = [{"tool": "run_command", "signature": "rm -rf /"}]
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_human_attached": True, "tool_approval_allowlist": allowlist})
        assert _gate().after_model(state, runtime) is None

    def test_safety_hit_rule_family_override_allows(self) -> None:
        """Prior rule-family approval suppresses re-escalation for a
        different command under the same rule."""
        first = _pipeline().evaluate_action("run_command", {"command": "rm -rf /"})
        assert first.decision == "escalate"
        allowlist = [{"rule": first.rule_id}]
        state = _state([_tc("run_command", {"command": "rm -rf /tmp"})])
        runtime = _Runtime({"soothe_human_attached": True, "tool_approval_allowlist": allowlist})
        assert _gate().after_model(state, runtime) is None

    def test_safety_hit_bypass_allows(self) -> None:
        """Bypass skips safety (no escalation, no interrupt)."""
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_interaction_mode": "bypass"})
        assert _gate().after_model(state, runtime) is None


# ---------------------------------------------------------------------------
# Decision table: ambiguous actions by mode
# ---------------------------------------------------------------------------


class TestAmbiguousActions:
    def test_auto_mode_allows_ambiguous(self) -> None:
        """Auto mode default-approves rule-unresolved actions (no interrupt)."""
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        runtime = _Runtime({"soothe_clarification_mode": "auto"})
        assert _gate().after_model(state, runtime) is None

    def test_manual_mode_all_interrupts_when_human_attached(self, monkeypatch) -> None:
        """manual_scope=all: every gated call asks the human."""
        captured = _stub_interrupt(monkeypatch, {"decisions": [{"type": "approve"}]})
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        runtime = _Runtime({"soothe_clarification_mode": "manual", "soothe_human_attached": True})
        _gate(manual_scope="all").after_model(state, runtime)
        assert len(captured) == 1

    def test_manual_mode_ambiguous_only_allows(self) -> None:
        """manual_scope=ambiguous_only: rule-unresolved actions auto-approve."""
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        runtime = _Runtime({"soothe_clarification_mode": "manual", "soothe_human_attached": True})
        assert _gate(manual_scope="ambiguous_only").after_model(state, runtime) is None

    def test_manual_mode_autopilot_allows(self) -> None:
        """Headless manual (defensive): no human → allow (retry-sentinel
        parity; the old path resolved to execute)."""
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        runtime = _Runtime({"soothe_clarification_mode": "manual"})
        assert _gate(manual_scope="all").after_model(state, runtime) is None

    def test_force_manual_tool_approval_interrupts_every_call(self, monkeypatch) -> None:
        """force_manual_origins includes tool_approval: every gated call
        goes to the human when one is attached."""
        captured = _stub_interrupt(monkeypatch, {"decisions": [{"type": "approve"}]})
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        runtime = _Runtime({"soothe_clarification_mode": "auto", "soothe_human_attached": True})
        _gate(force_manual_tool_approval=True).after_model(state, runtime)
        assert len(captured) == 1

    def test_missing_mode_falls_back_to_default(self) -> None:
        """No clarification mode in configurable → build-time default."""
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        assert _gate(default_clarification_mode="auto").after_model(state, _Runtime({})) is None


# ---------------------------------------------------------------------------
# Mixed batches and human decisions
# ---------------------------------------------------------------------------


class TestDecisionsAndBatches:
    def test_mixed_batch_rejects_deny_and_interrupts_safety(self, monkeypatch) -> None:
        """One deny-rule call and one safety call: the deny call strips
        inline; the safety call interrupts with the human."""
        from soothe.config.models import ToolApprovalRule

        cfg = ToolApprovalConfig(deny_rules=[ToolApprovalRule(tool="run_command", pattern="apt *")])
        captured = _stub_interrupt(
            monkeypatch,
            {"decisions": [{"type": "reject", "message": "no"}]},
        )
        state = _state(
            [
                _tc("run_command", {"command": "apt install foo"}, call_id="c1"),
                _tc("run_command", {"command": "rm -rf /"}, call_id="c2"),
                _tc("read_file", {"file_path": "/w/y"}, call_id="c3"),
            ]
        )
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate(pipeline=_pipeline(cfg)).after_model(state, runtime)
        # Only the safety call reached the human.
        assert len(captured) == 1
        assert [ar["name"] for ar in captured[0]["action_requests"]] == ["run_command"]
        # The read_file call survives untouched; both gated calls are gone.
        messages = result["messages"]
        ai = messages[0]
        assert [tc["id"] for tc in ai.tool_calls] == ["c3"]
        # Two error ToolMessages: inline deny + human reject.
        errors = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(errors) == 2
        assert all(m.status == "error" for m in errors)

    def test_human_reject_strips_call_with_reason(self, monkeypatch) -> None:
        _stub_interrupt(
            monkeypatch,
            {"decisions": [{"type": "reject", "message": "not allowed"}]},
        )
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate().after_model(state, runtime)
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.status == "error"
        assert "not allowed" in tool_msg.content

    def test_human_edit_rewrites_call(self, monkeypatch) -> None:
        _stub_interrupt(
            monkeypatch,
            {
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "run_command",
                            "args": {"command": "rm -rf /tmp/safe"},
                        },
                    }
                ]
            },
        )
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate().after_model(state, runtime)
        ai = result["messages"][0]
        assert ai.tool_calls[0]["args"]["command"] == "rm -rf /tmp/safe"

    def test_human_respond_synthesizes_success_message(self, monkeypatch) -> None:
        _stub_interrupt(
            monkeypatch,
            {"decisions": [{"type": "respond", "message": "answered"}]},
        )
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate().after_model(state, runtime)
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.status == "success"
        assert tool_msg.content == "answered"

    def test_short_decisions_padded_with_approve(self, monkeypatch) -> None:
        """Mid-flight allowlist change shrinking the action list: missing
        trailing decisions default to approve."""
        captured = _stub_interrupt(monkeypatch, {"decisions": []})
        state = _state(
            [
                _tc("run_command", {"command": "rm -rf /"}, call_id="c1"),
                _tc("run_command", {"command": "rm -rf /etc"}, call_id="c2"),
            ]
        )
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate().after_model(state, runtime)
        # Both interrupted; empty decisions list padded to approve → both
        # calls survive, no message rewrite needed.
        assert len(captured[0]["action_requests"]) == 2
        assert result is None
        ai = state["messages"][-1]
        assert len(ai.tool_calls) == 2

    def test_malformed_resume_defaults_to_approve(self, monkeypatch) -> None:
        """A non-dict resume payload resolves to approve for all."""
        _stub_interrupt(monkeypatch, "garbage")
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        runtime = _Runtime({"soothe_human_attached": True})
        result = _gate().after_model(state, runtime)
        assert result is None or result["messages"][0].tool_calls != []


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_evaluator_error_passes_through(self, monkeypatch) -> None:
        """Any evaluator exception → allow (downstream guards still run)."""
        from soothe.sloop.clarification import tool_approval_pipeline as pipeline_mod

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("evaluator exploded")

        monkeypatch.setattr(pipeline_mod.ToolApprovalPipeline, "evaluate_action", _boom)
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        assert _gate().after_model(state, _Runtime({})) is None


# ---------------------------------------------------------------------------
# Context reader
# ---------------------------------------------------------------------------


class TestAutoModeContext:
    def test_from_runtime_reads_all_keys(self) -> None:
        runtime = _Runtime(
            {
                "workspace": "/workspace",
                "tool_approval_allowlist": [{"tool": "run_command", "signature": "x"}],
                "soothe_clarification_mode": "manual",
                "soothe_human_attached": True,
                "soothe_interaction_mode": "bypass",
            }
        )
        ctx = AutoModeContext.from_runtime(runtime, default_mode="auto")
        assert ctx.workspace == "/workspace"
        assert len(ctx.allowlist) == 1
        assert ctx.clarification_mode == "manual"
        assert ctx.human_attached is True
        assert ctx.bypass is True

    def test_missing_keys_fail_safe(self) -> None:
        ctx = AutoModeContext.from_runtime(_Runtime({}), default_mode="manual")
        assert ctx.workspace is None
        assert ctx.allowlist == ()
        assert ctx.clarification_mode == "manual"
        assert ctx.human_attached is False
        assert ctx.bypass is False

    def test_invalid_mode_falls_back_to_default(self) -> None:
        runtime = _Runtime({"soothe_clarification_mode": "bogus"})
        ctx = AutoModeContext.from_runtime(runtime, default_mode="auto")
        assert ctx.clarification_mode == "auto"


# ---------------------------------------------------------------------------
# Veritas leg (RFC-LOOP-030: hint-carrying unresolved calls only)
# ---------------------------------------------------------------------------


def _veritas_runtime(human: bool = False) -> _Runtime:
    def _loop_view() -> Any:
        from soothe.sloop.clarification.protocol import LoopStateView

        return LoopStateView(
            goal_id="g",
            goal_description="refactor auth",
            user_request="",
            iteration=0,
            intent_classification=None,
            plan_summary=None,
            recent_step_outputs=(),
            workspace_summary=None,
            active_skills=(),
            active_mcp_servers=(),
        )

    return _Runtime(
        {
            "soothe_clarification_mode": "auto",
            "soothe_human_attached": human,
            "soothe_veritas_loop_view": _loop_view(),
            "thread_id": "t1",
        }
    )


def _answer(schema: Any):
    """Build a stub veritas answerer recording every request."""
    calls: list[Any] = []

    async def _answerer(request: Any, *, thread_id: Any = None, loop_id: Any = None) -> Any:
        calls.append(request)
        return schema

    return _answerer, calls


def _approve() -> Any:
    from soothe.subagents.veritas.schemas import VeritasAnswerSchema

    return VeritasAnswerSchema(
        defer=False,
        answers=["approve"],
        confidence=0.95,
        rationale="fine",
        answer_is_question=[False],
        source="decision_model",
    )


def _deny() -> Any:
    from soothe.subagents.veritas.schemas import VeritasAnswerSchema

    return VeritasAnswerSchema(
        defer=False,
        answers=["deny"],
        confidence=0.95,
        rationale="too risky",
        answer_is_question=[False],
        source="decision_model",
    )


def _defer() -> Any:
    from soothe.subagents.veritas.schemas import VeritasAnswerSchema

    return VeritasAnswerSchema(defer=True, confidence=0.0, rationale="unsure", answers=[])


# Commands hitting a sub-critical (high) nano rule produce risk hints; the
# critical rm_root does not (it hard-denies / escalates deterministically).
_HINT_COMMAND = "git push -f origin main"


class TestVeritasLeg:
    @pytest.mark.asyncio
    async def test_hint_free_call_never_consults_veritas(self) -> None:
        """Model-call budget: in-workspace, hint-free calls allow at L1."""
        answerer, calls = _answer(_approve())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": "pytest -xvs"})])
        assert await gate.aafter_model(state, _veritas_runtime()) is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_critical_hit_never_consults_veritas(self) -> None:
        """Deterministic rejections/escalations never reach veritas."""
        answerer, calls = _answer(_approve())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": "rm -rf /"})])
        result = await gate.aafter_model(state, _veritas_runtime())
        assert result is not None
        assert calls == []

    @pytest.mark.asyncio
    async def test_hint_call_with_approve_allows_and_mints_grant(self) -> None:
        answerer, calls = _answer(_approve())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        assert await gate.aafter_model(state, _veritas_runtime()) is None
        assert len(calls) == 1
        # The decision subject travels with the request.
        ar = calls[0].metadata["action_requests"][0]
        assert ar["name"] == "run_command"
        assert "git push" in ar["args"]["command"]
        # A prefix grant was minted; a different command with the same first
        # token now clears at L1 with zero further veritas calls.
        state2 = _state([_tc("run_command", {"command": "git push -f origin other"})])
        assert await gate.aafter_model(state2, _veritas_runtime()) is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_hint_call_with_deny_rejects_inline(self) -> None:
        answerer, _ = _answer(_deny())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        result = await gate.aafter_model(state, _veritas_runtime())
        assert result is not None
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.status == "error"
        assert "veritas denied" in tool_msg.content
        # The tool call is stripped from the AI message.
        ai = result["messages"][0]
        assert ai.tool_calls == []

    @pytest.mark.asyncio
    async def test_hint_call_defer_interrupts_when_human_attached(self, monkeypatch) -> None:
        """Veritas defer with a human: standard action_requests interrupt."""
        captured = _stub_interrupt(
            monkeypatch,
            {"decisions": [{"type": "approve"}]},
        )
        answerer, _ = _answer(_defer())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        result = await gate.aafter_model(state, _veritas_runtime(human=True))
        assert len(captured) == 1
        # The hint rule rides along so a human approval can mint a rule override.
        assert captured[0]["escalated_rule_id"]
        # Approved → tool call kept, no rewrite.
        assert result is None

    @pytest.mark.asyncio
    async def test_hint_call_defer_degrades_to_reject_in_autopilot(self) -> None:
        """Veritas defer without a human: instructive reject, no interrupt."""
        answerer, _ = _answer(_defer())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        result = await gate.aafter_model(state, _veritas_runtime(human=False))
        assert result is not None
        tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert tool_msg.status == "error"
        assert "veritas deferred" in tool_msg.content
        ai = result["messages"][0]
        assert ai.tool_calls == []

    @pytest.mark.asyncio
    async def test_veritas_error_preserves_deterministic_outcome(self) -> None:
        async def _boom(request: Any, **kwargs: Any) -> Any:
            raise RuntimeError("down")

        gate = _gate(veritas_answer=_boom)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        assert await gate.aafter_model(state, _veritas_runtime()) is None

    @pytest.mark.asyncio
    async def test_missing_loop_view_skips_veritas_leg(self) -> None:
        answerer, calls = _answer(_deny())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        runtime = _Runtime({"soothe_clarification_mode": "auto"})
        assert await gate.aafter_model(state, runtime) is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_manual_mode_hint_call_goes_to_human_not_veritas(self, monkeypatch) -> None:
        """Manual mode: the human decides hint-carrying calls, veritas does not."""
        captured = _stub_interrupt(
            monkeypatch,
            {"decisions": [{"type": "approve"}]},
        )
        answerer, calls = _answer(_approve())
        gate = _gate(veritas_answer=answerer)
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        runtime = _Runtime(
            {
                "soothe_clarification_mode": "manual",
                "soothe_human_attached": True,
                "soothe_veritas_loop_view": None,
            }
        )
        result = gate.after_model(state, runtime)
        assert len(captured) == 1
        assert calls == []
        assert result is None

    @pytest.mark.asyncio
    async def test_no_veritas_answerer_keeps_sync_behaviour(self) -> None:
        """Without an answerer the gate stays deterministic: hint calls allow."""
        gate = _gate()
        state = _state([_tc("run_command", {"command": _HINT_COMMAND})])
        assert await gate.aafter_model(state, _veritas_runtime()) is None


# ---------------------------------------------------------------------------
# Builder wiring
# ---------------------------------------------------------------------------


class TestBuilderWiring:
    def test_build_auto_mode_gate_from_config(self) -> None:
        from soothe.coreagent.builder import AgentBuilder

        builder = AgentBuilder.__new__(AgentBuilder)
        builder._identity_runtime = None
        builder._intake_only_specs = []

        from types import SimpleNamespace

        ta_cfg = ToolApprovalConfig()
        builder._config = SimpleNamespace(
            agent=SimpleNamespace(
                clarification=SimpleNamespace(
                    tool_approval=ta_cfg,
                    default_mode="auto",
                    force_manual_origins=("plan_mode_review",),
                )
            ),
            security=None,
        )
        gate = builder._build_auto_mode_gate()
        assert isinstance(gate, AutoModeMiddleware)

    def test_gate_none_when_disabled(self) -> None:
        from types import SimpleNamespace

        from soothe.coreagent.builder import AgentBuilder

        builder = AgentBuilder.__new__(AgentBuilder)
        ta_cfg = ToolApprovalConfig(enabled=False)
        builder._config = SimpleNamespace(
            agent=SimpleNamespace(
                clarification=SimpleNamespace(
                    tool_approval=ta_cfg, default_mode="auto", force_manual_origins=()
                )
            ),
            security=None,
        )
        assert builder._build_auto_mode_gate() is None
