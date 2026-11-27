# Loop Transcript Single Authority — Marker-Segmented Mutation Log

**Status**: Draft (implemented 2026-11-27; see "Implementation Status")
**Date**: 2026-11-27
**Origin**: Loop `01a21cb0` resume diagnosis (goal completion report absent from resumed transcript; transcript ends on the "Evaluate user-goal coverage" eval card)

## Problem

Resuming a completed loop loses the goal completion report. The resumed
transcript ends on the eval step card; the final assistant report ("I deployed
and verified…") is missing or sits mid-transcript. Root cause is a role
inversion, not a missing patch:

- **Resume reads only the display card store** — frozen goal snapshots plus the
  live card tail (`packages/soothe-cli/src/soothe_cli/tui/app/_history.py:105-123`,
  daemon side `loop_card_manager.fetch_loop_history`). The authoritative
  transcripts that *do* persist the report — the CE ledger `goal_completion`
  pair (`sloop/stations/completion/finalize.py:60-102`) and the ThreadLogger
  `phase=goal_completion` conversation row — are never consulted on resume.
- **The display store is a best-effort derived cache with three races**:
  1. `freeze_goal_display` snapshots the ledger without draining the background
     card-ingest queue (bounded queue + 200 ms debounce,
     `loop_card_manager.py:234-343`); the report's final chunks can still be in
     flight. It then destroys the live tail via
     `ledger.reset_for_next_goal()` (`loop_card_manager.py:654`,
     `loop_card_ledger.py:126-133`), so the report is permanently erased.
     The fallback `_peek_goal_completion_from_ledger` (engine.py:49-55) reads
     the same un-flushed ledger and also misses.
  2. `snapshot_collapser._ensure_assistant_card`
     (`soothe/display/snapshot_collapser.py:45-58`) early-returns if *any*
     non-empty ASSISTANT card exists in the segment, so a truncated streaming
     card suppresses the full report and stays pinned mid-transcript
     (creation-order ledger + `_last_card_per_id` first-occurrence pinning).
  3. Freeze keeps only `split_cards_by_user_segments(live_cards)[-1]`
     (`loop_card_manager.py:632-633`); follow-on/approval goals (which produce
     no summary by design) can own the last segment, ending it on step cards.
- **Freeze failures are swallowed** (engine.py:1829-1834) and the CE ledger
  pair is written fire-and-forget after the `completed` wire event, drained
  only at close (finalize.py:858-917) — a daemon stop loses it.
- **Live vs replay asymmetry**: live, the report is streamed and mounted by the
  stream path which then skips the ledger duplicate
  (`tui/app/_messages_mixin.py:293-309`, `_goal_completion_mounted_this_turn`).
  Replay has no stream, so it depends entirely on the lossy store above.
- The eval card ("Evaluate user-goal coverage") is bound earlier and through
  the reliable event path (root_eval inserts the step before synthesis,
  `sloop/stations/decompose/root_eval.py:663-677`), which is why it — not the
  report — survives as the last card.
- The same bug class was already fixed once for ThreadLogger
  (engine.py:1764-1775, "resume rendering loses the final answer"); the card
  store path is the remaining instance.

User decision: **no backward compatibility — replace the mechanism with a
clean design.**

## Design Decisions

| Decision | Choice |
|---|---|
| Source of truth | The **durable card mutation log** (already persisted per `(loop_id, seq)` in display.db SQLite/PG, append-only, idempotent on replay) |
| Goal segmentation | **Goal boundary markers** appended into the same log — no separate snapshot table, no log reset |
| Report persistence | **Synchronous marker write** at turn terminal, carrying the full goal completion text; never derived from the display ledger |
| Read model | **Fold-on-read**: replay log → fold → split on markers → read-repair terminal report card from marker text |
| Snapshots / freeze machinery | **Deleted** (goal snapshot table, `freeze_goal_display`, `reset_for_next_goal`, write-time collapser repair) |
| CE ledger pair / ThreadLogger | Keep their engine/observability roles; **retire their display-resume responsibilities** (no cross-store fallback chains) |
| TUI live/replay dedup | **Stable card ids only** — the stream-mounted report widget adopts the binder's card id; content-sniffing hacks deleted |
| Migration | Clean cutover; legacy snapshot rows dropped; historical loops replay as marker-less single-segment transcripts |

## Architecture

```
                    WRITE (unchanged ingest pipeline)
runner stream ──> card ingest worker ──> LoopCardLedger.append ──> mutations (display.db)
   (queue + 200ms debounce — no longer correctness-critical)

                    WRITE (new, synchronous)
engine turn terminal ──> LoopCardLedger.append(goal_boundary marker)
                          {goal_id, goal_text, status, goal_completion_text, ts}
                          ── before stream.end / idle / any cleanup

                    READ (new, single path for resume AND live catch-up)
fetch_loop_history ──> replay mutations ──> fold_display_cards
                          ──> split on goal_boundary markers
                          ──> per marker: ensure terminal report card (read-repair)
                          ──> {goals[], live_cards, live_goal_index}
```

Invariants:

1. The report is a deliverable: its text is persisted synchronously at turn
   terminal, before idle, and never inferred from a derived store.
2. One source of truth. Display cards, snapshots, TUI transcript are
   projections; projections may lag or be rebuilt at any time.
3. Append-only. Goal boundaries are markers, never deletes.
4. Live and replay render through the same fold; ordering is marker order +
   mutation seq, not "who happened to bind first".

## Write Path

### Goal boundary marker

New mutation op in `soothe/display/card_ledger.py`:

```
CardMutation(
    seq=<assigned>,
    ts=utc_now_iso(),
    op="goal_boundary",
    card_id=f"__goal__:{goal_id}",
    kind="goal_boundary",
    data={
        "goal_id": str,
        "goal_text": str,
        "status": "completed" | "cancelled",
        "goal_completion_text": str,     # full report body; "" for cancelled
        "started_at": str, "completed_at": str,
        "duration_ms": int, "tokens_used": int,
    },
)
```

`InMemoryCardLedger.apply` treats `goal_boundary` as a no-op for the card
index but records the marker in a `boundaries: list[GoalBoundary]` field
exposed by `snapshot()`-adjacent accessors.

### Commit point and ordering

`engine.py` replaces the freeze block (currently 1807-1834) with:

```
goal_completion_text = "".join(goal_completion_response).strip()  # stream truth
await card_manager.commit_goal_boundary(loop_id, goal_text=..., goal_completion=...)
# then stream.end / idle / turn cleanup
```

- The text source is the engine's accumulated `goal_completion_response`
  chunks — the same bytes the client saw streamed. It never comes from the
  display ledger, so ingest lag is irrelevant to durability.
- The write is a single `LoopCardLedger.append` (persist-before-apply,
  `loop_card_ledger.py:93-101`, already synchronous via `asyncio.to_thread`).
  Cancelled turns write `status="cancelled"` with empty text.
- `commit_goal_boundary` is **awaited**; failure propagates as a warning-level
  degraded marker (empty completion text) rather than a swallowed exception —
  a missing report on resume then has a single, honest place to look.
- `reset_for_next_goal` is never called again. The live segment continues
  accumulating; the next marker is the boundary.

### Ingest pipeline

Unchanged: bounded queue, 200 ms debounce, overflow deque
(`loop_card_manager.py:234-343`). These become pure latency concerns —
worst case, the tail cards appear on resume a moment later than the marker,
and read-repair handles the ordering (below).

### Retired writers

- CE ledger `goal_completion` pair (`finalize.py:60-102`): remains for engine
  context reconstruction only; the daemon display path no longer depends on
  it and nothing reads it on resume.
- ThreadLogger goal-completion row: observability only. The engine's explicit
  tail flush (engine.py:1764-1782) stays — it serves the ThreadLogger's own
  consumers, not resume.
- `_peek_goal_completion_from_ledger` (engine.py:49-55): deleted (it existed
  only to patch the freeze race).

## Read Path

`fetch_loop_history` (loop_card_manager) becomes:

1. `ledger = await self.ensure_for_loop(loop_id)` — hot path uses the
   in-memory folded ledger (already loaded at `ensure_loaded`,
   `loop_card_ledger.py:62-91`); cold path replays the mutation log once,
   same cost as today's startup load.
2. `cards = fold_display_cards(ledger.snapshot())` (unchanged fold helpers:
   `_last_card_per_id` + `merge_consecutive_assistant_cards`).
3. Split `cards` on `ledger.boundaries` → `goals: list[GoalSegment]`, live
   tail after the last marker. `live_goal_index = len(boundaries)` — no
   `loop_status` sniffing; marker `status` fields carry it.
4. **Read-repair (per completed goal)**: if the segment's last card is not an
   ASSISTANT card whose `loop_output_phase == "goal_completion"` and whose
   content equals `marker.goal_completion_text`, append a synthesized terminal
   report card from the marker (stable id `asst:goal_completion:#n` within the
   segment). If a truncated report card exists mid-segment, it is replaced by
   the marker text, not merely supplemented. Precedent:
   `reconcile_step_card_groups` (display/step_card_reconcile.py:218) already
   repairs step phases at fetch time — read-repair is an established pattern
   here.
5. Return `{goals, live_cards, live_goal_index}` in the same wire shape the
   TUI consumes today (wire compatibility is retained at the protocol level;
   only the producing mechanism changes).

### Compaction (phase 2, optional)

Replaying a full mutation log is bounded by loop length (mutations are
per-chunk). The hot-path in-memory ledger makes this free while the daemon
lives; cold starts pay one replay. If profiling shows a problem, add a
`op="compaction"` marker whose data is the folded card list for `seq <= N`:
replay applies it as a prefix substitute. It is strictly a cache — the log is
never truncated, compaction rows can be ignored, and `delete_loop` removes
both. Not part of the initial cutover.

## What Gets Deleted (dead code rule)

| Component | Disposition |
|---|---|
| `goal_snapshots` table + `insert_goal_snapshot_with_auto_index` / `list_goal_snapshots` / `goal_snapshot_count` (display_store*.py) | Delete table + API |
| `freeze_goal_display` (loop_card_manager.py:608-669) | Replaced by `commit_goal_boundary` |
| `reset_for_next_goal` (loop_card_ledger.py:126-133) | Delete (no caller) |
| `build_goal_snapshot` / `_ensure_assistant_card` / `_ensure_user_card` / `split_cards_by_user_segments` freeze use (snapshot_collapser.py) | Delete write-time repair; fold helpers used by the read path remain |
| `_peek_goal_completion_from_ledger` (engine.py:49-55) | Delete |
| `GoalDisplaySnapshot` wire type | Read path returns the same wire shape; type moves from "frozen store record" to "folded segment projection" |
| TUI `_goal_completion_mounted_this_turn` / `_assistant_card_already_visible` content-sniffing (messages_mixin.py:293-309) | Replace with id-based dedup: the stream-mounted report widget is registered under the binder's stable card id (`asst:goal_completion:#n`), so `_find_existing_card_widget` (messages_mixin.py:283-289) already covers the duplicate |
| CLI `sanitize_resume_display_cards` | Keep (drops TOOL cards — orthogonal UI policy) |

## Migration (clean cutover)

- Display DB: `DROP TABLE goal_snapshots` (or new schema version; both SQLite
  and PG backends in display_store*.py). Mutation table unchanged —
  `goal_boundary` is just a new `op` value; `(loop_id, seq)` uniqueness already
  absorbs idempotent re-appends.
- Existing loops: their mutation logs were already reset at each old-style
  freeze, so pre-cutover transcripts replay as single-segment marker-less
  transcripts (what the old live tail already showed). Historical report
  recovery is explicitly out of scope per the no-compat decision.
- Deploy: daemon restart picks up new code; no data migration job.

## Failure Modes

| Failure | Behavior under new design |
|---|---|
| Report chunks never bound (ingest lag / overflow) at turn terminal | Marker still carries full text; read-repair synthesizes the report card. Ingest worker eventually binds a duplicate stream card → folded away by stable id |
| Daemon killed between report stream and marker write | Live client already saw the stream; resume shows the goal as running-tail (no marker) — honest, visible, and the marker ordering (write before stream.end/idle) keeps the window to the turn-terminal instant |
| `commit_goal_boundary` store failure | Logged warning + degraded marker; resume read-repair falls back to whatever cards exist. Single place to alert on |
| Marker written but ingest worker still busy | Read path folds by seq; marker lands after the tail cards it terminates — correct by construction |
| Two goals, second one cancelled | Two markers; second segment `status="cancelled"`, no read-repair, no report card synthesized |

## Test Plan

1. **Resume report invariant** (regression for the reported bug): complete a
   goal → restart daemon → `fetch_loop_history` → the goal segment's last card
   is an ASSISTANT card with `loop_output_phase="goal_completion"`, content
   byte-equal to the streamed report.
2. **Ingest race injection**: delay the card ingest worker past
   `commit_goal_boundary` (queue stall) → assertion 1 still holds.
3. **Marker ordering**: `commit_goal_boundary` is awaited before
   `_ensure_turn_stream_end` (ordering assertion on a fake clock).
4. **Live/replay golden test**: same loop driven live vs killed-and-resumed;
   folded card sequence (ids + terminal contents) equivalent.
5. **TUI**: stream-mounted report widget and replayed ledger card share one
   stable id — mount once, no duplicate render, no content-sniffing branch.
6. **Cancelled turn**: marker with empty completion; no synthesized report
   card; segment ends on last real card.
7. Update `test_resume_loop_history_ordering.py` — it currently mocks
   `_load_loop_history` entirely; extend it to assert which cards the history
   fetch returns (report present, position last).

## Implementation Slices

1. `goal_boundary` mutation + ledger apply/accessors (`card_ledger.py`,
   `loop_card_ledger.py`) + unit tests.
2. `commit_goal_boundary` in loop_card_manager; engine.py replaces the freeze
   block; delete `_peek_goal_completion_from_ledger`.
3. Read path: marker-split fold + read-repair in `fetch_loop_history`; delete
   snapshot store API + table.
4. TUI stable-id dedup; delete content-sniffing hacks.
5. Test plan items 1-7; `./scripts/verify_finally.sh` gate.

Slices 1-2 alone fix the reported bug (marker carries the report text even
before the read path stops trusting snapshots — the snapshot table can be
deleted only after slice 3, so slices 2 and 3 ship together in one release;
slice 4 is UI polish and can trail).

## Implementation Status (2026-11-27)

Slices 1–3 landed; slice 4 (TUI stable-id dedup) deferred as planned.

- `goal_boundary` mutation: `CardOp` extension + `build_goal_boundary_mutation`
  in `soothe/display/card_ledger.py`; `InMemoryCardLedger.apply` treats a
  boundary as a segment terminator (fold resets cards, boundary records
  accumulate), so folding a full log yields the live segment.
- Daemon ledger (`loop_card_ledger.py`) rewritten around the full persisted
  log; `replace_with` preserves the archived prefix (mutations up to and
  including the last boundary) verbatim and re-seqs only live-segment items.
  `reset_for_next_goal` deleted. New `history_segments()` folds the log at
  mutation level into per-boundary segments plus the live tail.
- Manager (`loop_card_manager.py`): `freeze_goal_display` →
  `commit_goal_boundary` (bounded ingest quiesce that cancels the debounced
  flush and binds the tail immediately, then one synchronous
  persist-before-apply marker append); `fetch_loop_history` folds segments
  and read-repairs each completed goal's terminal report card from the
  marker text (truncated streaming report cards are replaced). Wire
  response shape unchanged.
- Engine: turn-terminal block commits the marker with the accumulated
  `goal_completion_response` text (cancelled turns carry none);
  `_peek_goal_completion_from_ledger` deleted.
- Store: `goal_display_snapshots` table and its API removed from both
  SQLite and PostgreSQL backends; the schema script drops the table (clean
  cutover, no migration job).
- Tests: host `tests/unit/wire/test_card_ledger_goal_boundary.py` (builder,
  fold segmentation, cross-segment id independence, card_id validation);
  daemon `test_loop_card_manager.py` rewritten freeze tests → boundary
  commit, cold-reload replay, multi-goal segmentation, ingest-race
  (drain disabled), cancelled-goal replay; `test_step_card_reconcile.py`
  archived-goal case reseeded via boundary commit; `test_engine_cancel.py`
  mocks `commit_goal_boundary`; `test_goal_snapshot_auto_index.py` deleted.

### Deviations from the draft

1. **Mutation-level segmentation, not folded-card counts.** The draft's
   read step split a *folded* card list by marker counts; implementation
   splits the *mutation* stream and folds each segment independently.
   Reason: stable binder card ids repeat across goals (per-segment
   ordinals), so a single fold over the whole log would collapse goal 2's
   card onto goal 1's slot. Mutation-level splitting makes each segment
   fold self-contained.
2. **`replace_with` is segment-scoped, not full-log.** The flush path
   rewrites the card projection on structural changes; the implementation
   rewrites only the live segment's mutations (archived prefix verbatim)
   instead of the whole log, which is what keeps the log append-only in
   effect.
3. **Report invariant source.** The engine passes the accumulated stream
   text (as designed) but with zero-valued timing/token metadata — the old
   freeze also defaulted these; wiring real values is engine bookkeeping
   left for a follow-up.

