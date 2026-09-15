"""LLM-driven plan synthesis from step execution evidence."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from soothe.prompts.graph_wrapper import GraphPromptWrapper
from soothe.sloop.orchestrator.runtime_context import LoopRuntimeContext
from soothe.utils.messages import extract_text_from_message_content

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

_PLAN_SYNTHESIS_HUMAN_TRIGGER = (
    "Synthesize the implementation plan from the research evidence above. "
    "Output only the plan document following the template."
)

# Cap the prior plan body fed back during a refinement pass to avoid
# blowing up the input-token budget on large plans.
_PRIOR_PLAN_MAX_CHARS = 8000

# Maximum number of retry attempts when the LLM returns an empty response
# (i.e., all output was thinking tokens that got stripped, leaving no
# visible plan text). Thinking models occasionally spend their entire
# budget reasoning inside <think> blocks without producing any final
# output text — a retry with a nudge message coaxes the model to emit
# the plan in the visible content channel.
_MAX_EMPTY_RETRIES = 2

# Nudge message appended on retry to steer thinking models toward
# emitting visible (non-thinking) output rather than spending the
# entire budget on internal reasoning.
_EMPTY_RETRY_NUDGE = (
    "Your previous response contained only internal reasoning with no visible "
    "plan document. Output the complete plan now as your final answer — do not "
    "use <think> tags or reasoning blocks, output only the plan document text "
    "following the template."
)


async def synthesize_plan(
    ctx: LoopRuntimeContext,
    *,
    llm: BaseChatModel,
    config: SootheConfig | None = None,
    refinement_comments: str | None = None,
    prior_plan: str | None = None,
) -> str:
    """Generate a plan document from step execution evidence via LLM.

    Args:
        ctx: Loop runtime context with `loop_state` containing the ledger.
        llm: Chat model for the synthesis call.
        config: Optional SootheConfig for ledger projection caps.
        refinement_comments: User-requested plan refinement. When provided
            with `prior_plan`, the LLM revises the prior plan per the comments.
        prior_plan: The previous plan draft being refined.

    Returns:
        Generated plan document text (may be empty on failure).
    """
    state = ctx.loop_state
    goal = state.goal or "No goal specified"

    # Build system prompt from the plan synthesis template.
    system_text = _render_plan_synthesis_system_prompt(
        user_goal=goal,
        workspace=getattr(state, "workspace", None),
        config=config,
    )

    is_refinement = bool((refinement_comments or "").strip()) and bool((prior_plan or "").strip())

    # During refinement, the prior plan is already passed via the refinement
    # trigger. Projecting the ledger here re-injects the old plan body as a
    # "prior goal completion" report, which anchors the LLM on the stale plan
    # and suppresses the requested changes. Skip ledger projection entirely
    # for refinement passes — the refinement trigger carries everything needed.
    if is_refinement:
        messages: list[Any] = [SystemMessage(content=system_text)]
        messages.append(
            HumanMessage(content=_build_refinement_trigger(refinement_comments, prior_plan))
        )
        ledger_msgs: list[Any] = []
    else:
        # Project execute_step ledger messages as evidence.
        wrapper = GraphPromptWrapper(config)
        ledger_cfg = None
        if config is not None:
            ledger_cfg = config.agent.loop.plan_prompt_ledger
        projection = wrapper.project_ledger(
            kind="synthesis",
            state=state,
            ledger_cfg=ledger_cfg,
        )
        ledger_msgs = list(projection.messages)

        # Assemble the message list: system + ledger evidence + human trigger.
        messages = [SystemMessage(content=system_text)]
        messages.extend(ledger_msgs)
        messages.append(HumanMessage(content=_PLAN_SYNTHESIS_HUMAN_TRIGGER))

    approx_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    logger.info(
        "[PlanSynthesis] LLM call starting: evidence_msgs=%d approx_chars=%d refinement=%s",
        len(ledger_msgs),
        approx_chars,
        is_refinement,
    )

    # Retry loop: thinking models (e.g. glm-5.2 with hide_thinking_tokens=True)
    # can spend their entire output budget on internal reasoning, producing
    # zero visible text after thinking-token stripping. On empty output we
    # append a nudge message and retry, up to _MAX_EMPTY_RETRIES times.
    attempt = 0
    while True:
        start = time.perf_counter()
        try:
            response = await llm.ainvoke(messages)
        except Exception:
            logger.exception("[PlanSynthesis] LLM call failed")
            return ""

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        plan_text = _extract_text(response).strip()

        logger.info(
            "[PlanSynthesis] LLM call completed: elapsed_ms=%d plan_chars=%d attempt=%d",
            elapsed_ms,
            len(plan_text),
            attempt,
        )

        if plan_text or attempt >= _MAX_EMPTY_RETRIES:
            return plan_text

        # Empty response on a non-final attempt: nudge and retry.
        logger.warning(
            "[PlanSynthesis] Empty response after thinking-token strip "
            "(attempt=%d/%d); appending nudge and retrying",
            attempt + 1,
            _MAX_EMPTY_RETRIES,
        )
        messages.append(HumanMessage(content=_EMPTY_RETRY_NUDGE))
        attempt += 1


def _render_plan_synthesis_system_prompt(
    *,
    user_goal: str,
    workspace: str | None,
    config: SootheConfig | None = None,
) -> str:
    """Render the plan synthesis system prompt from the Jinja2 template."""
    from soothe.prompts import build_timestamp_xml_footer, load_agent_instructions
    from soothe.prompts.loader import load_prompt_fragment

    template = load_prompt_fragment("instructions/plan_synthesis_system.xml")
    parts = [
        template.render(user_goal=user_goal),
    ]
    if workspace:
        agent_instructions_max_chars = 8000
        if config is not None:
            agent_instructions_max_chars = int(config.agent.agent_instructions_max_chars)
        block = load_agent_instructions(
            workspace,
            headline_max_chars=agent_instructions_max_chars,
        )
        if block:
            parts.append(block)
    parts.append(build_timestamp_xml_footer())
    return "\n\n".join(parts)


def _extract_text(response: Any) -> str:
    """Extract text content from an LLM response (AIMessage or str)."""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", "")
    return extract_text_from_message_content(content)


def _build_refinement_trigger(comments: str | None, prior_plan: str | None) -> str:
    """Build the human message that drives a refinement re-synthesis.

    The operator requested changes to the prior plan. Instruct the LLM to
    revise the prior plan per the comments, preserving what worked and only
    changing what the feedback calls out.
    """
    feedback = (comments or "").strip()
    prior = (prior_plan or "").strip()
    if len(prior) > _PRIOR_PLAN_MAX_CHARS:
        prior = prior[:_PRIOR_PLAN_MAX_CHARS] + "\n…[truncated]"
    return (
        "The operator requested the following refinement to the previous plan "
        "draft. Revise the plan to address the feedback — "
        "keep what was correct and change only what the comments call out. "
        "Output the full revised plan document following the template.\n\n"
        f"## Refinement feedback\n{feedback}\n\n"
        f"## Previous plan draft\n{prior}"
    )


__all__ = ["synthesize_plan"]
