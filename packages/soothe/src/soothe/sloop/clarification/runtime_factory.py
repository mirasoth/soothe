"""Bridge from `SootheConfig` + runtime mode to a `ClarificationPolicy`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from soothe.sloop.clarification.auto import AutoClarificationPolicy
from soothe.sloop.clarification.interactive import EmitFn, InteractiveClarificationPolicy
from soothe.sloop.clarification.protocol import (
    ClarificationPolicy,
    ClarificationRequest,
)
from soothe.sloop.clarification.selector import build_default_clarification_policy
from soothe.sloop.clarification.tool_approval_pipeline import ToolApprovalPipeline
from soothe.subagents.veritas import answer as veritas_answer

if TYPE_CHECKING:
    from soothe.config.models import SootheConfig
    from soothe.subagents.veritas.schemas import VeritasAnswerSchema

logger = logging.getLogger(__name__)


def resolve_clarification_mode(
    requested: str | None,
    config: SootheConfig,
) -> Literal["auto", "manual"]:
    """Pick the effective mode from the request value and the config default.

    Args:
        requested: Per-request mode (typically from the wire payload).
            `None` or unrecognized values fall back to the config default.
        config: Active `SootheConfig` with `agent.clarification.default_mode`.

    Returns:
        `"auto"` or `"manual"`.
    """
    cleaned = (requested or "").strip().lower()
    if cleaned in ("auto", "manual"):
        return cleaned  # type: ignore[return-value]
    return config.agent.clarification.default_mode


def build_clarification_policy_for_runner(
    config: SootheConfig,
    *,
    mode: str | None = None,
    emit: EmitFn | None = None,
    human_attached: bool = False,
    thread_id: str | None = None,
    loop_id: str | None = None,
    interaction_mode: str | None = None,
) -> ClarificationPolicy:
    """Build the clarification policy a runner injects into `LoopRuntimeContext`.

    Args:
        config: Soothe config providing clarification and veritas sub-blocks
            plus the chat-model factory.
        mode: Per-request `auto`/`manual` override; falls back to
            `config.agent.clarification.default_mode`.
        emit: Emit function for early UI notification.
        human_attached: When True and mode is `auto`, wire an
            `InteractiveClarificationPolicy` as the `interactive_fallback`.
        thread_id: Loop thread id used as the Langfuse `session_id`.
        loop_id: Loop id forwarded to Langfuse for trace correlation.
        interaction_mode: CoreAgent interaction mode. `bypass` skips all
            tool-approval deny/safety checks.

    Returns:
        A `ClarificationPolicy` ready to attach to a goal run.
    """
    resolved_mode = resolve_clarification_mode(mode, config)
    clar_cfg = config.agent.clarification
    ta_cfg = clar_cfg.tool_approval

    is_bypass = interaction_mode == "bypass"
    tool_approval_pipeline: ToolApprovalPipeline | None = None
    if ta_cfg.enabled:
        tool_approval_pipeline = ToolApprovalPipeline(
            config=ta_cfg,
            security_config=config.security,
            bypass_security=is_bypass,
        )

    if resolved_mode == "manual":
        # RFC-622 §9b: pipeline pre-filters the human relay in manual mode —
        # deny/safety stages always auto-reject dangerous actions; allow
        # rules auto-approve only when manual_scope is ambiguous_only.
        return build_default_clarification_policy(
            mode="manual",
            emit=emit,
            tool_approval_pipeline=tool_approval_pipeline,
            manual_allow_rules=(ta_cfg.manual_scope == "ambiguous_only"),
        )

    veritas_cfg = config.agent.veritas
    veritas_model = config.create_chat_model(veritas_cfg.model_role)

    # RFC-622 §9b: fast model for tool-approval fallback, think for intent.
    ta_fallback_cfg = ta_cfg.veritas_fallback
    tool_approval_model = veritas_model
    if (
        ta_cfg.enabled
        and ta_fallback_cfg.enabled
        and ta_fallback_cfg.model_role != veritas_cfg.model_role
    ):
        tool_approval_model = config.create_chat_model(ta_fallback_cfg.model_role)

    async def _veritas(request: ClarificationRequest) -> VeritasAnswerSchema:
        # RFC-622 §9b: fast model for tool-approval fallback, think for intent.
        use_ta_fallback = (
            request.origin_node == "tool_approval" and ta_cfg.enabled and ta_fallback_cfg.enabled
        )
        model = tool_approval_model if use_ta_fallback else veritas_model
        max_context_steps = (
            ta_fallback_cfg.max_context_steps if use_ta_fallback else veritas_cfg.max_context_steps
        )
        return await veritas_answer(
            request,
            model=model,
            max_context_steps=max_context_steps,
            soothe_config=config,
            thread_id=thread_id,
            loop_id=loop_id,
            max_retries=veritas_cfg.max_retries,
            retry_backoff_seconds=veritas_cfg.retry_backoff_seconds,
            coerced_confidence=veritas_cfg.coerced_confidence,
        )

    interactive_fallback: ClarificationPolicy | None = (
        InteractiveClarificationPolicy(emit=emit) if human_attached else None
    )

    return build_default_clarification_policy(
        mode="auto",
        veritas_answer=_veritas,
        emit=emit,
        min_confidence=clar_cfg.auto_min_confidence,
        interactive_fallback=interactive_fallback,
        force_manual_origins=list(clar_cfg.force_manual_origins or ()),
        degrade_to_manual_on_failure=clar_cfg.degrade_to_manual_on_failure,
        autopilot_retry_on_fail=clar_cfg.autopilot_retry_on_fail,
        tool_approval_pipeline=tool_approval_pipeline,
    )


def bind_clarification_emit(
    policy: ClarificationPolicy | None,
    emit: EmitFn,
) -> None:
    """Wire runtime `emit` into interactive clarification legs.

    Runners build the policy before the graph `emit` closure exists. Call
    this once `emit` is available so auto→manual upgrades
    (`answer_as_manual_fallback`) can re-notify the TUI before
    `interrupt(...)` pauses the graph.
    """
    if policy is None:
        return
    if isinstance(policy, InteractiveClarificationPolicy):
        policy.bind_emit(emit)
        return
    if isinstance(policy, AutoClarificationPolicy):
        fallback = policy.interactive_fallback
        if isinstance(fallback, InteractiveClarificationPolicy):
            fallback.bind_emit(emit)


__all__ = [
    "bind_clarification_emit",
    "build_clarification_policy_for_runner",
    "resolve_clarification_mode",
]
