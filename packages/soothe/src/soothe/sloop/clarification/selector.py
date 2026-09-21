"""Build the default clarification policy based on runtime mode."""

from __future__ import annotations

from typing import Literal

from soothe.sloop.clarification.auto import AutoClarificationPolicy, VeritasAnswerFn
from soothe.sloop.clarification.interactive import (
    EmitFn,
    InteractiveClarificationPolicy,
)
from soothe.sloop.clarification.protocol import ClarificationPolicy

ClarificationMode = Literal["manual", "auto"]


def build_default_clarification_policy(
    mode: ClarificationMode,
    *,
    veritas_answer: VeritasAnswerFn | None = None,
    emit: EmitFn | None = None,
    min_confidence: float = 0.4,
    interactive_fallback: ClarificationPolicy | None = None,
    force_manual_origins: tuple[str, ...] | list[str] | None = None,
    degrade_to_manual_on_failure: bool = True,
    autopilot_retry_on_fail: bool = True,
) -> ClarificationPolicy:
    """Return the clarification policy for the given runtime mode.

    Args:
        mode: `manual` for TUI relay, `auto` for veritas.
        veritas_answer: Required for `auto` mode; callable taking a
            `ClarificationRequest`, returning a `VeritasAnswerSchema`.
        emit: Emit function for `InteractiveClarificationPolicy`.
        min_confidence: Confidence threshold for the auto policy.
        interactive_fallback: Policy injected into `AutoClarificationPolicy`.
        force_manual_origins: Origins that skip veritas and use the relay.
        degrade_to_manual_on_failure: Route veritas failures to the
            interactive fallback (TUI only).
        autopilot_retry_on_fail: Return a synthetic retry answer when no
            human is attached, prompting the LLM to try a different action.

    Raises:
        ValueError: `mode == "auto"` without `veritas_answer`.
    """
    if mode == "manual":
        return InteractiveClarificationPolicy(emit=emit)
    if mode == "auto":
        if veritas_answer is None:
            msg = "auto mode requires veritas_answer callable"
            raise ValueError(msg)
        return AutoClarificationPolicy(
            veritas_answer,
            min_confidence=min_confidence,
            interactive_fallback=interactive_fallback,
            force_manual_origins=force_manual_origins,
            degrade_to_manual_on_failure=degrade_to_manual_on_failure,
            autopilot_retry_on_fail=autopilot_retry_on_fail,
        )
    msg = f"unknown clarification mode: {mode!r}"
    raise ValueError(msg)


__all__ = ["ClarificationMode", "build_default_clarification_policy"]
