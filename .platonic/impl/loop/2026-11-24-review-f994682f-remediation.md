# IG-XXX: Full-Package Review Remediation (review f994682f)

## Summary

Fix the 2 Critical + 12 Major findings from the full-package static review
(`.review/f994682f/code-review.md`). No scope beyond the review's todo list;
each fix follows the suggestion recorded in the report's Section 4.

## Design Decisions

1. **Approval decisions (Critical)** — token contract, not content judgment.
   `answer_to_decision` and the veritas token classifier resolve canonical
   action tokens by exact match (whitespace/case normalized) against a
   declared vocabulary; empty/unrecognized answers fail safe to reject, and
   the veritas approval question solicits an explicit `approve`/`deny`
   token. Prose never resolves a decision (escalates). This keeps the wire
   contract structured per code-style rule 9 without introducing an
   additional light-LLM classification hop.
2. **Writer init deadlock (Critical)** — resolve the shared pool BEFORE
   entering the thread-affine init lock; the critical section is strictly
   synchronous. Racing losers construct throwaway instances with no worker
   or I/O resources, so discarding them leaks nothing.
3. **Bound-loop lifecycle** — `close_shared_instance` clears `_bound_loop`
   after the marshalled shutdown (guarded against a concurrently created
   new singleton); `_run_on_main` rebinds when the bound loop is closed.
4. **Released loops are per-run** — a durable boundary write re-attaches the
   loop (`discard` on durable enqueue). Late index-only stragglers stay
   filtered; a resumed run's writes resume with its first durable write.
5. **Shutdown drain** — `_drain_pending` pauses the worker, flushes pending
   loops under `asyncio.timeout(close_timeout)`, logs dropped loop IDs.
6. **Shared pool loop affinity** — singleton classmethods marshal onto one
   bound owner loop (`bind_shared_pool_main_loop`, wired in the daemon's
   `pools.py`), mirroring the writer/sqlite-coordinator pattern; the
   registry-bind path was also restructured to avoid re-entering the
   non-reentrant `_pool_lock` (latent deadlock found during the fix).
7. **Parallel wave abort** — step tasks enqueue their terminal outcome
   before re-raising `CancelledError`/`GraphInterrupt`; the consumer treats
   such results as wave-terminal: cancel siblings, propagate.
8. **`LoopState` scratch** — `ContextVar` instead of a class-level store;
   reset in `finally` so failed constructions cannot leak captured kwargs.
9. **Partial workspace recovery** — counters/`checkpoint_id` describe the
   last APPLIED checkpoint; `CheckpointPayload.incomplete` marks partial
   replay.
10. **`drain_goal_runtime` guard** — refuse to signal this process, its
    group, or any ancestor/ancestor group; markers are agent-writable
    input, and the daemon is the process the agent's own kill guards
    already protect. Layered with spawn-time ownership sidecars
    (`<marker>.meta`, schema 1: pid + pgid + OS start-time token) written
    by `soothe-nano`'s shell tools at spawn (foreground sessions, host
    background, guest one-liner best-effort); the drain cross-checks the
    sidecar against the live process before signaling — a missing or
    mismatched sidecar refuses the kill (marker kept for inspection).
    With a soothe-nano that predates sidecar support, validation degrades
    to filename + protected-set checks with a one-time warning; the
    `soothe-nano` floor is bumped together with that release.
11. **Langfuse** — runtime import moved to an `observability` extra;
    missing install warns once instead of debug-level silence.
12. **Ledger corruption** — `LedgerCorruptionError` (new
    `soothe/context/errors.py`) from both stores' parse failure; DB
    unavailability still returns `[]`.
13. **Backend lifecycle** — `WorkspaceSyncBackend.aclose()` protocol
    method; `Workspace.close()` closes the per-run content backend (the
    shared primary backend stays owned by the manager).

## Verification

- New regressions: `tests/unit/persistence/test_loop_writer_fixes.py`
  (init race, re-attach, shutdown drain, bound-loop clear/rebind),
  wave-abort tests in `test_executor_parallel_live_stream.py`,
  schema-leak test in `test_schemas.py`, partial-recovery assertions in
  `test_checkpoint.py`, protected-PID test in
  `test_runner_drain_goal_runtime.py`.
- Updated contract tests: `test_tool_approval_bridge.py`,
  `test_auto_mode.py` (prose → safe reject/escalate), corrupt-ledger test
  now expects `LedgerCorruptionError`, content-mirror factory test uses
  per-run backends.
- `pytest packages/soothe/tests/unit` — 3150 passed / 16 skipped;
  `pytest packages/soothe-daemon/tests/unit` — 657 passed;
  `ruff check` + `ruff format --check` clean.

## Known Limitations

- The wave-abort fix is verified at unit level with a stubbed step
  executor; the report's dynamic-verification note (graph-runtime
  integration) still applies.
- Marker ownership validation is layered defense, not proof: the sidecar
  lives in the same agent-writable workspace, so a determined agent that
  reads the OS start time could still forge a matching pair for a
  non-protected process. The unforgeable guarantees remain the
  protected-set (this process + ancestors) and process isolation.
