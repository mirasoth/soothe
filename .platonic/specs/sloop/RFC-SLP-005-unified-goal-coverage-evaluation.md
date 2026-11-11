# RFC-SLP-005: Unified Goal Coverage Evaluation — PCE and Reactive Eval

**Status**: Draft
**Kind**: Architecture Design
**Created**: 2026-11-10
**Authors**: Soothe Team
**Updated**: 2026-11-10
**Depends on**: RFC-SLP-002, RFC-SLP-003, RFC-LOOP-007, RFC-LOOP-009
**Related**: RFC-SLP-004, RFC-LOOP-005, RFC-LOOP-006
**Revises**: RFC-SLP-003 §When Eval Runs (eval-required predicate), §Eval Thread Envelope

---

## Abstract

Goal coverage evaluation becomes **two engines over one signal bus**:

1. **PCE (Proactive Coverage Evaluation)** — a per-loop parallel monitor
   that observes the **goal DAG** (completed, ongoing, interrupted goals
   plus steps) while goals execute, and appends **CoverageSignal**
   records onto goal records. Typesafe-first, fingerprint-cached, LLM
   escalation. **Signal-only**: PCE never pauses, redirects, or mutates
   execution.
2. **Reactive Eval (RFC-SLP-003, kept)** — the ROOT_EVAL gate and
   engine-injected `kind=eval` StepNode, trigger logic unchanged,
   extended to **consume** PCE signals.

The Context Engine goal DAG remains the single Source of Truth. This RFC
closes the single-leaf coverage gap observed in loop 7d10, where two
goals executed as one large leaf each (92 and 327 tool calls) and
finalized with zero coverage audit.

---

## Scope and Non-Goals

### Scope

* PCE monitor: lifecycle, triggers, evaluation pass, cost caps.
* CoverageSignal schema, persistence, and consumption semantics.
* The fourth eval-required condition in the ROOT_EVAL gate.
* Envelope and finalize enrichment from signals.

### Non-Goals

* PCE-initiated execution changes (interrupts, step injection) — signal-only by design.
* Replacing RFC-LOOP-009 finalize / synthesis.
* Changes to LoopRail `evaluate_goal_completion`.
* Populating `routing_classification` or fixing the finish-vs-split classifier cache — separate follow-ups; PCE MUST NOT depend on them.

---

## Background & Motivation

Loop 7d10 diagnosis (`01a1c34e-4f0d-7b60-9c88-74263ba97d10`):

1. **Single-leaf goals never get an audit.** `eval_required()` triggers
   only on decomposition, >1 completed action leaf, or an early-exit
   `StepCloseReport`. Both goals ran as one leaf with zero
   `decompose_task` calls; ROOT_EVAL skipped Eval by predicate; the
   COMPLEX force-eval branch was unreachable because
   `routing_classification` is never populated in auto mode. Completion
   was never independently verified.
2. **No coverage signal during execution.** Reactive Eval fires only at
   action-tree green. Nothing observes a 92-minute single-leaf step
   mid-flight.
3. **Classifier churn lesson.** The typesafe finish-vs-split classifier
   ran 295 times on byte-identical degenerate input (conf ~0.5, all
   untrusted) because its `ContextVar` cache never hits across hops.
   Any proactive mechanism MUST NOT repeat this pattern: identical DAG
   state never re-evaluates.

---

## Architecture Overview

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

**Components**

| Component | Responsibility |
|---|---|
| PceEventQueue | Bounded async queue of step-boundary and budget-milestone events |
| PCE monitor | Per-loop asyncio task; snapshots the goal DAG, runs verdicts, writes signals |
| CoverageSignal | Persisted record on the goal record; written by PCE and (optionally) eval close |
| ROOT_EVAL gate | Existing gate plus the fourth signal-based eval-required condition |
| Eval envelope builder | Includes unconsumed signals as labeled untrusted evidence |

**Placement**: the monitor is spawned at graph invoke alongside
`pump_task` (`loop-pce:{loop_id}`) and cancelled at invoke complete.
No idle-period daemon, no orphan tasks. It never spawns in plan/ask
interaction modes (same `_READONLY_MODES` gate as ROOT_EVAL).

---

## PCE Monitor

### Triggers

Debounced by `min_pass_interval_s` (default 30s); pending events
coalesce.

* **Step boundaries**: step started / completed / failed, decompose
  committed, close_report filed.
* **Budget milestones per active step**: every `tool_call_interval`
  tool calls (default 25) and `elapsed_milestone_s` (default 600s).
  A token-count milestone is deferred until a mid-step token emitter
  exists (hop `usage_metadata`).

### Evaluation pass

1. **Snapshot** the full goal DAG: all goals (status, description) and
   steps (id, kind, status, description, tool count, tokens, elapsed,
   close_report), plus a compact ledger digest.
2. **Fingerprint** the snapshot; unchanged since the last pass → skip.
3. **Typesafe verdict** over the snapshot: one of `on_track`,
   `coverage_gap`, `goal_likely_complete`, `runaway_step`,
   `stalled`, `scope_drift`, plus confidence and probabilities.
   Drift-guard untrusted (<0.6 confidence or near-tie margin) → escalate.
4. **LLM escalation** (fast model, compact projection, same labels) on:
   untrusted verdict, conflicting consecutive verdicts, or step-boundary
   milestones. Bounded per goal.
5. **Append** a CoverageSignal to the active goal record (bounded ring).

### Cost caps and fail-safety

* Max passes per goal: 20. Max LLM escalations per goal: 5.
* Any PCE error (classifier unavailable, LLM failure, task crash) → log
  and skip the pass. PCE can never block the loop; signals written so
  far are retained.

---

## Abstract Schemas

### CoverageSignal

| Field | Type | Description |
|-------|------|-------------|
| signal_id | str | Unique id |
| goal_id | str | Goal the signal attaches to |
| source | enum | `pce` \| `eval_close` |
| kind | enum | `on_track` \| `coverage_gap` \| `goal_likely_complete` \| `runaway_step` \| `stalled` \| `scope_drift` |
| confidence | float | Verdict confidence |
| evidence | object | `{dag_fingerprint, step_id, milestone}` |
| created_at | datetime | Write time |
| consumed_by | enum | `null` \| `root_eval` \| `finalize` |

Signals form a bounded ring per goal (`signal_ring_size`, default 20).
`eval_close` sources let the reactive Eval publish close verdicts onto
the same bus so consumers see one uniform signal history.

---

## Unification — Signal Consumers

1. **Fourth eval-required condition.** Any unconsumed signal of kind
   `coverage_gap`, `runaway_step`, `stalled`, or `scope_drift`,
   or `goal_likely_complete` with confidence < 0.8 → Eval required
   **even for single-leaf goals**. Closes the 7d10 gap. The existing
   structural predicate (decomposed node, multi-leaf, early-exit) is
   unchanged.
2. **Eval envelope.** Latest unconsumed signals are listed as labeled
   untrusted evidence, same treatment as deferred items in RFC-SLP-003.
3. **finalize / report_validation.** When Eval was skipped but signals
   exist, the latest signal joins the validation context — goal
   completion is never fully unverified.

Consumers mark `consumed_by` when acting on a signal.

---

## Invariants and Constraints

| Invariant | Meaning | Consequence of Violation |
|-----------|---------|------------------------|
| Signal-only authority | PCE never mutates goals/steps or interrupts execution | Design collapse into an execution controller; retracted |
| Goal DAG is SoT | Signals are records on goal records, persisted via the existing checkpoint flush | Divergent state between PCE view and loop state |
| No re-evaluation of identical state | Fingerprint gate before every verdict | Classifier churn (295-call pattern from 7d10) |
| PCE cannot block the loop | All PCE failures are log-and-skip | Loop stalls on observability infrastructure |
| Read-only interaction modes | No monitor in plan/ask | Coverage audit activity in read-only modes |

---

## Config

```yaml
agent:
  loop:
    pce:
      enabled: true
      tool_call_interval: 25
      elapsed_milestone_s: 600
      min_pass_interval_s: 30
      max_passes_per_goal: 20
      max_llm_escalations_per_goal: 5
      signal_ring_size: 20
```

Readonly gate on interaction mode is builtin, not YAML. No `enabled`
flag path that silently skips consumption of already-written signals.

---

## Observability

Events: `loop.pce_started`, `pce_pass_skipped` (fingerprint reason),
`pce_verdict`, `pce_escalation`, `pce_signal_written`. Log lines
tagged `[pce]`. Signals rehydrate from the SQLite checkpoint on loop
restart (reentrant loop state).

---

## Testing Requirements

* Fingerprint cache: identical DAG state → no second classifier call.
* Milestone trigger fires at the configured tool-call count.
* Single-leaf runaway step → `runaway_step` signal →
  `eval_required()` True at action-tree green.
* Plan/ask interaction modes → monitor never spawned.
* Signal ring bounded to `signal_ring_size`.
* Signal persistence roundtrip across checkpoint reload.
* ROOT_EVAL consumes signals and marks `consumed_by`.
* PCE error injection (classifier unavailable, LLM failure, task crash)
  leaves the loop unaffected.

Do not weaken tests to treat PCE verdicts as execution decisions
(signal-only).

---

## Implementation Placement

Follow-on IG (not this RFC). Package: `soothe` (`sloop`,
`context`, `prompts`). Do not reverse the monorepo DAG.

---

## Open Questions (impl-time)

1. Compact ledger-digest size cap for the snapshot.
2. Whether `eval_close` signal backfill lands in the same change or a follow-up.
3. Typesafe question schema for the six signal kinds (one multi-label question vs per-kind binary questions).
4. Whether the 0.8 confidence bar for `goal_likely_complete` should be config-exposed.

---

## Related Documents

- [RFC-SLP-003](RFC-SLP-003-sloop-eval-thread.md) — reactive Eval thread (revised here)
- [RFC-SLP-002](RFC-SLP-002-sloop-recursive-decomposition.md) — decomposition topology
- [RFC-LOOP-009](../loop/RFC-LOOP-009-goal-completion-module.md) — finalize / goal completion
- [RFC-LOOP-007](../loop/RFC-LOOP-007-goal-context-management.md) — goal context
- [Design draft](../../drafts/2026-11-10-unified-goal-coverage-evaluation-design.md)
- [RFC Index](rfc-index.md)
