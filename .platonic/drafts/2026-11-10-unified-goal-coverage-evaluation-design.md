# Unified Goal Coverage Evaluation — PCE + Reactive Eval

**Status**: Draft
**Date**: 2026-11-10
**Origin**: Loop 7d10 diagnosis (`01a1c34e-4f0d-7b60-9c88-74263ba97d10`)

## Problem

Loop 7d10 exposed two structural gaps in goal coverage evaluation:

1. **Single-leaf goals never get a coverage audit.** `eval_required()`
   (context/models.py) triggers only on decomposition, >1 completed action
   leaf, or an early-exit `StepCloseReport`. Both 7d10 goals ran as one
   big leaf (92 tools / 13.5 min; 327 tools / 92.5 min) with zero
   `decompose_task` calls — so ROOT_EVAL skipped Eval by predicate, and
   completion was never independently verified. The COMPLEX force-eval
   branch was unreachable because `routing_classification` is never
   populated in auto mode.

2. **No coverage signal exists while a goal is executing.** The reactive
   Eval fires only at action-tree green. During a 92-minute single-leaf
   step, nothing observes whether the goal is on track, already achieved,
   or runaway — the loop discovers this only after the step ends.

Related churn observed in the same loop: the typesafe finish-vs-split
classifier ran 295 times on byte-identical degenerate input
(conf ~0.5, all untrusted) because the per-step verdict cache
(`ContextVar`) never hits across hops. Any new proactive mechanism
MUST NOT repeat this pattern.

## Design Decisions (confirmed with user)

| Decision | Choice |
|---|---|
| PCE authority | **Signal-only** — emits signals onto the goal DAG; never pauses/redirects execution |
| Trigger model | **Step boundaries + budget milestones** (tool count / tokens / elapsed on active steps) |
| Evaluation engine | **Typesafe-first, fingerprint-cached, LLM escalation** on untrusted/ambiguous/boundary |
| Unification shape | **Signal bus (loose)** — existing reactive Eval trigger kept, becomes a signal consumer |
| Monitor placement | **Per-loop task** spawned with each graph invoke; sees full goal DAG (completed, ongoing, interrupted) |

## Architecture

Two engines, one signal store, one consumer gate:

```
executor metrics ─┐                                  ┌─> ROOT_EVAL gate
step-boundary     ├─> PceEventQueue ─> PCE monitor ──┤    (structural predicate +
events (stations)─┘   (per-loop task)   signal-only  │     PCE signals = eval_required)
                                         │           ├─> Eval envelope (signals as evidence)
                                         ▼           └─> finalize / report_validation
                              CoverageSignal records
                              (on goal DAG, persisted via checkpoint flush)
                                         ▲
                              eval close reports (existing Eval engine, unchanged trigger)
```

The **goal DAG in the Context Engine remains the single Source of Truth**.
PCE only appends signal records onto it; it never mutates goals or steps.
The existing reactive Eval (ROOT_EVAL gate + engine-injected `kind=eval`
StepNode + LLM eval-decision, RFC-SLP-003) keeps its trigger logic and
becomes a consumer of the same signal store.

## PCE Monitor

**Lifecycle**

- Spawned at graph invoke alongside `pump_task` (`loop-pce:{loop_id}`),
  cancelled at invoke complete. No orphan tasks; no idle-period daemon.
- Never spawned when `interaction_mode` is plan/ask (same
  `_READONLY_MODES` gate as root_eval).

**Triggers** (debounced by `min_pass_interval_s`, default 30s)

- Step boundaries: step started / completed / failed, decompose committed,
  close_report filed.
- Budget milestones per active step: every 25 tool calls (default), 50k
  input tokens, 10 minutes elapsed.

**Evaluation pass**

1. Snapshot the full goal DAG — all goals (completed, ongoing,
   interrupted): status, description; steps: id, kind, status, tool
   count, tokens, elapsed, close_report; plus a compact ledger digest.
2. Fingerprint the snapshot; unchanged since the last pass → skip
   (identical state never re-evaluates — the 295-churn lesson).
3. Typesafe verdict: labels below + confidence + probabilities.
   Drift-guard untrusted (<0.6 confidence or near-tie margin) → escalate.
4. LLM escalation (fast model) on: untrusted verdict, conflicting
   consecutive verdicts, or step-boundary milestones. Bounded per goal.
5. Append a `CoverageSignal` to the active goal record (bounded ring).

**Cost caps**: max 20 passes and max 5 LLM escalations per goal. Any PCE
error → log + skip the pass; PCE can never block the loop.

## CoverageSignal

```yaml
CoverageSignal:
  signal_id: str
  goal_id: str
  source: pce | eval_close
  kind: on_track | coverage_gap | goal_likely_complete | runaway_step | stalled | scope_drift
  confidence: float
  evidence: {dag_fingerprint, step_id, milestone}
  created_at: datetime
  consumed_by: null | root_eval | finalize
```

Signal kinds are semantic and stable; `eval_close` lets the reactive
Eval engine publish its close verdicts onto the same bus so downstream
consumers see one uniform signal history.

## Unification — Signal Consumers

1. **`eval_required()` gains a fourth condition**: any unconsumed signal
   of kind `coverage_gap | runaway_step | stalled | scope_drift`, or
   `goal_likely_complete` with confidence < 0.8 → Eval required even for
   single-leaf goals. This closes the 7d10 gap.
2. **Eval envelope**: latest unconsumed signals are listed as labeled
   untrusted evidence (same treatment as deferred items in RFC-SLP-003).
3. **finalize / report_validation**: when Eval was skipped but signals
   exist, the latest signal joins the validation context — goal
   completion is never fully unverified.
4. **Existing LLM-driven trigger untouched**: decomposed-node,
   multi-leaf, and early-exit conditions behave exactly as today.

## Config

```yaml
agent:
  loop:
    pce:
      enabled: true
      tool_call_interval: 25
      token_milestone: 50000
      elapsed_milestone_s: 600
      min_pass_interval_s: 30
      max_passes_per_goal: 20
      max_llm_escalations_per_goal: 5
      signal_ring_size: 20
```

## Observability

Events: `loop.pce_started`, `pce_pass_skipped` (with fingerprint reason),
`pce_verdict`, `pce_escalation`, `pce_signal_written`. Log lines tagged
`[pce]`. Signals rehydrate from the SQLite checkpoint on loop restart
(reentrant loop state).

## Error Handling

- PCE failure (classifier unavailable, LLM error, task crash) never
  blocks the loop; signals written so far are retained.
- Monitor task cancellation at graph invoke complete is always clean.
- Loop restart → signals rehydrate with goal state from the checkpoint.

## Testing

- Fingerprint cache: identical DAG state → no second classifier call.
- Milestone trigger fires at the configured tool-call count.
- Single-leaf runaway step → `runaway_step` signal → `eval_required()`
  True at action-tree green.
- Plan/ask interaction modes → monitor never spawned.
- Signal ring bound to `signal_ring_size`.
- Signal persistence roundtrip across checkpoint reload.
- ROOT_EVAL consumes signals and marks `consumed_by`.
- PCE error injection leaves the loop unaffected.

## Non-Goals

- No PCE-initiated execution changes (no interrupts, no step injection) —
  signal-only by design.
- No replacement of RFC-LOOP-009 finalize / synthesis.
- No changes to LoopRail `evaluate_goal_completion`.
- No fix herein for `routing_classification` population or the
  finish-vs-split classifier cache (separate follow-ups; PCE must not
  depend on them).

## Open Items (impl-time)

1. Exact ledger-digest size cap for the snapshot.
2. Whether `eval_close` signals backfill from existing Eval outcomes in
   the same change or a follow-up.
3. Typesafe question schema for the six signal labels (one multi-label
   question vs per-kind binary questions).
4. Whether the 0.8 confidence bar for `goal_likely_complete` should be
   config-exposed.
