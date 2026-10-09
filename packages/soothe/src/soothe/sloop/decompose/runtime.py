"""Runtime context for executor-bound `decompose_task`."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from soothe.context.decomposition import DecompositionProposal

logger = logging.getLogger(__name__)

_current_step_id: ContextVar[str | None] = ContextVar("decompose_step_id", default=None)
_wave_seq: ContextVar[int] = ContextVar("decompose_wave_seq", default=0)
_proposal_sink: ContextVar[list[DecompositionProposal] | None] = ContextVar(
    "decompose_proposal_sink", default=None
)
_verdict_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "coverage_verdict_sink", default=None
)


@dataclass
class DecomposeRuntimeTokens:
    """Tokens to reset after a step thread finishes."""

    step: Token[str | None]
    wave: Token[int]
    sink: Token[list[DecompositionProposal] | None]
    verdict: Token[list[dict[str, Any]] | None] | None = None


def bind_decompose_runtime(
    *,
    step_id: str,
    sink: list[DecompositionProposal],
    wave_seq: int = 0,
    verdict_sink: list[dict[str, Any]] | None = None,
) -> DecomposeRuntimeTokens:
    """Bind step id, proposal sink, and optional verdict sink for a CoreAgent turn."""
    logger.debug(
        "[decompose] bind runtime step=%s wave=%d sink_id=%d sink_bound=%s verdict_bound=%s",
        step_id,
        wave_seq,
        id(sink),
        sink is not None,
        verdict_sink is not None,
    )
    verdict_token: Token[list[dict[str, Any]] | None] | None = None
    if verdict_sink is not None:
        verdict_token = _verdict_sink.set(verdict_sink)
    return DecomposeRuntimeTokens(
        step=_current_step_id.set(step_id),
        wave=_wave_seq.set(wave_seq),
        sink=_proposal_sink.set(sink),
        verdict=verdict_token,
    )


def reset_decompose_runtime(tokens: DecomposeRuntimeTokens) -> None:
    """Restore prior contextvar values."""
    step_id = _current_step_id.get()
    sink = _proposal_sink.get()
    verdict = _verdict_sink.get()
    queued = len(sink) if sink else 0
    verdict_count = len(verdict) if verdict else 0
    logger.debug(
        "[decompose] reset runtime step=%s proposals_queued=%d verdicts_queued=%d",
        step_id,
        queued,
        verdict_count,
    )
    _current_step_id.reset(tokens.step)
    _wave_seq.reset(tokens.wave)
    _proposal_sink.reset(tokens.sink)
    if tokens.verdict is not None:
        _verdict_sink.reset(tokens.verdict)


def current_step_id() -> str | None:
    """Return the decompose step id for the current task context, if any."""
    return _current_step_id.get()


def current_wave_seq() -> int:
    """Return the current execution wave sequence number."""
    return _wave_seq.get()


def current_proposal_sink() -> list[DecompositionProposal] | None:
    """Return the proposal sink list for the current task context, if any."""
    return _proposal_sink.get()


def current_verdict_sink() -> list[dict[str, Any]] | None:
    """Return the coverage-verdict sink list bound for the current eval step, if any."""
    return _verdict_sink.get()


def langgraph_configurable() -> dict[str, Any]:
    """Return the LangGraph `configurable` dict for the current task context.

    Shared by the decompose middleware and tool handler to read workspace /
    step-binding keys without duplicating the `get_config` boilerplate.
    Returns `{}` when no LangGraph runtime context is active.
    """
    try:
        from langgraph.config import get_config

        lg_cfg = get_config()
    except Exception:
        return {}
    if not isinstance(lg_cfg, dict):
        return {}
    conf = lg_cfg.get("configurable")
    return conf if isinstance(conf, dict) else {}
