# IG-780: Structured Coverage Verdict for Eval Steps

**Scope**: `packages/soothe/` (sloop eval + decompose runtime + executor + root_eval + prompts + tests)

## Problem

The ROOT_EVAL coverage gate finalizes a goal whenever the latest Eval step
*executed* successfully, without inspecting the Eval *verdict*
(`root_eval.py:216-222`). The continuation contract relied on the Eval LLM
either calling `decompose_task` (incomplete → subtasks) or returning a "short
completed-coverage verdict" as **free prose** (complete → finalize).

When the Eval LLM instead wrote an "incomplete" verdict as prose — no
`decompose_task` call, no `{"subtasks": [...]}` JSON — the system had no
detection for it: `text_recovery` found no proposals, no new pending steps
materialized, the action tree stayed green, and ROOT_EVAL finalized as
`completed`. Observed in loop `548d` (goal `e3a9aa49` "Fix G-3"): the Eval
(`FJV-EVAL`) emitted 1224 chars of analytical prose, recovered 0 proposals, and
the loop ended without the expected second plan-execute wave.

Root cause: the complete/incomplete distinction lived only in unstructured
prose, so a prose "incomplete" was a structurally invisible dead-end that the
gate silently treated as complete. This violates the No-Keyword-Heuristics rule
(prose cannot be regex-classified), so the fix must make the verdict a binding
structured signal.

## Solution

Replace the free-prose Eval verdict with a structured `coverage_verdict` tool
call. The Eval LLM MUST call `coverage_verdict(complete, reasoning,
remaining_subtasks)` as its terminal action:

- `complete=true` → the only sanctioned signal to finalize.
- `complete=false` with `remaining_subtasks` → the subtasks are queued as a
  `DecompositionProposal` (reusing the existing reconcile path) so new pending
  steps materialize and dispatch fires the next plan-execute wave.
- `complete=false` with no subtasks, or no `coverage_verdict` call at all (prose
  leakage) → ROOT_EVAL refuses to finalize and inserts another bounded Eval
  round to force a structured verdict (capped by `max_eval_rounds`).

The verdict (`complete` + `reasoning`) is carried in the eval step's existing
`execution.outcome` dict — no `StepNode` schema or persistence migration. It
flows through the same `primary_outcome → StepExecutionRecord.outcome →
StepExecution.outcome` path already used by `step_close_report`. ROOT_EVAL reads
`latest.execution.outcome.get("coverage_verdict")`.

`decompose_task` is removed from Eval threads; `coverage_verdict` subsumes its
continuation role (it queues the same `DecompositionProposal` type).

## Changes

### NEW `packages/soothe/src/soothe/sloop/eval/verdict_tool.py`
Executor-bound `coverage_verdict` StructuredTool, mirroring
`decompose/tool.py`:
- `_CoverageVerdictArgs`: `complete: bool`, `reasoning: str`,
  `remaining_subtasks: list[ProposedSubtask] = []`.
- Handler records the verdict dict on the verdict sink (contextvar). When
  `complete=false` and subtasks are present, it builds a `DecompositionProposal`
  (parent = current step id) and appends to the existing proposal sink, so
  reconcile commits continuation steps unchanged.
- Returns a terminal confirmation message.

### `packages/soothe/src/soothe/sloop/decompose/runtime.py`
Add a verdict sink ContextVar + bind/reset/accessor helpers, parallel to the
proposal sink: `_current_verdict_sink`, bound and reset alongside the decompose
runtime tokens. `bind_decompose_runtime` / `reset_decompose_runtime` are
extended to carry a verdict token; `current_verdict_sink()` returns the bound
list.

### `packages/soothe/src/soothe/sloop/engine/execute/executor.py`
- `__init__`: add `self.coverage_verdicts: list[dict] = []` (per-executor sink,
  drained per eval step).
- Bind the verdict sink into the decompose runtime at the existing
  `bind_decompose_runtime` call (executor.py:2486).
- After an eval step's stream, drain `self.coverage_verdicts` into
  `primary_outcome["coverage_verdict"]` (last verdict wins), near the existing
  eval text-recovery block (executor.py:3071). Clear the sink after draining.

### `packages/soothe/src/soothe/sloop/eval/middleware.py`
`EvalStepMiddleware` injects `coverage_verdict` instead of `decompose_task`.
`modify_request` ensures `coverage_verdict` is present on Eval threads; the
system addendum (updated below) mandates its use.

### `packages/soothe/src/soothe/sloop/stations/decompose/root_eval.py`
- `_eval_envelope`: replace instructions #5/#6 to mandate a terminal
  `coverage_verdict` call (complete=true OR complete=false+remaining_subtasks).
- `process`: at the `latest.status == "completed"` branch, read the verdict
  from `latest.execution.outcome.get("coverage_verdict")`. Finalize only when a
  `complete=true` verdict is present. Otherwise fall through to the bounded
  Eval insertion (the intake-label / `eval_required` gates are guarded to apply
  only when no prior eval exists), forcing a re-audit. `max_eval_rounds` bounds
  repeated re-audits → fatal.

### `packages/soothe/src/soothe/prompts/fragments/eval/eval_policy_system.xml`
Rewrite "When work remains" / "When the goal is complete" to mandate the
`coverage_verdict` tool as the single terminal action for both outcomes (no
prose verdict).

### NEW `packages/soothe/src/soothe/prompts/fragments/eval/coverage_verdict_tool.xml`
Tool description for `coverage_verdict`, mirrored from
`decompose/decompose_task_tool.xml`. Registered in
`prompts/fragments/__init__.py` as `COVERAGE_VERDICT_TOOL_DESCRIPTION`.

## Tests (`packages/soothe/tests/unit/`)

- `coverage_verdict` tool: complete=true records verdict, queues no proposal;
  complete=false with subtasks queues a `DecompositionProposal` on the sink.
- ROOT_EVAL: latest eval with `complete=true` verdict → finalize; latest eval
  with no verdict (prose leakage) → does not finalize, inserts another eval
  round (bounded by `max_eval_rounds`); latest eval complete=false with
  materialized subtasks → action tree not green → dispatch (existing path).
- Executor: verdict sink drained into `primary_outcome["coverage_verdict"]`.
