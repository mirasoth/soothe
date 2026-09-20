"""Host CoreAgent builder that injects host middleware and tools over the soothe-nano builder."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from soothe_deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from soothe_nano.agent import builder as nano_builder

from soothe.sloop.middleware import (
    GoalStepGuardMiddleware,
    IntakeOnlyTaskGuardMiddleware,
    WestWorldMiddleware,
)
from soothe.sloop.utils.subagent_catalog import partition_subagent_specs

if TYPE_CHECKING:
    from soothe.identity.runtime import IdentityRuntime


class AgentBuilder(nano_builder.AgentBuilder):
    """Soothe AgentBuilder: host injections + intake-only catalog split."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize with empty intake-only spec list and no identity runtime."""
        super().__init__(*args, **kwargs)
        self._intake_only_specs: list[SubAgent | CompiledSubAgent] = []
        self._identity_runtime: IdentityRuntime | None = None

    def _filter_subagents_for_graph(
        self, all_subagents: list[SubAgent | CompiledSubAgent]
    ) -> list[SubAgent | CompiledSubAgent]:
        catalog, intake = partition_subagent_specs(list(all_subagents))
        self._intake_only_specs = intake
        return catalog

    def _host_middleware_prefix(self) -> tuple:
        # Clear intake-only preferred_subagent before nano ToolEnforcement.
        from soothe.identity.middleware import IdentityMiddleware

        prefix: list[Any] = []
        if self._identity_runtime is not None and self._identity_runtime.enabled:
            prefix.append(IdentityMiddleware(self._identity_runtime))
        prefix.append(IntakeOnlyTaskGuardMiddleware())
        auto_mode_gate = self._build_auto_mode_gate()
        if auto_mode_gate is not None:
            prefix.append(auto_mode_gate)
        ask_user_gate = self._build_ask_user_gate()
        if ask_user_gate is not None:
            prefix.append(ask_user_gate)
        return tuple(prefix)

    def _build_ask_user_gate(self) -> Any | None:
        """Build the RFC-635 inline veritas fast path for `ask_user` calls.

        The gate answers confident questions inline (no interrupt, no graph
        hop); defer / failure questions fall through to the station with a
        `gate_deferred` marker so veritas never runs twice. Returns `None`
        when the gate is disabled.
        """
        from soothe.sloop.middleware import AskUserGateMiddleware

        try:
            clar_cfg = self._config.agent.clarification
            veritas_cfg = self._config.agent.veritas
        except AttributeError:
            return None
        if not clar_cfg.ask_user_gate.enabled:
            return None

        model = self._config.create_chat_model(veritas_cfg.model_role)
        config = self._config

        async def _veritas(request: Any, *, thread_id: str | None, loop_id: str | None) -> Any:
            from soothe.subagents.veritas import answer as veritas_answer

            return await veritas_answer(
                request,
                model=model,
                max_context_steps=veritas_cfg.max_context_steps,
                soothe_config=config,
                thread_id=thread_id,
                loop_id=loop_id,
                max_retries=veritas_cfg.max_retries,
                retry_backoff_seconds=veritas_cfg.retry_backoff_seconds,
                coerced_confidence=veritas_cfg.coerced_confidence,
            )

        return AskUserGateMiddleware(
            _veritas,
            min_confidence=clar_cfg.auto_min_confidence,
            default_clarification_mode=clar_cfg.default_mode,
            autopilot_retry_on_fail=clar_cfg.autopilot_retry_on_fail,
        )

    def _build_auto_mode_gate(self) -> Any | None:
        """Build the RFC-634 inline tool-approval gate from config.

        The gate replaces the old ``interrupt_on`` HITL wiring: deny-rule and
        autopilot-safety rejects resolve inline; human-decision cases emit
        the standard ``action_requests`` interrupt. Returns ``None`` when the
        tool-approval block is disabled.
        """
        from soothe.sloop.clarification.tool_approval_pipeline import ToolApprovalPipeline
        from soothe.sloop.middleware import AutoModeMiddleware

        try:
            clar_cfg = self._config.agent.clarification
            ta_cfg = clar_cfg.tool_approval
        except AttributeError:
            return None
        if not (ta_cfg.enabled and ta_cfg.inline_gate.enabled):
            return None
        pipeline = ToolApprovalPipeline(ta_cfg, security_config=self._config.security)
        return AutoModeMiddleware(
            pipeline,
            tools=ta_cfg.inline_gate.tools,
            active_in_bypass=ta_cfg.inline_gate.active_in_bypass,
            default_clarification_mode=clar_cfg.default_mode,
            manual_scope=ta_cfg.manual_scope,
            force_manual_tool_approval="tool_approval" in (clar_cfg.force_manual_origins or ()),
        )

    def _host_middleware_suffix(self) -> tuple:
        # Apply after ToolEnforcement so step/synthesis configurables win.
        from soothe.sloop.middleware import (
            AskUserPromptMiddleware,
            DecomposeTaskMiddleware,
            EvalStepMiddleware,
            GeneralPurposeVariantGuardMiddleware,
        )

        return (
            GoalStepGuardMiddleware(),
            WestWorldMiddleware(),
            DecomposeTaskMiddleware(),
            GeneralPurposeVariantGuardMiddleware(),
            EvalStepMiddleware(),
            AskUserPromptMiddleware(),
        )

    def build(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        """Build the agent graph with host middleware, tools, and kill guards."""
        # Install host daemon kill guards before toolkit resolution (nano hook).
        from soothe.security.daemon_kill_guards import ensure_daemon_kill_guards_installed

        ensure_daemon_kill_guards_installed()

        self._identity_runtime = kwargs.pop("identity_runtime", None)

        # Inject host-only tools not resolved from config.
        from soothe.coreagent.tools import (
            build_ask_user_tool,
            build_request_plan_mode_tool,
        )

        extra_tools = list(kwargs.get("tools") or [])
        extra_tools.append(build_request_plan_mode_tool())
        extra_tools.append(build_ask_user_tool())
        kwargs["tools"] = extra_tools

        # RFC-634: the AutoModeMiddleware host gate (built in
        # ``_host_middleware_prefix`` from ``agent.clarification.tool_approval``)
        # is the sole tool-approval HITL — deny-rule and autopilot-safety
        # rejects resolve inline (no interrupt, no graph round trip) and
        # human-decision cases emit the standard ``action_requests``
        # interrupt. No ``interrupt_on`` is wired for the mutating tools, so
        # the deepagents HumanInTheLoopMiddleware is not installed in agent
        # mode (fs permissions are None there).
        try:
            agent = super().build(*args, **kwargs)
        finally:
            self._identity_runtime = None
        from soothe.coreagent.core_agent import SootheNanoAgent

        if not isinstance(agent, SootheNanoAgent):
            agent.__class__ = SootheNanoAgent
        agent.bind_intake_only_subagents(self._intake_only_specs)
        return agent


def create_soothe_agent(config: Any | None = None, **kwargs: Any):
    """Create SootheNanoAgent with soothe host injections."""
    builder = AgentBuilder(config)
    return builder.build(**kwargs)


__all__ = ["AgentBuilder", "create_soothe_agent"]
