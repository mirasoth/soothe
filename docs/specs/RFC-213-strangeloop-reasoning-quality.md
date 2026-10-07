# RFC-213: StrangeLoop Reasoning Quality & Robustness

**RFC**: 213
**Title**: StrangeLoop Reasoning Quality & Robustness
**Status**: Draft
**Kind**: Architecture Design
**Created**: 2026-04-17
**Updated**: 2026-08-20
**Authors**: Claude Code
**Depends on**: RFC-200, RFC-203
**Related**: RFC-207, RFC-214, RFC-603, RFC-604, RFC-904
**Partially Superseded By**: RFC-904 (per-iteration assess+generate pair → `decompose_task`); RFC-905 (coverage Eval thread; GapResult / assess-only ROOT_EVAL withdrawn)

---

## Abstract

This RFC defines StrangeLoop reasoning quality enhancements through two-phase Plan architecture. Current runtime behavior removes progressive-planning requirements and instead grounds `plan-generate` with a bounded pre-generate evidence probe before generation. `PlanGeneration` now emits flattened decision fields rather than a nested `decision` object.

> **Supersession note (RFC-904 / RFC-905):** When recursive decomposition cuts over, the per-iteration assess + plan-generate pair is obsolete. Plan generation folds into executor-bound **`decompose_task`**. Coverage assessment is the RFC-905 **Eval thread** (fresh readonly CoreAgent + `decompose_task`), not assess-only ROOT_EVAL / `PlanGapAnalysis` / `StatusAssessment`.

---

## Reasoning Quality Progressive Actions (Historical)

### Progressive Plan Decisions

Evidence-driven strategy refinement through progressive decision-making:

**Progressive Decision Pattern**:
- Initial Plan: Broad strategy, coarse steps
- Mid-execution: Strategy refinement based on evidence
- Final Plan: Fine-grained steps based on learned context

### Evidence-Driven Strategy

**Evidence collection patterns**:
- Tool results: Success/failure, output length, error patterns
- Subagent results: Completion status, iteration count, evidence summaries
- Metrics: Wave metrics (tool call count, subagent tasks, errors)

**Strategy refinement triggers**:
- Evidence contradicts plan assumptions → replan
- Evidence confirms plan validity → continue
- Evidence indicates goal completion → done

### Progressive Action Implementation

```python
class ProgressiveActionStrategy(BaseModel):
    """Evidence-driven progressive action strategy."""

    evidence_threshold: float = 0.7
    """Threshold for strategy refinement decision."""

    replan_on_failure_count: int = 2
    """Failure count threshold triggering replan."""

    continue_on_success_rate: float = 0.8
    """Success rate threshold for continue decision."""

    evidence_weights: dict[str, float] = {
        "tool_success": 0.4,
        "output_quality": 0.3,
        "error_rate": 0.2,
        "iteration_progress": 0.1,
    }
    """Weighted evidence factors for decision."""
```

**Decision Logic**:
```python
def evaluate_progressive_decision(
    evidence: WaveEvidence,
    strategy: ProgressiveActionStrategy,
) -> Literal["continue", "replan", "done"]:
    # Calculate evidence score
    score = sum(
        evidence.factors[factor] * strategy.evidence_weights[factor]
        for factor in strategy.evidence_weights
    )

    # Progressive decision thresholds
    if score >= strategy.evidence_threshold:
        return "done" if evidence.goal_achieved else "continue"
    elif evidence.failure_count >= strategy.replan_on_failure_count:
        return "replan"
    else:
        return "continue"  # Default: maintain strategy
```

---

## Two-Phase Plan Architecture

> **Removed 2026-10-07 — superseded by RFC-904.** The per-iteration
> assess+generate pair (`StatusAssessment` + `PlanGeneration` merged into
> `PlanResult` via `LLMPlanner._combine_results`) is obsolete; plan generation
> folds into executor-bound `decompose_task` (RFC-904 §`decompose_task` Tool).
> Coverage assessment is the RFC-905 **Eval thread**, not assess-only
> ROOT_EVAL / `PlanGapAnalysis` / `StatusAssessment`. Normative field lists and
> merge behavior previously documented here now live in RFC-604 and
> `soothe.core.strange_loop.state.schemas`.

---

## Reasoning Flow Integration

> **Removed 2026-10-07 — superseded by RFC-904.** The combined reasoning
> process diagram (PLAN Phase → Two-Phase Plan Architecture → Progressive
> Action Strategy → EXECUTE Phase → Progressive decision logic) only existed
> to support the per-iteration assess+generate pair, which folds into
> executor-bound `decompose_task` (RFC-904).

---

## Configuration

```yaml
agentic:
  reasoning:
    progressive_actions:
      evidence_threshold: 0.7
      replan_on_failure_count: 2
      continue_on_success_rate: 0.8
      evidence_weights:
        tool_success: 0.4
        output_quality: 0.3
        error_rate: 0.2
        iteration_progress: 0.1

    # two_phase_plan: removed 2026-10-07 — superseded by RFC-904 decompose_task.
```

---

## Implementation Status

- ✅ Progressive action strategy model
- ✅ Evidence-driven decision logic
- ✅ Two-phase Plan architecture (StatusAssessment + PlanGeneration)
- ✅ Token efficiency optimization
- ✅ LLMPlanner integration
- ✅ Combined reasoning flow
- ⚠️ Evidence weight tuning (ongoing)

---

## References

- RFC-200: StrangeLoop Plan-Execute Loop Architecture
- RFC-203: StrangeLoop State & Memory Architecture
- RFC-603: Reasoning Quality Progressive Actions; §3.2 documents `goal_progress` as assess-model output only (IG-376)
- RFC-604: Plan Phase Robustness; abstract notes `goal_progress` / `confidence` post-processing split
- RFC-214: Loop message surface — plan-context `Goal` + `Execute iteration` header for assess

---

## Changelog

### 2026-10-07
- Removed superseded Two-Phase Plan Architecture and Reasoning Flow Integration sections (per RFC-904 / RFC-905). Historical Progressive Action Strategy retained as design-decision record.

### 2026-05-04
- Aligned with IG-376 / RFC-603 §3.2 / RFC-604 / RFC-214 for StatusAssessment `goal_progress` and plan human formatting; IG-329 trimmed `PlanGeneration` schema and added `plan_generate_instructions.xml`.

### 2026-04-17
- Consolidated progressive-action strategy and two-phase Plan architecture into unified reasoning quality design.

---

*StrangeLoop reasoning quality through progressive evidence-driven strategy refinement and two-phase Plan architecture for token efficiency.*