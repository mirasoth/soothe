# RFC-SLP-004: Short-Chain Fast Finalize

**Status**: Draft
**Kind**: Architecture Design
**Created**: 2026-11-09
**Authors**: Soothe Team
**Updated**: 2026-11-09
**Depends on**: RFC-SLP-003, RFC-LOOP-009
**Related**: RFC-SLP-002, RFC-LOOP-025

---

## 1. Abstract

When a goal executes as a short linear step chain (no parallel branches,
chain depth <= 3) and the final step already produced a substantive
deliverable, the pipeline MUST skip the coverage Eval step and use the
final step's output directly as the goal report — without a synthesis LLM
call. This RFC specifies the integrated fast-finalize path: config
simplification (remove `always_synthesize`), DAG-topology-based
`LEDGER_DIRECT` gate, enhanced typesafe eval decision with structured
close-report signals, and a typesafe report validation gate before
passthrough.

---

## 2. Scope and Non-Goals

### 2.1 Scope

This RFC defines:

* The removal of the `final_response` config field and the
  `always_synthesize` mode.
* A DAG-topology-based eligibility gate for `LEDGER_DIRECT` that replaces
  the `plan_wave_count <= 1` structural check.
* Enhanced typesafe eval decision signals: `close_report.requires_eval`,
  `step_kind`, `ask_user_interrupt`, and `artifact_produced`.
* A typesafe report validation gate that decides whether the final step
  output is a valid goal report (passthrough) or requires synthesis.
* The content source for `LEDGER_DIRECT` passthrough: the final completed
  step's execution output.

### 2.2 Non-Goals

* Schema changes to `StepCloseReport` (adding an `artifacts` field is
  deferred to a follow-up; the initial design infers artifacts from
  outcome text).
* Changes to the Eval thread's readonly tool set or `decompose_task`
  semantics (RFC-SLP-003 scope).
* Changes to the synthesis LLM prompt, scratchpad mode, or scenario
  classification (the synthesis path remains the unchanged fallback).
* LoopRail `evaluate_goal_completion` or consensus logic.

---

## 3. Background & Motivation

### 3.1 Observed waste in loop 46a7

A BRAINSTORM-mode goal produced a valid first-principles design draft
(24 tools, 169s) ending with an `ask_user` gate — the correct completion
signal for BRAINSTORM mode. The pipeline then:

1. Ran a SIMPLE eval decision whose LLM fallback misread the `ask_user`
   gate as incompleteness (`should_run=True`).
2. Inserted a coverage Eval step (60s, 6 tools) that confirmed
   `coverage_verdict=complete=True`.
3. Ran a full synthesis LLM call (21s, 127 chunks) to re-summarize the
   two steps into 3248 chars — even though the first step's output was
   the deliverable.

Total waste: ~81s + 6 tool calls + ~87k tokens.

### 3.2 Root causes

1. **`final_response` defaults to `always_synthesize`**, which forces
   `determine_completion_strategy` to return `synthesize` unconditionally,
   skipping `LEDGER_DIRECT` entirely.
2. **`LEDGER_DIRECT` gate is too narrow**: `plan_wave_count <= 1` excludes
   linear chains that span multiple plan waves (e.g., PLN-01 -> CWD-EVAL).
3. **Typesafe eval decision is text-only**: it cannot see structured
   completion signals (artifact produced, `ask_user` gate, step kind).
4. **No "final step output IS the report" passthrough**: the content
   source is `last_ledger_ai_content` (raw ledger text), and there is no
   typesafe check that the output is a valid goal report.

---

## 4. Design Principles

1. **Fail-safe**: every typesafe gate falls back to the existing LLM or
   synthesis path when unavailable or untrusted. No regression when the
   classifier is disabled.

2. **Asymmetric suppression**: directions that suppress work (skip eval,
   skip synthesis, passthrough report) require `suppress_min_confidence`;
   directions that match the fail-safe (run eval, synthesize) only need a
   trusted verdict.

3. **Structural first, content second**: the DAG-topology gate is purely
   structural (no content inspection). Only when the structure qualifies
   does the typesafe content validation run.

4. **No new completion strategy enum**: the design extends the existing
   `LEDGER_DIRECT` strategy rather than adding a `STEP_DIRECT` enum. The
   content source changes, but the strategy routing in `finalize.py`
   remains `LEDGER_DIRECT` vs `SYNTHESIZE`.

---

## 5. Design

### 5.1 Config: remove `always_synthesize`

The `final_response` config field
(`AgenticFinalResponseMode = Literal["auto", "always_synthesize"]`) is
removed. `LEDGER_DIRECT` is always the first candidate in
`determine_completion_strategy`; `SYNTHESIZE` is the fallback.

**Migration**: existing configs with `final_response: always_synthesize`
are silently ignored (Pydantic `extra=ignore`). The
`normalize_agentic_final_response_mode` function and the
`_normalize_final_response` validator are removed.

**Invariant**: `determine_completion_strategy` MUST NOT unconditionally
return `synthesize`. It MUST attempt `LEDGER_DIRECT` eligibility first,
falling back to `SYNTHESIZE` only when structural gates or content checks
fail.

### 5.2 DAG-topology gate for `LEDGER_DIRECT`

The `plan_wave_count <= 1` check in `_ledger_direct_eligible` is replaced
by a DAG-topology check derived from `StepDAG.nodes`.

A helper `_is_linear_chain(dag, max_depth: int) -> bool` returns `True`
when all of the following hold:

1. **Single root**: exactly one node with `parent_step_id is None`.
2. **No parallel branches**: no node is a dependency of more than one
   other node (forward fanout <= 1).
3. **Chain depth <= `max_depth`**: the longest dependency chain length
   does not exceed `max_depth` (default 3).
4. **No decomposition children**: no node has `parent_step_id` pointing to
   another action node (decomposition produces non-linear sub-chains).

The existing gates are retained: `failed_steps == 0`,
`total_steps <= simple_ledger_direct_max_steps`,
`last_wave_tool_call_count <= ledger_direct_max_tool_calls`.

### 5.3 Enhanced typesafe eval decision

The typesafe eval decision (`decide_eval_coverage_typesafe`) receives
additional structured fields in `ClassifierRequest.state`:

| Field | Source | Type | Meaning |
|-------|--------|------|---------|
| `close_report_requires_eval` | `StepCloseReport.requires_eval` for all completed steps | `bool` | `False` for all -> coverage likely complete |
| `step_kind` | `StepNode.kind` of the last completed step | `str` | `eval` -> no need for another eval; `brainstorm`/`discuss` -> `ask_user` gate = completion |
| `ask_user_interrupt` | Detect `ask_user` / `AskUserQuestion` tool call in the last step's `execution.output_messages` | `bool` | BRAINSTORM-mode completion signal |
| `artifact_produced` | Infer from outcome text (file-path patterns) or `close_report.recommendations` | `bool` | Deliverable produced -> goal likely achieved |

The `_CRITERIA` dict is updated to reference these signals:

* **`no_eval`**: "The close reports show `requires_eval=False`, the step
  produced an artifact, and (for brainstorm/discuss steps) the step ended
  with an `ask_user` gate — all indicating the goal is fully achieved."
* **`eval`**: "Coverage is uncertain: `requires_eval=True`, no artifact
  produced, or the step ended without a completion signal."

**Artifact detection (initial heuristic)**: `artifact_produced` is
inferred by searching the step's `execution.outcome` text for file-path
patterns (`.platonic/drafts/`, `.platonic/specs/`, `.py`, `.md`, etc.).
A future schema change may add `StepCloseReport.artifacts: list[str]` for
reliable population by CoreAgent.

### 5.4 LEDGER_DIRECT content source

The content for `LEDGER_DIRECT` passthrough is sourced from the final
completed step's execution output, not `last_ledger_ai_content`:

1. Find the last completed step in the DAG (by `plan_iteration` desc,
   then `id` desc).
2. Extract AI text from `step.execution.output_messages` (concatenate AI
   message `content`).
3. If empty, fall back to `last_ledger_ai_content(state)`.

A new helper `last_step_output_content(state, goal_steps) -> str` provides
this logic.

### 5.5 Typesafe report validation gate

Before committing to `LEDGER_DIRECT` passthrough, a typesafe classifier
checks: "Is the final step output a valid goal report?"

**New module**: `sloop/engine/completion/report_validation_typesafe.py`

* **Question**: `report_valid`
* **Choices**:
  * `valid`: "The output is substantive (not a tool log or one-liner),
    addresses the user goal directly, and is coherent enough to serve as
    the final goal report without further synthesis."
  * `invalid`: "The output is a tool call log, a one-line status, a
    clarifying question without substantive content, or does not address
    the user goal."
* **Asymmetry**: `valid` (suppress synthesis) requires
  `suppress_min_confidence`; `invalid` (synthesize) just needs a trusted
  verdict.
* **Fallback**: unavailable / untrusted -> `SYNTHESIZE` (fail-safe).

### 5.6 Integrated fallback chain

```
determine_completion_strategy
  |
  +-- _dag_requires_synthesis (failed steps, deep chain, subagent cap)
  |     True -> SYNTHESIZE
  |
  +-- empty ledger -> SYNTHESIZE
  |
  +-- _ledger_direct_eligible (DAG topology: _is_linear_chain, chain_depth <= 3)
  |     False -> SYNTHESIZE
  |     True -> LEDGER_DIRECT candidate
  |          |
  |          +-- last_step_output_content (step execution output)
  |          |     empty -> fall back to last_ledger_ai_content
  |          |       empty -> SYNTHESIZE
  |          |
  |          +-- typesafe report validation
  |                valid -> LEDGER_DIRECT passthrough
  |                invalid -> SYNTHESIZE
  |                unavailable/untrusted -> SYNTHESIZE (fail-safe)
  |
  +-- default -> SYNTHESIZE
```

### 5.7 Interaction with ROOT_EVAL

The enhanced typesafe eval decision (Section 5.3) runs at `ROOT_EVAL`
time, before any coverage Eval step is inserted. When the typesafe path
returns `no_eval` (trusted + above suppression bar), `ROOT_EVAL` routes
to `finalize` without inserting an Eval step.

This is the existing flow in `RootEvalNode.process` for `SIMPLE` intake
labels — the change is that the typesafe path now has structured signals
that enable it to correctly classify BRAINSTORM-mode completion.

---

## 6. Examples

### 6.1 Loop 46a7 (retrospective)

* PLN-01 completes with design draft + `ask_user` gate.
* `ROOT_EVAL`: enhanced typesafe sees `close_report_requires_eval=False`,
  `step_kind=brainstorm`, `ask_user_interrupt=True`,
  `artifact_produced=True` -> `no_eval` -> finalize.
  **Saves 60s + 6 tool calls.**
* `finalize`: DAG topology = single linear step -> `LEDGER_DIRECT`
  eligible.
* `LEDGER_DIRECT` content: PLN-01 execution output (design draft) ->
  typesafe report validation: `valid` -> passthrough.
  **Saves 21s synthesis.**

### 6.2 Linear chain with weak output (passthrough rejected)

* Step 1 completes with a one-line status ("Done, created the file.").
* `ROOT_EVAL`: typesafe sees `artifact_produced=True` -> `no_eval` ->
  finalize.
* `finalize`: DAG topology = single linear step -> `LEDGER_DIRECT`
  eligible.
* `LEDGER_DIRECT` content: one-line status -> typesafe report validation:
  `invalid` -> SYNTHESIZE.
  **Synthesis runs; user gets a proper report.**

### 6.3 Parallel multi-step chain (LEDGER_DIRECT not eligible)

* Steps A, B run in parallel; both complete.
* `ROOT_EVAL`: typesafe may say `no_eval` -> finalize.
* `finalize`: `_is_linear_chain` returns `False` (forward fanout > 1) ->
  SYNTHESIZE.
  **Synthesis runs; user gets an aggregated report.**

---

## 7. Relationship to Other RFCs

* **[RFC-SLP-003](RFC-SLP-003-sloop-eval-thread.md)**: This RFC enhances
  the eval decision that gates whether an Eval step (RFC-SLP-003) is
  inserted. The Eval thread's readonly tool set and `decompose_task`
  semantics are unchanged.
* **[RFC-LOOP-009](../loop/RFC-LOOP-009-goal-completion-module.md)**: This
  RFC modifies the goal completion strategy selection
  (`determine_completion_strategy`) and the `LEDGER_DIRECT` content
  source. The synthesis fallback path is unchanged.
* **RFC-SLP-002**: `_is_linear_chain` reads the `StepDAG` structure
  defined by RFC-SLP-002's recursive decomposition.
* **RFC-LOOP-025**: The intake label (`SIMPLE` / `MINIMAL` / `COMPLEX`)
  from RFC-LOOP-025 drives which eval decision path runs at `ROOT_EVAL`.

---

## 8. Open Questions

1. **Artifact detection reliability**: the heuristic (infer from outcome
  text) may produce false negatives for steps that produce artifacts
  without mentioning file paths in their outcome text. Should we add
  `StepCloseReport.artifacts: list[str]` as a schema change in this RFC,
  or defer to a follow-up?

2. **`ask_user_interrupt` detection**: should we inspect the step's
  `execution.output_messages` for `ask_user` tool calls (heuristic), or
  should the executor set a structured flag on `StepCloseReport` when it
  detects an `ask_user` interrupt (reliable, requires executor changes)?

3. **Config migration**: should existing configs with
  `final_response: always_synthesize` emit a deprecation warning at load
  time, or be silently ignored?

---

## 9. Conclusion

The short-chain fast-finalize path eliminates three layers of waste
(unnecessary eval, unnecessary synthesis, unnecessary re-summarization)
for goals where the first step's output is already the deliverable. By
combining structural DAG-topology gates with typesafe content validation,
the design achieves fast finalization without sacrificing safety: every
typesafe gate has a fail-safe fallback to the existing LLM or synthesis
path.

> When a short linear chain produces a substantive deliverable, the step
> output IS the goal report — no eval, no synthesis, no re-summary.
