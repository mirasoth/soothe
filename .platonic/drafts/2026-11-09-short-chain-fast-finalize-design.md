# Short-Chain Fast Finalize

**Status**: Draft
**Date**: 2026-11-09
**Origin**: Loop 46a7 diagnosis (`01a1be4b-3a45-72b0-b1c8-b2108cd746a7`)

## Problem

Loop 46a7 (BRAINSTORM-mode WKR design discussion) wasted ~81s + 6 tool calls
because the pipeline forced three unnecessary layers after the first step
already produced the deliverable:

| Step | Time | Tools | Outcome |
|------|------|-------|---------|
| PLN-01 | 169s | 24 | Produced valid design draft with quasitons. Ended with `ask_user` gate (correct BRAINSTORM behavior). |
| root_eval SIMPLE decision | — | — | `should_run=True`. LLM misread the `ask_user` gate as incompleteness. |
| CWD-EVAL | 60s | 6 | Coverage audit. `coverage_verdict=complete=True`. Pure waste. |
| synthesis | 21s | — | `general_summary` re-summarized the two steps into 3248 chars. |

### Root causes

1. **Typesafe eval decision is text-only.** `decide_eval_coverage_typesafe`
   reads `goal` + `step_history` text. It cannot see that PLN-01 produced an
   artifact file and ended with an `ask_user` interrupt — the BRAINSTORM-mode
   completion signal. The LLM fallback then misreads the pause as
   incompleteness.

2. **`final_response` defaults to `always_synthesize`.** This forces
   `determine_completion_strategy` to return `synthesize` unconditionally,
   skipping `LEDGER_DIRECT` entirely. LEDGER_DIRECT never runs in the
   default configuration.

3. **LEDGER_DIRECT gate is too narrow.** `_ledger_direct_eligible` requires
   `plan_wave_count <= 1`. A 2-step linear chain across 2 plan waves
   (PLN-01 -> CWD-EVAL) does not qualify, so even under `auto` mode
   synthesis would fire.

4. **No "final step output IS the report" passthrough.** Before synthesis,
   there is no typesafe check asking whether the last step's output already
   answers the user goal. `LEDGER_DIRECT` uses `last_ledger_ai_content`
   (raw ledger text), not a structured "is this a valid goal report?"
   decision.

## Design

### Layer 1 — Config: remove `always_synthesize`

Remove the `final_response` config field
(`AgenticFinalResponseMode = Literal["auto", "always_synthesize"]`).
LEDGER_DIRECT is always the first candidate (what `auto` mode does today,
but always on). Synthesis is the fallback.

`determine_completion_strategy` no longer checks for `always_synthesize` —
it always tries LEDGER_DIRECT first, falling back to SYNTHESIZE when
structural gates or content checks fail.

**Migration**: The `final_response` field and its validator are removed from
`SootheConfig`. Existing configs with `final_response: always_synthesize` are
silently ignored (Pydantic `extra=ignore` or explicit deprecation log). The
`normalize_agentic_final_response_mode` function is removed.

**Files**:
- `config/models.py` — remove `AgenticFinalResponseMode`, `final_response`
  field, `_normalize_final_response` validator,
  `normalize_agentic_final_response_mode`
- `context/planning_completion.py` — remove `always_synthesize` branch in
  `determine_completion_strategy`

### Layer 2 — Relax LEDGER_DIRECT gate: DAG topology

Replace `plan_wave_count <= 1` in `_ledger_direct_eligible` with DAG topology
checks derived from the `StepDAG`:

- **No parallel branches**: every step has at most 1 dependent step
  (max forward fanout = 1). This ensures the chain is strictly linear.
- **`chain_depth <= N`** (default N=3): the longest dependency chain in the
  DAG. Already computed by `DagPlanningContext.chain_depth`.
- **`total_steps <= M`**: existing `simple_ledger_direct_max_steps` gate
  retained.

Keep existing gates: `failed_steps == 0`,
`last_wave_tool_call_count <= ledger_direct_max_tool_calls`.

The DAG topology check is computed from `StepDAG.nodes` by examining
`dependencies` and `parent_step_id` relationships. A helper
`_is_linear_chain(dag, max_depth)` returns `True` when:

1. No node has more than 1 node depending on it (forward fanout <= 1).
2. The maximum dependency chain length <= `max_depth`.
3. No decomposition children (`parent_step_id is None` for root, single
   root only).

**Files**:
- `context/planning_completion.py` — add `_is_linear_chain` helper, update
  `_ledger_direct_eligible` to use it instead of `plan_wave_count <= 1`
- `context/planning_step_planner.py` — pass DAG topology to
  `determine_completion_strategy` (replace `plan_wave_count` with
  `is_linear_chain` + `chain_depth`)

### Layer 3 — Enhanced typesafe eval decision: structured signals

Add structured fields to the typesafe classifier state beyond the current
text-only `goal` + `step_history`:

| Signal | Source | Meaning |
|--------|--------|---------|
| `close_report_requires_eval` | `StepCloseReport.requires_eval` for all steps | `False` for all steps -> coverage likely complete |
| `step_kind` | `StepNode.kind` of the last completed step | `eval` -> no need for another eval; `brainstorm`/`discuss` -> ask_user gate = completion |
| `ask_user_interrupt` | Detect `ask_user` / `AskUserQuestion` tool call in the last step's execution output | BRAINSTORM-mode completion signal |
| `artifact_produced` | Infer from outcome text (file paths like `.platonic/drafts/...`) or `StepCloseReport.recommendations` mentioning deliverables | Deliverable produced -> goal likely achieved |

These are passed as structured fields in the `ClassifierRequest.state` dict,
alongside the existing `goal` and `step_history` text fields.

**Criteria update**: The `_CRITERIA` dict in `typesafe_eval_decision.py` is
updated to reference these signals:

- `no_eval`: "The close reports show `requires_eval=False`, the step produced
  an artifact, and (for brainstorm/discuss steps) the step ended with an
  `ask_user` gate — all indicating the goal is fully achieved."
- `eval`: "Coverage is uncertain: `requires_eval=True`, no artifact produced,
  or the step ended without a completion signal."

The asymmetry is preserved: `no_eval` (suppress) requires
`suppress_min_confidence`; `eval` (fail-safe) just needs a trusted verdict.

**Artifact detection (initial heuristic)**: Infer `artifact_produced` by
searching the step's `execution.outcome` text for file-path patterns
(`.platonic/drafts/`, `.platonic/specs/`, `.py`, `.md`, etc.). This is a
heuristic; a future schema change can add `StepCloseReport.artifacts:
list[str]` for reliable population by CoreAgent.

**Files**:
- `sloop/eval/typesafe_eval_decision.py` — add structured fields to
  `ClassifierRequest.state`, update `_CRITERIA`
- `sloop/eval/eval_decision.py` — extract structured signals from
  `StepNode` list and pass to typesafe path
- `sloop/stations/decompose/root_eval.py` — pass `goal.steps.nodes` to
  `decide_eval_required` (already passed, but ensure structured extraction)

### Layer 4 — LEDGER_DIRECT content + typesafe report validation

#### Content source: step execution output

Instead of `last_ledger_ai_content(state)` (last non-planning AI message from
the ledger), use the final completed step's structured execution output:

1. Find the last completed step in the DAG (by `plan_iteration` desc, then
   `id`).
2. Extract AI text from `step.execution.output_messages` (concatenate AI
   message content).
3. If empty, fall back to `last_ledger_ai_content(state)` (existing behavior).

This captures the actual deliverable (the design draft text), not just the
last ledger message which might be a tool result or intermediate turn.

**New helper**: `last_step_output_content(state, goal_steps) -> str` in
`sloop/utils/messages.py` or a new module.

#### Typesafe report validation gate

Before committing to LEDGER_DIRECT passthrough, a new typesafe classifier
checks: "Is the final step output a valid goal report?"

- **Criteria**:
  - `valid`: "The output is substantive (not a tool log or one-liner),
    addresses the user goal directly, and is coherent enough to serve as
    the final goal report without further synthesis."
  - `invalid`: "The output is a tool call log, a one-line status, a
    clarifying question without substantive content, or does not address
    the user goal."
- **Asymmetry**: `valid` (passthrough/suppress synthesis) requires
  `suppress_min_confidence`; `invalid` (synthesize) just needs trusted
  verdict.
- **Fallback**: unavailable/untrusted -> SYNTHESIZE (fail-safe).

**New module**: `sloop/engine/completion/report_validation_typesafe.py`
following the pattern of `scenario_typesafe.py` /
`typesafe_eval_decision.py`.

**Files**:
- `sloop/utils/messages.py` — add `last_step_output_content` helper
- `sloop/engine/completion/report_validation_typesafe.py` — new typesafe
  report validation
- `sloop/stations/completion/finalize.py` — call report validation before
  LEDGER_DIRECT passthrough; if invalid/untrusted, fall back to SYNTHESIZE
- `sloop/engine/completion/synthesis.py` — no change needed (synthesis
  still runs as fallback)

### Fallback chain

```
determine_completion_strategy
  │
  ├─ _dag_requires_synthesis (failed steps, deep chain, subagent cap, ...)
  │   └─ True → SYNTHESIZE
  │
  ├─ empty ledger → SYNTHESIZE
  │
  ├─ _ledger_direct_eligible (DAG topology: linear, chain_depth <= 3, ...)
  │   └─ False → SYNTHESIZE
  │   └─ True → LEDGER_DIRECT candidate
  │        │
  │        ├─ last_step_output_content (step execution output)
  │        │   └─ empty → fall back to last_ledger_ai_content
  │        │        └─ empty → SYNTHESIZE
  │        │
  │        └─ typesafe report validation
  │             ├─ valid → LEDGER_DIRECT passthrough (step output = goal report)
  │             ├─ invalid → SYNTHESIZE
  │             └─ unavailable/untrusted → SYNTHESIZE (fail-safe)
  │
  └─ default → SYNTHESIZE
```

### What this would have done for loop 46a7

1. **PLN-01 completes** with design draft at
   `.platonic/drafts/2026-11-09-wkr-design.md` + `ask_user` gate.
2. **root_eval**: enhanced typesafe eval decision sees
   `close_report_requires_eval=False`, `step_kind=brainstorm`,
   `ask_user_interrupt=True`, `artifact_produced=True` -> `no_eval` (skip
   coverage eval) -> finalize. **Saves 60s + 6 tool calls.**
3. **finalize**: `determine_completion_strategy` checks DAG topology — PLN-01
   is a single linear step, no parallel branches, `chain_depth=1` ->
   LEDGER_DIRECT eligible.
4. **LEDGER_DIRECT content**: PLN-01's execution output (the design draft
   text) -> typesafe report validation: "valid report" -> passthrough as
   goal report. **Saves 21s synthesis.**
5. **Total savings**: ~81s + 6 tool calls + ~87k tokens.

## Implementation surface

| File | Change |
|------|--------|
| `config/models.py` | Remove `AgenticFinalResponseMode`, `final_response` field, validator |
| `context/planning_completion.py` | Remove `always_synthesize` branch; add `_is_linear_chain`; update `_ledger_direct_eligible` gate |
| `context/planning_step_planner.py` | Pass DAG topology to `determine_completion_strategy` |
| `sloop/eval/typesafe_eval_decision.py` | Add structured signals to classifier state; update `_CRITERIA` |
| `sloop/eval/eval_decision.py` | Extract structured signals from StepNode list; pass to typesafe path |
| `sloop/stations/decompose/root_eval.py` | Ensure structured signals are extracted from StepDAG |
| `sloop/utils/messages.py` | Add `last_step_output_content` helper |
| `sloop/engine/completion/report_validation_typesafe.py` | New module: typesafe report validation gate |
| `sloop/stations/completion/finalize.py` | Wire up report validation before LEDGER_DIRECT passthrough |

## Open questions

1. **Artifact detection**: heuristic (infer from outcome text) vs schema change
   (`StepCloseReport.artifacts: list[str]`). Design uses heuristic initially;
   schema change deferred to a follow-up if the heuristic proves unreliable.

2. **`ask_user_interrupt` detection**: should we inspect the step's
   `execution.output_messages` for `ask_user` tool calls, or should the
   executor set a structured flag on `StepCloseReport` when it detects an
   `ask_user` interrupt? The former is heuristic; the latter is reliable but
   requires executor changes.

3. **Config migration**: should existing configs with
   `final_response: always_synthesize` emit a deprecation warning, or be
   silently ignored? Design assumes silent ignore (Pydantic `extra=ignore`).

## Risks

- **False positive passthrough**: the typesafe report validation might
  passthrough a weak output. Mitigation: the suppression bar
  (`suppress_min_confidence`) gates the passthrough direction; uncertain
  verdicts fall back to synthesis.

- **Linear chain with incomplete coverage**: a 2-step linear chain where
  step 2 failed would not qualify (failed_steps == 0 gate retained).

- **Typesafe classifier unavailability**: all typesafe gates fall back to
  the existing LLM / synthesis path. No regression when the classifier is
  disabled.
