# RFC-SLP-006: Intelligent Decompose Decision — Evidence-Staged Finish-vs-Split

**Status**: Draft
**Kind**: Architecture Design
**Created**: 2026-11-11
**Authors**: Soothe Team
**Updated**: 2026-11-11
**Depends on**: RFC-SLP-002, RFC-SLP-003, RFC-SLP-005
**Related**: IG-SLP-006 (trust + guide), IG-SLP-010 (in-loop direct response)
**Revises**: RFC-SLP-002 §decompose_task injection (t=0-only finish-vs-split), §parallel nudge gating, §StepNode schema additions (depth-guard semantics — audit tiers do not count)
**Design draft**: `.platonic/drafts/2026-11-11-intelligent-decompose-decision-design.md`

---

## Abstract

The finish-vs-split decision becomes **evidence-staged**: instead of a single
classifier verdict frozen at thread start, the decision is re-opened when
execution evidence exists, fed by cost signals, guarded against structural
losses at commit, and measured after the fact.

1. **Root split nudge** — a trusted root "split" verdict injects the parallel
   nudge addendum (the organic path the intake-classification deletion left
   dead).
2. **Evidence re-check** — finish-vs-split re-runs at tool-call milestones
   and context pressure, with the tool trajectory as state; a trusted "split"
   can re-inject a stripped `decompose_task`.
3. **Decompose quality telemetry** — a per-proposal `DecomposeQuality` ring
   on the goal record, computed at action-tree green. Signal-only.
4. **Shape guards** — single-child proposals on action parents are refused
   pre-queue; pure-sequential child chains are flagged at reconcile.
5. **Context-pressure hint** — a coarse token estimate injects a split hint
   below the compaction threshold.
6. **Eval-continuation headroom** — `kind=eval` audit tiers no longer
   consume lineage depth, so coverage-continuation children get the same
   decompose headroom as root children.

Do-or-decompose, trust + guide, and the asymmetric fail-safe are unchanged.

---

## Scope and Non-Goals

### Scope

* Finish-vs-split verdict lifecycle (cache, re-check, un-strip).
* Prompt addendum injection conditions on step threads.
* `DecomposeQuality` schema, computation, and ring storage.
* Tool-level and reconcile-level shape guards.
* `DecomposeLoopConfig` extensions.

### Non-Goals

* Reviving intake classification / pass2 (RFC-SLP-002 deleted it; P1 uses
  the runtime verdict instead).
* LLM gates inside `decompose_task` (IG-SLP-006 posture stands).
* Any consumer of quality telemetry in v1 — it is a tuning dataset, not an
  input to a decision.
* Eval-thread policy changes (RFC-SLP-003 stands; eval parents are exempt
  from the single-child guard).
* Realized-concurrency measurement (needs step wall-clock timestamps; the
  v1 schema is DAG-shape-only).

---

## Background

The finish-vs-split classifier runs once per step, at the first model call,
on `step_description` + `goal` (+ optional client-forced complexity). Its
"finish" verdict strips `decompose_task` (bar: `suppress_min_confidence`
0.9); its "split" verdict is discarded by the middleware. Since intake
classification deletion, `task_complexity == "complex"` on the root occurs
only when the client forces `intake_scope`, so the parallel nudge never
fires for organic goals — the finish-by-default THREAD_POLICY bias is
unopposed. Nothing measures whether committed decompositions were worth their
wave + barrier + per-child context warmup.

---

## Architecture

```
step thread start ──> t=0 finish-vs-split ──> finish≥0.9: strip tool
        │                                     split (trusted): root? -> nudge addendum
        │
in-thread ──> every recheck_tool_calls completions ─┐
        │        + context-pressure threshold ──────┴─> evidence re-check (≤ max_rechecks)
        │                                                split (trusted): un-strip + addendum
        │                                                finish: strip stays (sticky)
        │
decompose_task call ──> single-child (action parent): refuse, guide in-thread
        │
RECONCILE ──> commit; pure-chain children flagged sequential_only
        │
action tree green ──> DecomposeQuality ring append (signal-only)
```

### Stage table

| Stage | When | Mechanism | Direction |
|---|---|---|---|
| 1. Thread start | first model call | verdict: strip on finish ≥0.9; nudge on trusted root split | suppress / advise |
| 2. In-thread | tool milestones, token pressure | re-check with trajectory; un-strip on trusted split; pressure hint | enable / advise |
| 3. Commit | tool call, reconcile | single-child refusal; chain flag | structural |
| 4. Tree-green | ROOT_EVAL entry | quality telemetry | observe |

---

## 1. Root Split Nudge

`decide_finish_vs_split_typesafe` already returns "split" on trusted
verdicts; the middleware MUST cache it (same per-step contextvar pattern)
and `modify_request` MUST append `PARALLEL_NUDGE_ADDENDUM` when **all** of:

* agent mode (not plan/ask, not eval-step config),
* the step is a DAG root (`SOOTHE_IS_DAG_ROOT_KEY`),
* the cached verdict is "split".

The nudge is advisory, so the bar is `verdict_trusted` (conf ≥ 0.6 +
decisive margin) — the fail-safe direction. The client-forced
`task_complexity == "complex"` nudge path is unchanged (operator intent).
Gate: `decompose.root_split_nudge` (default true).

## 2. Evidence Re-check

The middleware counts tool completions per step (`awrap_tool_call`). At
every `recheck_tool_calls`-th completion (default 10; 0 disables), and at
the context-pressure threshold, it invalidates the per-step verdict cache;
the next `awrap_model_call` re-runs the classifier with the enriched state:
`tool_call_count` + the last N tool names with one-line result digests
(capped; N = 8).

* Trusted **split** → if the tool was stripped, re-inject it and append the
  split-candidate addendum once. Work-enabling = fail-safe direction →
  `verdict_trusted` bar.
* Trusted **finish** ≥ suppress bar → the strip stays (sticky).
* Unavailable / untrusted → current tool surface unchanged.

MUST NOT fire more than `max_rechecks_per_step` (default 2) times per step.
Agent mode + action steps only. Re-evaluation fires only on changed state
(new tool results between calls) — identical state never re-evaluates (the
7d10 churn lesson, RFC-SLP-005 §Background).

## 3. Decompose Quality Telemetry

`DecomposeQuality` per committed proposal, computed at action-tree green
(ROOT_EVAL entry) from the StepDAG + StepExecution records only:

| Field | Type | Description |
|---|---|---|
| parent_step_id | str | Decomposed parent |
| child_count | int | Committed children |
| single_child | bool | child_count == 1 |
| sequential_only | bool | Children form a pure dependency chain |
| independent_children | int | Children with no sibling dependencies |
| deepest_chain_len | int | Longest sibling dependency chain |
| child_failures | int | Children that ended failed |
| child_retries | int | Sum of child retry_count |
| child_tool_calls_sum | int | Sum of child execution tool_call_count |
| child_duration_ms_sum | int | Sum of child execution duration_ms |

Stored as a bounded ring on `GoalNode` (`decompose_quality`,
`quality_ring_size`, default 20 — the `coverage_signals` pattern),
persisted via the CE store, emitted as a `decompose_quality` event.
**Signal-only**: no decision reads it in v1.

## 4. Shape Guards

**Single-child refusal (tool-level).** `decompose_task` with exactly one
subtask on an **action**-kind parent MUST refuse with a guidance message
(finish in this thread; use `write_todos`). Saves the wave, the reconcile
barrier, and the child context warmup. **Eval parents are exempt** — a
single remaining subtask from a coverage verdict is continuation semantics
(RFC-SLP-003). The parent step kind MUST be bound into the decompose runtime
(the executor owns it at thread spawn).

**Sequential-chain flag (reconcile-level, advisory).** When one proposal's
children form a pure chain (each depends on the previous, no other sibling
edges), the commit is unchanged but the quality record carries
`sequential_only`. Rejection is a v2 decision gated on telemetry — a
reconcile-time rejection cannot steer the already-ended proposing thread.

## 5. Context-Pressure Split Hint

`modify_request` estimates the thread's tokens coarsely (message text bytes
÷ 4) and compares against `agent.loop.context_window_limit`. At
`split_hint_threshold_pct` (default 0.60 — below the 0.80 compaction
threshold, so the hint precedes evidence loss) it appends a one-time
context-pressure hint addendum: remaining independent work is cheaper as
child threads with fresh contexts than as a compacted thread. Hint-only, so
coarse precision is acceptable; when a mid-step token emitter exists (the
deferred PCE open question), this estimate and PCE token milestones share
it. Idempotent per step.

## 6. Eval-Continuation Decompose Headroom

The depth guard (`lineage_depth(parent) >= max_depth` → reject) counts the
eval tier: root(1) → eval(2) → continuation children(3), so under default
`max_depth=3` the children of a `complete=false` verdict are **leaves** —
they cannot decompose, even when the verdict's remaining work is itself
parallelizable. The eval node is a coverage-audit checkpoint injected by
ROOT_EVAL, not a work decomposition level chosen by the executor; it SHOULD
NOT consume depth.

`StepDAG.lineage_depth` is replaced by `action_lineage_depth`: the
parent-chain walk skips `kind="eval"` nodes. Effect:

* root(1) → eval(skip) → continuation child(2) → **can decompose once** →
  grandchild(3) blocked — identical headroom to root's direct children.
* Each eval round's continuation tier resets to root-tier depth, mirroring
  the `plan_iteration` semantics (RFC-SLP-002: eval-spawned waves are a
  recognized tier).

Runaway safety does not depend on lineage depth: `max_steps` (30),
`max_waves` (10), `max_eval_rounds` (10), and the branch caps bound the
tree independently. Rename in place — `lineage_depth` has exactly one
production caller (the reconcile depth guard); the old surface is deleted,
not aliased. `max_depth` config description updated to "action-lineage
decomposition depth (eval audit tiers do not count)". The waves-exhausted
eval-only claim filter is unchanged by design: deeper recursion consumes
waves, and the anti-spin guard keeps its authority.

---

## Invariants

| Invariant | Meaning | Consequence of Violation |
|---|---|---|
| Asymmetric fail-safe | Strip/suppress needs ≥0.9 confidence; keep/nudge/un-strip needs trusted only | Wrong suppression serializes parallel work |
| Evidence-staged, never churned | Re-evaluation fires only on changed state, bounded 2/step | 7d10-class classifier churn (295 identical calls) |
| Do-or-decompose | No intake pre-classification of organic goals | pass2 revival |
| Trust + guide | No LLM gates inside the tool; guards are structural | d15f-apparatus regression (IG-SLP-006) |
| No keyword heuristics | New signals are verdicts / counts / token pressure / DAG shape | Rule 9 violation |
| Eval continuation semantics | Eval parents exempt from shape guards | Coverage continuation refused |
| Read-only modes | Stages 1/2/5 never fire in plan/ask; tool already stripped there | Rule: Eval-adjacent behavior gated on interaction mode |
| Signal-only telemetry | Quality records never gate execution in v1 | Observability becomes a controller |
| Audit tiers are depth-free | Eval nodes never count toward `max_depth` | Continuation children are forced leaves; verdict remaining-work cannot fan out |

---

## Config

```yaml
agent:
  loop:
    decompose:
      max_depth: 3
      max_steps: 30
      max_waves: 10
      max_branch_root: 5
      max_branch_inner: 3
      root_split_nudge: true        # P1 (0 disables)
      recheck_tool_calls: 10        # P2 (0 disables)
      max_rechecks_per_step: 2      # P2
      split_hint_threshold_pct: 0.60 # P5 (0 disables)
      quality_ring_size: 20         # P3
```

---

## Observability

Events: `decompose_split_nudge`, `decompose_recheck` (trigger, verdict,
re-injected?), `decompose_single_child_refused`, `decompose_quality`.
Log lines tagged `[decompose]` (existing convention).

---

## Testing Requirements

* Root trusted split → nudge appended once; untrusted → no nudge; non-root →
  no nudge; plan/ask → never.
* Re-check: milestone invalidation fires exactly one classifier call with
  trajectory state; un-strip on trusted split; sticky strip on trusted
  finish; bounded at `max_rechecks_per_step`; identical state (no new tool
  results) never re-evaluates.
* Pressure hint fires at 0.60 of the limit, once per step, not in plan/ask.
* Single-child refusal on action parents; eval parents unaffected.
* Chain flag: pure-chain proposal commits with `sequential_only`; branched
  proposals do not flag.
* Quality record fields at tree-green; ring bound; persistence roundtrip via
  the CE store.
* Classifier unavailable at any stage → fail-safe tool surface unchanged.
* Eval-continuation child CAN decompose under default `max_depth=3`
  (root → eval → child commits; child's proposal accepted); grandchild
  blocked at depth 3; pure action chains keep the old semantics; multi-round
  eval (eval → child → eval → child) still bounded by `max_steps`.

---

## Implementation Placement

Follow-on IG-SLP-012. Package `soothe` (`sloop/decompose`,
`sloop/stations/decompose`, `context`, `prompts`, `config`). Do not
reverse the monorepo DAG.

---

## Open Questions (impl-time)

1. Re-check trajectory digest cap (chars) and N (tool names).
2. Whether re-checks fire on child steps or roots only (v1: all action
  steps, budget-capped).
3. Chain-guard rejection criteria once telemetry accumulates.
4. Switching the P5 estimate to `usage_metadata` when an emitter exists.