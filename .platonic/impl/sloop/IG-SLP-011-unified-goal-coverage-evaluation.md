# IG-SLP-011: Unified Goal Coverage Evaluation — PCE

**Scope**: `packages/soothe/` (new `sloop/pce/` module + context models + config + root_eval + executor events + finalize + tests)
**Source**: Derived from RFC-SLP-005
**Related RFCs**: RFC-SLP-003 (revised §When Eval Runs, §Envelope), RFC-LOOP-009

## Problem

Coverage evaluation is terminal-only. `eval_required()` triggers on
decomposition, >1 completed action leaf, or early-exit close reports —
so single-leaf goals (loop 7d10: 92 and 327 tool calls) finalize with
zero coverage audit, and nothing observes goal progress mid-execution.
The COMPLEX force-eval branch is dead code because
`routing_classification` is never populated.

## Solution

A **PCE monitor** — a per-loop asyncio task spawned at graph invoke —
observes the full goal DAG while goals execute and appends
**CoverageSignal** records onto `GoalNode`. Typesafe-first verdicts,
fingerprint-cached (identical DAG state never re-evaluates — the 7d10
finish-vs-split churn lesson), LLM escalation bounded per goal.
Signal-only: PCE never mutates goals/steps or interrupts execution.

ROOT_EVAL gains a **fourth eval-required condition** from unconsumed
signals (closes the single-leaf gap); the Eval envelope and finalize
validation consume signals as evidence.

## Changes

### NEW `packages/soothe/src/soothe/sloop/pce/`

- `events.py`: `PceEvent` (kind: `step_started | step_completed |
  step_failed | decompose_committed | close_report_filed |
  tool_milestone | elapsed_milestone`, plus step/goal
  ids and milestone payload) and `PceEventQueue` — bounded
  `asyncio.Queue` (maxsize 256, drop-oldest on overflow; events are
  hints, losing one is safe).
- `projection.py`: `snapshot_goal_dag(context_engine) -> PceSnapshot`
  and `fingerprint(snapshot) -> str`. Snapshot: all goals (id, status,
  description) + steps (id, kind, status, tool_count, tokens, elapsed,
  close_report presence) + ledger digest capped at 2k chars. The
  fingerprint hashes the canonical serialization.
- `typesafe_pce.py`: `decide_pce_verdict_typesafe(snapshot)` — one
  Choice question, labels `on_track | coverage_gap |
  goal_likely_complete | runaway_step | stalled | scope_drift` +
  confidence/probabilities. Reuse `verdict_trusted` (drift guard:
  <0.6 confidence or near-tie → untrusted). Escalation:
  `pce_llm_verdict` fast-model call over the same snapshot, same
  labels, compact prompt.
- `monitor.py`: `PceMonitor` — owns the event queue, spawns as
  `loop-pce:{loop_id}` at graph invoke (alongside `pump_task`,
  strange_loop.py:1034), cancelled in the `finally` block. The
  fingerprint cache is keyed per goal (`_last_fingerprints[goal_id]`)
  and the milestone counters folded into a goal's fingerprint are scoped
  to that goal's step ids (`_goal_milestone_state`), so switching goals
  neither suppresses nor invalidates another goal's passes. Pass loop:
  drain events → debounce (`min_pass_interval_s`, coalesce) →
  snapshot → fingerprint check (skip if unchanged since last verdict) →
  typesafe verdict → escalate if untrusted / conflicting consecutive
  verdicts / boundary event → append `CoverageSignal` to the active
  goal's `coverage_signals` ring. Boundary batches skip the typesafe
  round trip and go straight to the LLM (typesafe fallback when the LLM
  is unavailable) — the ROOT_EVAL gate needs fresh signals fast. Caps: `max_passes_per_goal` (20),
  `max_llm_escalations_per_goal` (5). Every failure path is
  log-and-skip; the monitor never raises into the loop.

### `packages/soothe/src/soothe/context/models.py`

- Add `CoverageSignal` model (fields per RFC-SLP-005 §Abstract
  Schemas: signal_id, goal_id, source, kind, confidence, evidence,
  created_at, consumed_by).
- Add `coverage_signals: list[CoverageSignal] = Field(default_factory=list)`
  to `GoalNode` (context/models.py:373), next to `evidence_ledger`.
  Persistence flows through the CE store (GoalNode serialized wholesale);
  ring bounded by the monitor on append (`signal_ring_size`, default 20).

### `packages/soothe/src/soothe/config/models.py`

- Add `PceLoopConfig` after `EvalLoopConfig` (config/models.py:902):
  `enabled: bool = True`, `tool_call_interval: int = 25`,
  `elapsed_milestone_s: int = 600`,
  `min_pass_interval_s: int = 30`, `max_passes_per_goal: int = 20`,
  `max_llm_escalations_per_goal: int = 5`, `signal_ring_size: int = 20`,
  `gate_settle_timeout_s: int = 20` (bounded ROOT_EVAL wait for in-flight
  boundary passes; 0 disables).
- Wire `pce: PceLoopConfig` onto the loop config that owns `eval`
  (`config.agent.loop`).

### `packages/soothe/src/soothe/sloop/strange_loop.py`

- In `run_with_progress` where `pump_task` spawns (~line 1034): spawn
  the PCE monitor when `config.agent.loop.pce.enabled` and
  `interaction_mode` not in `_READONLY_MODES` (plan/ask never spawn
  it — same gate as root_eval.py:230). Cancel in the existing
  `finally` before pool close. The monitor handle lives on the loop
  runtime so stations/executor can reach the event queue.

### `packages/soothe/src/soothe/sloop/engine/execute/executor.py`

- Emit `tool_milestone` events: after each tool call completes, when
  `tool_call_count % tool_call_interval == 0` (`_aggregate_tool_message`).
- Emit `step_started` in `_execute_step_collecting_events` (executor).
- Terminal events emit POST-COMMIT, not from the executor:
  `step_completed` / `step_failed` / `close_report_filed` fire in
  `record_progress.py` after the CE status flip and close-report attach
  (plus the ask-step path in `execute.py`). The original executor-side
  emission raced the status flip — the PCE snapshot still showed the step
  active, so the completion pass hit the unchanged-fingerprint skip.
- `elapsed_milestone` needs no executor hook: the monitor's queue wait
  times out at `elapsed_milestone_s` and synthesizes the event.
- `token_milestone` was removed in the post-impl cleanse: no mid-step input-token sums exist at the tool-completion site, so the config field and event kind had no producer. Re-add both together with an emitter based on hop `usage_metadata`.
- Emission is fire-and-forget into the bounded queue; executor never
  awaits monitor consumption. `_emit_pce` has no broad except — the
  graph-interrupt guard test forbids swallow-sites on the stream path,
  and `emit_pce_event` is itself fail-safe.

### `packages/soothe/src/soothe/sloop/stations/decompose/root_eval.py`

- Gate settle: before the insert/skip decision, ROOT_EVAL calls
  `_settle_pce_monitor(ctx)` → `PceMonitor.settle(gate_settle_timeout_s)`
  so the completion-time signal lands before the gate reads it.
  Fail-open: no monitor / timeout / 0 config simply proceeds.
- Fourth eval-required condition, evaluated before the
  `eval skip predicate` branch (~line 346): unconsumed
  (`consumed_by is None`) signals on the current goal of kind
  `coverage_gap | runaway_step | stalled | scope_drift`, or
  `goal_likely_complete` with confidence < 0.8 → Eval required even
  for single-leaf goals. Mark consumed signals `consumed_by="root_eval"`.
- Envelope: `_eval_envelope` appends the latest unconsumed signals as
  labeled untrusted evidence (same formatting as deferred items).

### `packages/soothe/src/soothe/sloop/engine/completion/`

- `finalize.py` / `report_validation_typesafe.py`: when Eval was
  skipped and the goal has signals, include the latest signal kind +
  confidence in the validation context; mark it
  `consumed_by="finalize"`.

## Testing

New unit tests in `packages/soothe/tests/unit/`:

- `sloop/pce/test_pce_monitor.py`: fingerprint skip (identical
  snapshot → single classifier call), per-goal cache keying (goal switch
  does not invalidate another goal's entry; other-goal milestones do not
  leak into the fingerprint), debounce coalescing, per-goal
  pass/escalation caps, error injection (classifier unavailable, LLM
  failure, crash) leaves the loop unaffected, ring bound.
- `sloop/pce/test_pce_events.py`: queue drop-oldest under overflow, registry
  routing, unknown-loop/kind tolerance, `close_report_filed` in
  `BOUNDARY_EVENT_KINDS`. (Milestone arithmetic lives in the executor
  `_aggregate_tool_message` counter; the interval read is covered indirectly
  via `_pce_tool_interval`.)
- `test_pce_monitor.py` race regression: a fingerprint-skipped pass must not
  advance the debounce window — the post-commit `step_completed` pass runs
  immediately after a skipped `close_report_filed` pass.
- `sloop/pce/test_pce_mode_gating.py`: plan/ask → monitor never
  spawned.
- `sloop/test_root_eval_pce.py`: single-leaf goal + `runaway_step`
  signal → `eval_required` True; consumed marking; `goal_likely_complete`
  conf >= 0.8 does not force Eval.
- `context/test_goal_signals.py`: CoverageSignal persistence roundtrip
  through the CE sqlite store; rehydration after checkpoint reload.

Run `./scripts/verify_finally.sh` before commit. Run soothe unit suite
(`packages/soothe/tests/unit/`) standalone — the cli/daemon combined
pytest run has a pre-existing asyncio fixture conflict.

## Non-Goals (from RFC-SLP-005)

No PCE-initiated execution changes; no `routing_classification`
population; no finish-vs-split classifier cache fix. `eval_close`
signal backfill is a follow-up (Open Question 2).

## Post-Implementation Cleanse (2026-11-11)

Rule 6/19 pass after live verification (loops 01a1c5c0, 01a1c88e).

- Removed `token_milestone` entirely (config field, event kind,
  `input_tokens` absorb key): no producer existed — mid-step input-token
  sums are not available at the tool-completion site. Re-add both together
  with an emitter based on hop `usage_metadata`.
- Removed unused `PceEvent.ts` field (written, never read).
- Trimmed `sloop/pce/__init__.py` to the house-style docstring: all
  consumers import submodules directly, so the re-export block was unused
  indirection.
- Absorb simplified to `tool_milestone` → `tool_count` only: the
  synthesized elapsed event carries no payload, so the other absorb keys
  were no-ops.
- Replaced Mock-tolerance `getattr`/`isinstance` guards in
  `root_eval`/`finalize`/`record_progress` with direct typed access
  (`GoalNode.coverage_signals` always exists; the auto-final test Mock
  now sets `coverage_signals = []`).

Kept: `elapsed_milestone_s` (drives the monitor queue-wait timeout),
`close_requires_eval` on the snapshot (fingerprint input),
`reset_pce_registry` (test isolation).

Net −65/+17 lines; full suite and `verify_finally.sh` green after the pass.
