# IG-SLP-012: Intelligent Decompose Decision

**Scope**: `packages/soothe/` (sloop/decompose + stations/decompose + context + config + prompts + tests)
**Source**: Derived from RFC-SLP-006
**Related RFCs**: RFC-SLP-002 (revised §injection/nudge), RFC-SLP-003 (eval exemption), RFC-SLP-005 (ring/fingerprint patterns)

## Problem

The finish-vs-split classifier verdict is computed once per step at the first
model call and frozen: "split" verdicts are discarded, a t=0 "finish" strip
can never be revisited even when execution evidence later shows the step
spans independent workstreams, and the root parallel nudge is dead for
organic goals (only client-forced `intake_scope` sets root complexity to
"complex" since intake classification deletion). Nothing measures whether
committed decompositions were worth their wave + barrier + per-child context
warmup, and structural losses (single-child proposals, pure-chain children)
pass silently. Additionally the depth guard counts the eval tier
(root(1) → eval(2) → continuation children(3)), so children of a
`complete=false` verdict are forced leaves under default `max_depth=3`.

## Solution

An evidence-staged decision (RFC-SLP-006): root split verdicts earn the
parallel nudge; finish-vs-split re-runs at tool-call milestones and context
pressure with the tool trajectory as state (and can re-inject a stripped
tool); single-child proposals on action parents are refused pre-queue;
pure-chain children are flagged at reconcile; eval audit tiers stop
counting toward lineage depth (continuation children can decompose); a
`DecomposeQuality` ring on `GoalNode` records per-proposal outcomes at
tree-green (signal-only).

## Changes

### `packages/soothe/src/soothe/sloop/decompose/decompose_typesafe.py`

- `decide_finish_vs_split_typesafe` gains optional `tool_call_count: int |
  None` and `recent_tools: list[str] | None` params, merged into the
  classifier state when present (trajectory evidence for re-checks; t=0
  calls omit them, so the existing state shape is unchanged).
- "split" already returns after `verdict_trusted` — keep exactly that bar
  (advisory/fail-safe direction). No new thresholds here.

### `packages/soothe/src/soothe/sloop/decompose/runtime.py`

- `DecomposeRuntimeTokens` / `bind_decompose_runtime` gain `step_kind:
  str | None = None`; new accessor `current_step_kind()` (same
  contextvar pattern as `current_step_id`). The executor owns the kind at
  bind time; the tool reads it for the single-child guard.

### `packages/soothe/src/soothe/sloop/engine/execute/executor.py`

- At the existing `bind_decompose_runtime` call site (~line 2549), pass
  `step_kind=step.kind`.

### `packages/soothe/src/soothe/sloop/decompose/middleware.py`

- `_try_jev_finish_vs_split`: cache trusted "split" verdicts too
  (`_jev_finish_split.set("split")`); read the trajectory contextvars when
  re-evaluating and pass `tool_call_count` / `recent_tools` into the
  classifier call.
- New per-step contextvars: `_jev_tool_calls` (int), `_jev_recent_tools`
  (bounded list of `"{name}: {digest}"`, last 8, digest ≤ 120 chars),
  `_jev_rechecks_done` (int).
- `awrap_tool_call` (after the existing plan/ask guard passthrough):
  increment `_jev_tool_calls`, append the digest, and when the count hits a
  `recheck_tool_calls` multiple AND `_jev_rechecks_done <
  max_rechecks_per_step` → increment the recheck counter and reset
  `_jev_finish_split_step` so the next `awrap_model_call` re-evaluates.
  Never re-evaluate without new tool results (churn guard).
- `modify_request` (agent branch), in order:
  1. verdict "finish" → strip (unchanged);
  2. verdict "split" AND root AND `root_split_nudge` → append
     `PARALLEL_NUDGE_ADDENDUM` (once per step, keyed set like
     `_INJECTION_LOGGED_STEPS`);
  3. re-check verdict "split" while previously stripped → re-inject the tool
     and append `SPLIT_CANDIDATE_ADDENDUM` once.
- Context-pressure hint (P5): coarse estimate
  `sum(len(str(m.content)) for m in request.messages) // 4` vs
  `agent.loop.context_window_limit` from `conf.get("soothe_config")`; at
  ≥ `split_hint_threshold_pct` → append `CONTEXT_PRESSURE_SPLIT_HINT`
  once per step. Skip when the tool is currently stripped and re-checks are
  exhausted.
- All new paths gated on agent mode + non-eval step (existing
  `SOOTHE_EVAL_STEP_ID_KEY` / mode checks) — plan/ask behavior unchanged.

### `packages/soothe/src/soothe/sloop/decompose/tool.py`

- `_build_and_queue`: when `len(parsed) == 1` AND
  `current_step_kind() == "action"` → return refusal guidance (finish in
  this thread; use `write_todos`; one subtask gains no parallelism).
  Eval parents (`current_step_kind() == "eval"`) are exempt — coverage
  continuation semantics (RFC-SLP-003). No proposal is queued on refusal.

### `packages/soothe/src/soothe/context/models.py` (StepDAG)

- Replace `lineage_depth` with `action_lineage_depth(step_id)`: the
  parent-chain walk skips `kind="eval"` nodes (root = 1, missing = 0).
  Single production caller is the reconcile depth guard; delete the old
  method — no alias. Effect: root(1) → eval(skip) → continuation child(2)
  → child's proposal accepted under `max_depth=3`; grandchild(3) blocked.

### `packages/soothe/src/soothe/sloop/decompose/reconcile.py`

- Depth guard (~line 120): `dag.action_lineage_depth(parent_id)`.
- `plan_commit_from_proposals`: per accepted proposal, before the
  cross-proposal merge, compute `sequential_only` — n ≥ 2 children where
  resolving `depends_on_local` yields child i depending on child i-1 for
  all i and no other sibling edges. Expose on the result as
  `sequential_only_parents: set[str]` (commit unchanged; advisory flag).
- `ReconcileResult` gains `sequential_only_parents`.

### `packages/soothe/src/soothe/context/models.py`

- New `DecomposeQuality` model (fields per RFC-SLP-006 §3).
- `GoalNode` gains `decompose_quality: list[DecomposeQuality] =
  Field(default_factory=list)` next to `coverage_signals`; ring bounded by
  the writer (`quality_ring_size`).

### `packages/soothe/src/soothe/sloop/stations/decompose/root_eval.py`

- At entry, when the action tree is green: for every `decomposed` parent
  whose children are all terminal, append a `DecomposeQuality` record
  computed from the StepDAG + child `StepExecution` records (child_count,
  single_child, sequential_only, independent_children, deepest_chain_len,
  child_failures, child_retries, child_tool_calls_sum,
  child_duration_ms_sum). One record per parent per goal (idempotent — skip
  parents already recorded). Bound the ring to `quality_ring_size`;
  `ctx.ce.defer_save()`. Emit a `decompose_quality` stream event with the
  record payload.

### `packages/soothe/src/soothe/config/models.py`

- `DecomposeLoopConfig`: update `max_depth` description to
  "Max action-lineage decomposition depth per goal (eval audit tiers do not
  count)". Gains: `root_split_nudge: bool = True`,
  `recheck_tool_calls: int = Field(default=10, ge=0)`,
  `max_rechecks_per_step: int = Field(default=2, ge=0)`,
  `split_hint_threshold_pct: float = Field(default=0.60, ge=0.0, le=0.95)`,
  `quality_ring_size: int = Field(default=20, ge=1)`.

### Prompts

- NEW `prompts/fragments/decompose/split_candidate_addendum.xml`:
  the re-check classified this step's remaining work as spanning independent
  areas; if 2+ independent parts remain, call `decompose_task` now (fresh
  child contexts), else finish here. Terse (≤ 15 lines).
- NEW `prompts/fragments/decompose/context_pressure_split_hint.xml`:
  context pressure is high; remaining independent work is cheaper as child
  threads than as a compacted thread. Terse (≤ 10 lines).
- Export both from `prompts/fragments/__init__.py` and `prompts/__init__.py`
  (the `PARALLEL_NUDGE_ADDENDUM` pattern).

## Testing

New/extended unit tests in `packages/soothe/tests/unit/core/loop/decompose/`:

- `test_decompose_middleware.py`: root + trusted split → nudge appended
  once; untrusted split → no nudge; non-root split → no nudge; milestone
  re-check fires exactly one classifier call with trajectory state and
  respects `max_rechecks_per_step`; trusted split re-check on a stripped
  step re-injects the tool + addendum; trusted finish re-check keeps the
  strip; no-new-tools model call never re-evaluates; pressure hint at 0.60
  of the limit, once per step, agent mode only.
- `test_decompose_task_tool.py`: single-child refusal on action parents
  (no proposal queued, guidance returned); single child on eval parent
  queues normally.
- `test_reconcile.py`: pure-chain proposal commits with
  `sequential_only_parents` set; branched proposal does not flag.
- `context/test_step_dag.py`: rename `test_lineage_depth` →
  `test_action_lineage_depth`; keep the R/A/B chain assertions; add
  eval-skip cases (root → eval → child: child depth 2; eval itself 0).
- `test_reconcile.py`: update the `max_depth_exceeded` test comment
  (action-lineage semantics); NEW eval-continuation case — root(decomposed)
  → eval → child, child's proposal commits under default `max_depth=3`;
  child's grandchild proposal rejected; two eval rounds still bounded by
  `max_steps`.
- NEW `test_decompose_quality.py`: quality fields at tree-green (hand-built
  DAG with mixed children), ring bound, idempotent skip of recorded parents,
  CE persistence roundtrip.
- `test_root_eval_pce.py`-style mode gating: plan/ask → no nudge, no
  re-check, no hint.
- Classifier-unavailable injection at each stage → tool surface unchanged.

Run `./scripts/verify_finally.sh` before commit. Run the soothe unit suite
(`packages/soothe/tests/unit/`) standalone — the cli/daemon combined pytest
run has a pre-existing asyncio-fixture conflict.

## Cleanse (same change)

- Remove the now-dead comment block in `modify_request` describing the
  nudge as client-forced-only once the JEV path lands (the comment, not the
  forced path itself).
- No compat shims: `DecomposeQuality` is additive; no old surface renamed.