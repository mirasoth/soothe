# Intelligent Decompose Decision — Design Draft

**Date**: 2026-11-11
**Target**: RFC-SLP-006 / IG-SLP-012
**Scope**: finish-vs-split intelligence for StrangeLoop step threads
**Depends on**: RFC-SLP-002 (recursive decomposition), RFC-SLP-003 (Eval thread), RFC-SLP-005 (PCE signal discipline)

---

## Problem

The decompose-or-not decision is made once, at step-thread start, from the
step description and goal text alone — before any execution evidence exists.
Since intake classification was deleted (`0cd352e4b`, IG-SLP-010), no
organic goal ever receives a proactive split signal: root `task_complexity`
is `"simple"` unless the client forces `intake_scope`
(`sloop/utils/routing_scope.py`, `stations/decompose/dispatch.py`), so the
parallel nudge is dead for organic goals. Meanwhile nothing records whether
past decompositions were good or wasteful, so criteria and thresholds are
static guesses.

## Current state

Three layers decide "decompose or not":

| Layer | Mechanism | Where |
|---|---|---|
| Prompt | THREAD_POLICY finish-by-default; SPLIT only when ALL of (independent deliverables, evidence can't finish in-thread, saves wall-clock) | `prompts/fragments/decompose/thread_policy_system.xml` |
| Coded suppression | JEV `finish_vs_split` Choice classifier, once per step at first model call; strips `decompose_task` on "finish" ≥ `suppress_min_confidence` (0.9). "split" verdicts are **discarded** by the middleware | `sloop/decompose/decompose_typesafe.py`, `sloop/decompose/middleware.py` |
| Budget | Branch caps (5 root / 3 inner, truncate), max_depth=3, max_steps=30, max_waves=10 | `DecomposeLoopConfig`, `sloop/decompose/reconcile.py` |

The tool is pure validate-and-enqueue (IG-SLP-006, trust+guide). Westworld
phrase triggers remain as deliberate operator overrides.

## Gaps

- **G1 — t=0 freeze**: the classifier verdict is cached per step before the
  first tool call; the most predictive split signal (scope discovered
  mid-execution) cannot exist yet. A wrong suppression serializes work for
  the whole step.
- **G2 — one-directional suppression**: a stripped thread that later proves
  too big has no re-evaluation path; the circuit breaker steers in-thread but
  never revisits the split decision.
- **G3 — dead proactive path**: the parallel nudge fires only on
  client-forced complexity; organic goals get no split encouragement against
  the finish-by-default prompt bias.
- **G4 — no outcome feedback**: no telemetry links a decomposition to its
  realized quality (single child, pure-chain children, child failures).
- **G5 — structural losses pass silently**: a 1-child proposal is a pure loss
  (extra wave + reconcile barrier + child context warmup); an all-sequential
  sibling set gains nothing over in-thread `write_todos`.
- **G6 — unused cost signals**: context pressure (a thread near compaction
  makes fresh child threads objectively cheaper) never enters the decision.

## Proposals

### P1 — Root split-verdict nudge (revives the organic path)

The JEV already runs at root and already applies the trusted gate to "split"
verdicts; the middleware just throws the answer away. Change: cache the
"split" verdict; on agent-mode DAG roots inject `PARALLEL_NUDGE_ADDENDUM`
(same addendum the client-forced path uses). Advisory only — the fail-safe
direction (keep tool) is unchanged, so the bar stays `verdict_trusted`
(conf ≥ 0.6 + decisive margin).

*Alternatives considered*: (a) revive intake classification — rejected,
RFC-SLP-002 deliberately deleted pass2 and IG-SLP-010 deleted intake
classification; (b) a new root "goal shape" classifier question — rejected,
redundant with the existing finish-vs-split question.

### P2 — Post-evidence re-evaluation (fixes the t=0 freeze)

Re-ask finish-vs-split at evidence boundaries instead of only at thread
start: after every `recheck_tool_calls` tool completions (default 10) and
at the context-pressure threshold (shared with P5). The re-check feeds the
tool trajectory (recent tool names + one-line result digests, capped) into
the classifier state. A trusted "split" re-injects the tool if it was
stripped and appends a one-time split-candidate addendum; a trusted
"finish" keeps the strip (sticky). Bounded to `max_rechecks_per_step`
(default 2). Unavailable classifier → no re-check (fail-safe).

The trigger counter lives in the middleware `awrap_tool_call` (fires for
every tool call on a step thread); crossing a milestone invalidates the
per-step verdict cache so the next `awrap_model_call` re-evaluates. This
respects the 7d10 churn lesson: re-evaluation only ever fires on *changed*
state (new tool results), never on identical input.

### P3 — Decompose quality telemetry (closes the feedback loop)

A `DecomposeQuality` record per committed proposal, computed at
action-tree green from the StepDAG alone: parent, child_count, single_child,
sequential_only (children form a pure dependency chain), independent_children
(no sibling deps), child_failures, child_retries, child_tool_calls_sum,
child_duration_ms_sum, deepest_chain_len. Stored as a bounded ring on
`GoalNode` (same pattern as `coverage_signals`, RFC-SLP-005) and emitted
as a wire event. **Signal-only in v1** — no decision consumes it; it is the
tuning dataset for the bars and criteria text. Realized-concurrency fields
(timestamp overlap) are deferred until steps carry wall-clock timestamps.

### P4 — Deterministic shape guards

- **Single-child refusal (tool-level, pre-queue)**: `decompose_task` with
  exactly one subtask on an action-kind parent → refuse with guidance to
  continue in-thread (`write_todos`). Saves a full wave + barrier + warmup.
  Eval-kind parents are exempt — a single remaining subtask from a coverage
  verdict is continuation, not parallelization (RFC-SLP-003).
- **Sequential-chain flag (reconcile-level, advisory v1)**: children forming
  a pure chain commit normally but are flagged `sequential_only` in P3
  telemetry. Rejection is deferred to v2 and decided by P3 data — a
  reconcile-time rejection cannot steer the proposing thread (it already
  ended), so rejecting before the data justifies it only burns waves.

### P6 — Eval-continuation decompose headroom

Coverage-continuation children (from `coverage_verdict(complete=false,
remaining_subtasks)`) sit at lineage depth 3 under default `max_depth=3`
because the eval node consumes a tier — they are forced leaves even when the
remaining work is parallelizable. The eval node is a ROOT_EVAL-injected audit
checkpoint, not a decomposition level: `action_lineage_depth` (replacing
`lineage_depth`, single production caller) skips `kind="eval"` in the
parent-chain walk, giving continuation children the same headroom as root's
direct children. Runaway stays bounded by `max_steps` / `max_waves` /
`max_eval_rounds` / branch caps, which are independent of lineage depth.

### P5 — Context-pressure split hint

A coarse sync token estimate in the middleware (message text bytes ÷ 4 vs
`agent.loop.context_window_limit`) fires a one-time hint addendum at
`split_hint_threshold_pct` (default 0.60) of the limit — *below* the
compaction threshold (0.80) so the hint precedes evidence loss. Hint-only, so
estimate precision is acceptable; when a mid-step token emitter lands (the
deferred PCE open question), both can share it.

## Unified framing

P1–P5 are not five features; they are one decision pipeline staged over the
step lifecycle:

| Stage | When | Mechanism |
|---|---|---|
| 1. Thread start | first model call | t=0 verdict — strip on finish ≥0.9; nudge on root split (P1) |
| 2. In-thread | tool milestones, context pressure | evidence re-check (P2), split hint (P5) — the only stage that can un-strip |
| 3. Commit | decompose_task / reconcile | shape guards (P4) |
| 4. Tree-green | action tree green | quality telemetry (P3) — feeds future tuning |

Do-or-decompose, trust+guide, asymmetric fail-safe, and no-keyword-heuristics
are all preserved: every new signal is semantic (classifier verdicts, tool
counts, token pressure, DAG shape), never user-text keywords.

## Locked decisions

1. P1 bar for the nudge is `verdict_trusted`, not the suppression bar — the
   nudge is advisory and fail-safe-direction.
2. P2 re-checks are bounded (2/step) and gated to agent mode + action steps.
3. P3 is signal-only in v1; no consumer ships in this RFC.
4. P4 single-child refusal is tool-level (pre-wave); chain flag is
   reconcile-level and advisory; eval parents exempt from both.
5. P5 estimate is coarse-by-design at 0.60 of the context limit; hint-only.
6. Config lands on `DecomposeLoopConfig` next to the existing budgets.
7. P6 renames `lineage_depth` → `action_lineage_depth` (eval-skip) in
   place — old surface deleted, no alias; `max_depth` description updated.
8. P6 leaves the waves-exhausted eval-only claim filter untouched (anti-spin
   guard keeps authority; deeper recursion simply consumes waves).

## Open questions

1. Re-check trajectory state shape and char cap.
2. Whether P2 re-checks should also fire for child steps or roots only (v1:
   all action steps, budget-capped).
3. Chain-guard rejection thresholds once P3 data exists.
4. Whether the P5 estimate should switch to hop `usage_metadata` when a
   mid-step token emitter exists.