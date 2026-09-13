"""Tests for intake system prompt assembly."""

from __future__ import annotations

from soothe.sloop.intention.prompts import (
    INTAKE_CLASSIFY_HUMAN_TASK,
    INTAKE_CLASSIFY_SYSTEM_PROMPT,
    build_intake_system_prompt,
    build_prompt_timestamp_block,
)
from soothe.utils.prompt_clock import prompt_datetime_context


def test_build_prompt_timestamp_block_includes_live_values() -> None:
    block = build_prompt_timestamp_block()
    ctx = prompt_datetime_context()
    assert block.startswith("<PROMPT_TIMESTAMP>")
    assert block.endswith("</PROMPT_TIMESTAMP>")
    assert ctx["current_date"] in block
    assert ctx["current_time"] in block
    assert ctx["schedule_timezone"] in block


def test_build_intake_system_prompt_includes_identity_and_timestamp() -> None:
    # Capture the datetime context once and pass it to the builder so the
    # assertions use the same snapshot — no boundary-second race.
    ctx = prompt_datetime_context()
    prompt = build_intake_system_prompt(INTAKE_CLASSIFY_SYSTEM_PROMPT, "Soothe", ctx=ctx)

    assert prompt.startswith("<ASSISTANT_IDENTITY>")
    assert "<PROMPT_TIMESTAMP>" in prompt
    assert ctx["current_date"] in prompt
    assert ctx["current_time"] in prompt
    assert ctx["schedule_timezone"] in prompt
    assert "<INTAKE_CLASSIFY>" in prompt
    assert prompt.index("<ASSISTANT_IDENTITY>") < prompt.index("<INTAKE_CLASSIFY>")
    assert prompt.index("<INTAKE_CLASSIFY>") < prompt.index("<PROMPT_TIMESTAMP>")
    assert prompt.endswith("</PROMPT_TIMESTAMP>")


def test_build_intake_system_prompt_formats_assistant_name_in_examples() -> None:
    prompt = build_intake_system_prompt(INTAKE_CLASSIFY_SYSTEM_PROMPT, "Soothe")

    assert "I'm Soothe, a helpful AI assistant invented by Dr. Xiaming Chen" in prompt
    assert "我是Soothe" in prompt
    assert "FORBIDDEN identity reply" in prompt
    assert "never output this" in prompt
    assert "(per ASSISTANT_IDENTITY)" not in prompt
    assert "{assistant_name}" not in prompt
    assert "{assistant_creator}" not in prompt
    assert "{assistant_role}" not in prompt
    assert "{assistant_vendor_denylist}" not in prompt


def test_intake_human_task_avoids_identity_priming() -> None:
    assert INTAKE_CLASSIFY_HUMAN_TASK == "Classify the user message above. JSON only."
    assert "Identity replies" not in INTAKE_CLASSIFY_HUMAN_TASK


# ── Complexity-calibration regression guards ─────────────────────────────────
# These assert the prompt carries the operational complexity criteria and the
# anti-over-classification rule. Without them the LLM reverts to labeling any
# analysis/research as complex, inflating complex routing (observed in loop
# b976 where every continuation goal was complex). See TaskComplexity docstring.


def test_prompt_has_operational_complexity_criteria() -> None:
    prompt = INTAKE_CLASSIFY_SYSTEM_PROMPT
    # Complex is defined by structural independence, not by "is it analysis".
    assert "2+ INDEPENDENT areas/modules/deliverables" in prompt
    assert "could proceed in parallel" in prompt


def test_prompt_has_anti_over_classification_rule() -> None:
    prompt = INTAKE_CLASSIFY_SYSTEM_PROMPT
    assert "Do NOT label work complex merely because" in prompt
    assert "A single cohesive analysis or report is simple" in prompt


def test_prompt_grounds_complex_verdict_in_reasoning() -> None:
    """The reasoning must name the independent areas for complex goals —
    chain-of-thought grounding that improves classification accuracy."""
    prompt = INTAKE_CLASSIFY_SYSTEM_PROMPT
    assert "name the 2+ independent areas/phases" in prompt


def test_prompt_single_deliverable_examples_are_simple() -> None:
    """Few-shot calibration: single analysis/report/plan deliverables are
    simple, not complex."""
    prompt = INTAKE_CLASSIFY_SYSTEM_PROMPT
    assert '"research the market for electric bikes" → is_task:true' in prompt
    assert (
        '"research the market for electric bikes" → is_task:true,'
        " response_language:en, social_response:null, task_complexity:simple"
    ) in prompt
    assert '"analyze last quarter\'s sales data and summarize trends" → is_task:true' in prompt
    assert "task_complexity:simple" in prompt.split("analyze last quarter")[1].splitlines()[0]


def test_prompt_has_genuinely_multi_area_complex_examples() -> None:
    """Few-shot calibration: complex examples must show 2+ named independent
    areas (so the model learns complex = multi-area, not = big/analysis)."""
    prompt = INTAKE_CLASSIFY_SYSTEM_PROMPT
    assert '"implement authentication, billing, and notifications"' in prompt
    assert (
        '"implement authentication, billing, and notifications" → is_task:true,'
        " response_language:en, social_response:null, task_complexity:complex"
    ) in prompt
    assert '"migrate the monolith into the orders, payments, and shipping services"' in prompt
