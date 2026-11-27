# IG-DAEM-004: Daemon Restart Resilience — Durable Loop Input, Auto-Resume Default, TUI Restart Awareness

**Implementation Guide**: IG-DAEM-004
**Title**: Loop input survives daemon restart; auto-resume on by default; TUI restart-aware reconnect
**Status**: Implemented
**Created**: 2026-11-27
**Dependencies**: Rule 10 (Unified Persistence Backend), Rule 15 (Reentrant Loop State), §7b (Package Boundaries)
**Related**: [IG-670](../../archive/impl/IG-670-daemon-auto-resume-interrupted-goals.md),
[RFC-DAEM-005](../specs/daemon/RFC-DAEM-005-daemon-communication-protocol.md),
[RFC-306](../specs/RFC-306-durability-protocol-architecture.md)

---

## Problem

A daemon restart (graceful or crash) loses loop work in three ways:

1. **Queued loop inputs evaporate.** `LoopInputDispatcher` keeps one in-memory
   `asyncio.Queue` per loop; `shutdown()` explicitly drops queued-but-unstarted
   messages and a crash evaporates them. The user's typed text is unrecoverable —
   auto-resume can only synthesize a generic "continue this loop" prompt.
2. **Auto-resume is opt-in.** `agent.loop.checkpoint.auto_resume_on_start`
   defaults to `false`, so after restart a `running` loop sits parked until a
   human runs `soothe loop continue`. Its availability peeks also fail silently
   (`debug` logs), degrading recovery to guesswork.
3. **The TUI cannot tell that the daemon restarted.** Reconnect logic resets
   client-side seq floors and reattaches, but the user sees no signal that the
   daemon restarted and their loop was recovered — and a turn that fails on a
   dead daemon is dropped entirely rather than parked locally.

## Decision

### D1 — Durable loop-input queue (write-ahead wall)

New daemon-owned store `soothe_daemon.persistence.loop_input_store` (SQLite via
`SqliteRuntimeRegistry` + PostgreSQL variant, selected by
`persistence.default_backend` — same factory pattern as the cron store).
Table `loop_input_queue(id, loop_id, payload, created_at)` plus
`loop_runtime_meta(key, value)` for daemon-side runtime counters.

Semantics — **complete-before-process**:

- `enqueue`: persist the JSON payload first, then hand it to the in-memory
  queue with a `__durable_id`. Oversized payloads (> 16 MiB serialized) fall
  back to memory-only with a warning — the fast path never breaks.
- Worker dequeue: delete the row *before* processing. A crash mid-turn loses
  that one in-flight message but the loop itself is recovered by auto-resume
  (synthetic re-entry), which is strictly better than re-processing a
  potentially half-ingested user turn.
- Restart recovery (`LoopInputDispatcher.recover_pending_inputs`, called from
  `SootheDaemon.start()` *before* `_detect_incomplete_threads`): all remaining
  rows are re-enqueued in order with their original durable ids. Rows survive
  until a worker actually picks them up, so the queue is crash-safe across
  repeated restarts.
- `cleanup_loop` deletes the loop's rows (loop deletion / GC path unchanged).

Payloads are the exact `loop_input` queue payloads (text, client_id, options,
attachments) — attachments ride along in JSON, which closes the attachment
consistency gap.

### D2 — Auto-resume default on + fail-loud peeks

`LoopCheckpointConfig.auto_resume_on_start` flips to `true`. The classifier
semantics are unchanged (age cancel, clarification policy, autopilot exclusion,
concurrency cap all keep IG-670 behavior); only the default and the observability
change: checkpoint/clarification/list peeks upgrade `debug` → `warning` so a
broken persistence layer is visible instead of silently degrading recovery.

### D3 — Durable outbound event seq

The per-loop outbound `seq` counter (`QueryEngine._loop_event_seq`) is persisted
to `loop_runtime_meta` at every turn terminal (`_emit_turn_stream_end`) and on
daemon stop (`flush_loop_seqs`); a fresh process hydrates the counter before the
turn's first frame (`hydrate_loop_seq`, called from the loop-input worker path).
Client sessions already reset their seq floors on reconnect, so this is a
daemon-only change: the seq namespace no longer rewinds, which keeps
reattach-replay and post-restart frames orderable.

Turn-boundary atomicity itself (checkpoint `execution_checkpoint` +
`interrupted` status + auto-resume re-entry) is *existing* behavior
(`tests/integration/daemon/test_daemon_thread_recovery.py`) and is intentionally
not re-implemented — D2 makes it actually fire by default.

### D4 — `daemon_info` RPC + TUI restart awareness

Additive RPC `daemon_info` → `{daemon_id, pid, started_at, version,
readiness_state}` where `daemon_id = f"{pid}:{started_at}"`. The TUI
(`TuiDaemonSession.fetch_daemon_info`) compares `daemon_id` across connections;
on change it mounts a system banner ("Daemon restarted — loop state reattached;
queued input preserved") and re-queues a turn that died on a connection error
(reuse of the existing `_pending_messages` queue, which already drives the
queued-goals bar and drain loop). Command-looking input (`/…`) is never
re-queued blindly.

### Explicitly out of scope

Full stream replay (`stream_resume` with `last_seq`) requires a wire-protocol
revision synchronized across all four client repos (RFC-450) — card-ledger
reattach replay remains the recovery path for missed stream frames. Follow-up
candidate under RFC-DAEM-005.

## Follow-up implemented (same IG, 2026-11-27)

The `stream_resume` capability and its consumption wiring landed additively:

- **Daemon**: `query/stream_replay.py` — bounded per-loop ring of recent
  seq-stamped frames (512 frames / 64 loops, LRU, process-local) recorded from
  `_broadcast_loop_message`; `stream_resume` RPC (`_handle_stream_resume`)
  returns frames after `last_seq` plus `oldest_seq`/`newest_seq`/`gap`.
  Documented in the AsyncAPI spec (`daemon_info` + `stream_resume`).
- **Python client**: `DaemonSession` tracks a per-loop stream-seq cursor
  (`_note_stream_seq` in both `iter_turn_chunks` and the post-idle drain;
  survives reconnects within one daemon lifetime, per-loop LRU map ≤ 32),
  exposes `last_stream_seq()` / `reset_stream_seq_tracking()` / `resume_stream()`.
- **TUI**: `_refresh_daemon_epoch` resets the client cursor on detected daemon
  restart (the seq namespace rewinds); the background consumer reconnect path
  calls `_replay_missed_stream_frames()` — buffered card frames are applied
  through the same `_apply_card_wire_frame` pipeline as live frames, gaps are
  logged (card-ledger reattach replay stays authoritative for evicted spans).

Remaining: go/rust/typescript clients may adopt `stream_resume` at their own
pace (additive RPC, older daemons degrade to card replay).

## Changes

| Area | File |
|------|------|
| Durable store (SQLite + PG + factory) | `packages/soothe-daemon/src/soothe_daemon/persistence/loop_input_store.py` (new) |
| Write-ahead wall + recovery | `packages/soothe-daemon/src/soothe_daemon/runtime/loop_dispatcher.py` |
| Startup recovery hook + `daemon_info` + seq flush | `packages/soothe-daemon/src/soothe_daemon/server/core.py` |
| `daemon_info` handler | `packages/soothe-daemon/src/soothe_daemon/protocol/router.py` |
| Seq hydrate / persist / flush | `packages/soothe-daemon/src/soothe_daemon/query/engine.py`, `server/handlers.py` |
| Auto-resume default + warnings | `packages/soothe/src/soothe/config/models.py`, `packages/soothe-daemon/src/soothe_daemon/runtime/auto_resume.py` |
| TUI epoch tracking + banner + requeue | `packages/soothe-cli/src/soothe_cli/runtime/transport/session.py`, `runtime/state/daemon_restart.py` (new), `tui/app/_app.py`, `tui/app/_execution.py`, `tui/app/_history.py` |
| Tests | `packages/soothe-daemon/tests/unit/persistence/test_loop_input_store.py` (new), `tests/unit/runtime/test_loop_dispatcher_durable.py` (new), `tests/unit/runtime/test_auto_resume.py`, `tests/unit/query/test_engine_seq_persistence.py` (new), `tests/unit/protocol/test_router_daemon_info.py` (new), `packages/soothe-cli/tests/unit/runtime/test_daemon_restart.py` (new) |

## Acceptance

- [x] Queued `loop_input` survives graceful restart and SIGKILL: rows re-enqueue
      in order on next start (`recover_pending_inputs` before auto-resume)
- [x] Worker pickup deletes the durable row; crash mid-turn loses only that
      in-flight message and the loop is auto-resumed
- [x] Oversized / non-serializable payloads degrade to memory-only with a
      warning, never block the enqueue path
- [x] `auto_resume_on_start` defaults to `true`; peek failures log warnings
- [x] Per-loop event seq survives restart (persist at turn end, hydrate before
      first frame, flush on stop)
- [x] `daemon_info` RPC returns a stable per-process `daemon_id`
- [x] TUI detects daemon restart via `daemon_id` change, shows a banner, and
      re-queues a connection-failed user turn locally (commands excluded)
- [x] `./scripts/verify_finally.sh` green
