# IG-SLP-013: Goal-Completion Report First-Char Latency

**Scope**: `packages/soothe/` (config + planning completion + sloop utils messages + synthesis + tests)
**Source**: Loop `01a211ac-28c9-74a2-b304-e9d2c76fa419` incident analysis (2026-11-25)
**Related RFCs**: RFC-SLP-004 (short-chain fast finalize), RFC-SLP-005 (unified goal coverage evaluation)

## Problem

Loop a419 (trivial goal: write a 10-byte file) spent 219s wall time; the
goal-completion report contributed **160.9s of pure first-char latency**.
The synthesis LLM stream (`openai/hy4`, ~11.6k-char prompt) emitted zero
chunks for 160.9s, then delivered all 60 chunks (1169 chars) in one burst.
Downstream (finalize emit → daemon websocket adaptive delivery → TUI
incremental render) is streaming-friendly; the entire latency is upstream:

1. **Wrong model tier**: `goal_synthesis_model_role` defaults to
   `"default"` (heavy model). The fast role (hy4-flash) showed 1–4s TTFT
   in the same loop. For a trivial goal the heavy model spent ~160s before
   emitting any text (long internal reasoning and/or gateway buffering —
   no usage metadata from the provider either).
2. **Silent topology gate misfire**: `_dag_requires_synthesis` returned
   False ("simple execution → no synthesis required"), but
   `_ledger_direct_eligible` silently rejected because the audit step
   (`kind="eval"`, inserted by ROOT_EVAL with `parent_step_id=root.id`
   and no `dependencies`) breaks both the single-root check and the
   decomposition-children check in `_is_linear_chain`. `LEDGER_DIRECT`
   — a zero-synthesis passthrough — was never reached, and the rejection
   was logged nowhere.
3. **No TTFT budget**: the synthesis stream reader reuses
   `dispatch_idle_seconds` (default 240s) as its idle watchdog. That
   watchdog targets dispatch deadlocks, not latency; heartbeat sentinels
   (10s) keep the stream "alive" while masking the stall, so neither the
   `_SYNTHESIS_STALL_RETRY_MAX` re-dispatch nor the fallback summary ever
   fired within the incident window.

## Solution

Three layers, ordered by expected impact:

1. **Fast model for synthesis by default** — flip the
   `goal_synthesis_model_role` default to `"fast"`. Missing/unusable fast
   roles already fall back to the `default` spec inside
   `soothe_nano.llm.LLMFactory.create_chat_model` (empty-spec and
   instantiation failure paths both retry `default`), so no resolution
   changes are needed in `StrangeLoop`.
2. **Eval steps are audit, not structure** — `_ledger_direct_eligible`
   excludes `kind="eval"` nodes before the linear-chain check (and logs
   every rejection reason, ending the silent gate). The eval step carries
   a coverage verdict, not the deliverable; it must not distort the
   topology decision nor become the report source. Symmetrically,
   `last_step_output_content` prefers the latest non-eval completed step
   so `LEDGER_DIRECT` passthrough surfaces the execute deliverable
   (e.g. "Updated file /tmp/appr-1.txt …") instead of the verdict prose.
   Typesafe report validation (RFC-SLP-004) still gates the passthrough;
   unavailable/untrusted verdicts keep the fail-safe synthesize path.
3. **Synthesis-specific idle budget** — new
   `agent.loop.goal_synthesis_idle_seconds` (default 60s, `0` disables)
   feeds the synthesis stream reader's idle watchdog instead of
   `dispatch_idle_seconds`. With no tools active during synthesis, idle
   always means "provider emitted nothing for N seconds"; the existing
   re-dispatch → fallback machinery (`_astream_with_stall_retry`) then
   bounds the worst case to roughly `(retry_max + 1) × idle`. An
   explicitly configured value always wins; when the field is left at its
   default and a non-fast synthesis role is pinned, the dispatch deadlock
   budget (240s) is used instead — heavy models that legitimately take
   minutes to emit their first token must not lose the report to a stub.
   The scratchpad path keeps its `dispatch_idle_seconds`-based **total**
   timeout unchanged (different semantics; a short total budget would
   truncate long scratchpad reports wholesale).

Deferred (follow-ups, not in this IG): prompt slimming for trivial
scenarios (agent-instructions block dominates the 11.6k-char prompt), and
a manual probe of the tokenlink gateway's SSE behavior for the heavy
model (both models may buffer; the fast-model default is expected to
sidestep it).

## Changes

### `packages/soothe/src/soothe/config/models.py`

- `goal_synthesis_model_role` default `"default"` → `"fast"`; description
  notes the automatic fallback to the `default` spec when the fast role
  is not configured.
- Add `goal_synthesis_idle_seconds: float = Field(default=60.0, ge=0)`
  next to it: max seconds the synthesis stream may emit nothing before
  the idle watchdog re-dispatches/falls back (`0` disables the guard;
  explicitly configured values always win). Distinct from
  `dispatch_idle_seconds` (deadlock watchdog for the execute graph,
  default 240s) — synthesis has no tools, so idle here is a pure
  TTFT/inter-chunk budget. A non-fast role left at the field default
  uses the dispatch deadlock budget instead.

### `packages/soothe/src/soothe/sloop/engine/completion/synthesis.py`

- Add `_synthesis_idle_seconds()` and use it for the standard-path
  reader idle timeout in `_astream_with_stall_retry`. It reads
  `goal_synthesis_idle_seconds` directly; an explicitly configured
  value wins, otherwise a non-fast synthesis role uses the dispatch
  deadlock budget and the fast role uses the 60s default. No
  compatibility shim is kept — `StrangeLoopConfig` always declares
  the field.
- The scratchpad `asyncio.timeout` keeps `_dispatch_idle_seconds()`
  (total-timeout semantics unchanged); docstring updated to name both
  knobs.

### `packages/soothe/src/soothe/context/planning_completion.py`

- `_ledger_direct_eligible`: normalize `dag_nodes` (accept `StepDAG` or
  dict), drop `kind == "eval"` nodes, and run `_is_linear_chain` on the
  remainder. Step counts and last-wave tool-call counts are derived from
  the same non-eval set (eval tool calls of the last wave are
  subtracted); a non-eval step depending on an eval id is rejected
  instead of silently losing that dependency edge. An all-eval DAG now
  fail-safes to synthesize — previously a single eval node passed the
  linear-chain check and was eligible. An empty DAG keeps its previous
  eligible behavior.
- Add INFO logs to every rejection branch (steps/failed/chain/tool
  calls/dependency) with the decisive numbers, one reason per branch —
  the a419 investigation was blocked by the silent gate.

### `packages/soothe/src/soothe/sloop/utils/messages.py`

- `last_step_output_content`: prefer the latest completed step with
  `kind != "eval"`; fall back to the latest eval step only when no
  non-eval completed step exists (then `last_ledger_ai_content` as
  today). No behavior change for DAGs without eval steps.

### `packages/soothe/src/soothe/sloop/strange_loop.py`

- No changes: `create_chat_model(role)` already auto-falls back to the
  `default` spec when the fast role is absent or fails to instantiate,
  and the existing `except → self._fast_llm` belt stays.

## Tests (`packages/soothe/tests/unit/`)

### `tests/unit/core/loop/engine/completion/test_synthesis_stall_retry.py`

- `_generator` sets `goal_synthesis_idle_seconds=_IDLE_SECONDS` (new
  field); all existing scenarios keep passing (retry, fallback,
  provided-fallback text, partial-keep, healthy stream).
- `goal_synthesis_idle_seconds=0` runs the plain-astream path: a
  slow-but-healthy stream completes without retry (watchdog disabled).
- A differential case sets `dispatch_idle_seconds=0.2` and
  `goal_synthesis_idle_seconds=3.0` with a first token at 1.2s, so a
  regression back to the deadlock knob fails the test.
- A scratchpad-mode case patches `asyncio.timeout` and asserts the
  total timeout still comes from `dispatch_idle_seconds`.
- Budget-resolution cases pin the fast default, a heavy role (dispatch
  budget), and an explicit override.

### `tests/unit/context/test_linear_chain_fast_finalize.py`

- Execute + eval DAG (eval node: `parent_step_id=execute_id`, no deps) →
  `_ledger_direct_eligible` True; the same DAG with a second non-eval
  parallel branch → False.
- All-eval DAG → False; empty DAG → True (existing behavior).
- Three deliverable + one eval step → True; eval tool calls excluded
  from the tool cap; a dependency on an eval id → False.
- caplog assertions on the failure and tool-call rejection logs.

### `tests/unit/core/loop/engine/completion/test_last_step_output_content.py`

- Execute + eval completed steps → returns the execute step's output,
  not the eval verdict text.
- Only eval steps completed → returns the eval step output (existing
  ordering fallback), then `last_ledger_ai_content` when empty.

### `tests/unit/core/loop/core/test_strange_loop_model_roles.py`

- New: default config resolves `goal_synthesis_model_role == "fast"` and
  `StrangeLoop` asks `create_chat_model` for `"fast"`.

## Migration / Compatibility

- **Config default change**: deployments that never set
  `goal_synthesis_model_role` now synthesize with the `fast` role;
  routers without a fast entry transparently keep using the `default`
  model (factory fallback). Pinning `"default"` restores the heavy tier
  and also restores the 240s dispatch-scale idle budget unless
  `goal_synthesis_idle_seconds` is set explicitly.
- **New config field**: `goal_synthesis_idle_seconds` defaults to 60s
  for the fast role; `0` disables the guard entirely, and any explicitly
  configured value applies to all model roles.
- **No schema/persistence migration**: strategy selection, watchdog, and
  report sourcing are runtime-only decisions.

## Coding Plan

### Task 1: Config
**Files**: `config/models.py` — role default flip + new idle field.

### Task 2: Synthesis idle budget
**Files**: `sloop/engine/completion/synthesis.py` — `_synthesis_idle_seconds()`,
standard-path wiring, docstring.

### Task 3: Eval-aware completion strategy
**Files**: `context/planning_completion.py` — eval filter + rejection logs.

### Task 4: Eval-aware report source
**Files**: `sloop/utils/messages.py` — non-eval preference in
`last_step_output_content`.

### Task 5: Tests
All files listed above.

### Execution Order

1 → 2 → 3 → 4 → 5 (tests last; each task is independently green).

### Verification

- Targeted unit tests (single command):

```bash
.venv/bin/python -m pytest \
  packages/soothe/tests/unit/core/loop/engine/completion/ \
  packages/soothe/tests/unit/context/ \
  packages/soothe/tests/unit/config/test_config.py \
  packages/soothe/tests/unit/core/loop/core/test_strange_loop_model_roles.py
```

- `ruff check` + `ruff format --check` on touched files.
- `./scripts/verify_finally.sh` before any commit.
- Live replay: rerun the a419-style goal ("create a file … containing …")
  and confirm in runner.log either `action=ledger_direct passthrough
  (validated)` or `(structural leaf)` (IG-SLP-014, target ~0s), or a
  synthesis stream whose first chunk arrives in seconds (fast model) with
  the 60s idle budget as backstop.
