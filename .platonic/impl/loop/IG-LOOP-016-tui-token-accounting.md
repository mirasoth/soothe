# IG-LOOP-016: Instant, Correct, Complete TUI Token Accounting

**Created**: 2026-11-05
**Status**: Implemented
**Related**: IG-LOOP-004 (model-aware token estimation), RFC-450 (wire protocol)

## Problem

Token consumption on the Soothe TUI is wrong in three ways:

1. **Not instant** — the status bar is blanked for the whole turn and only shows a
   final number at turn end; usage-bearing stream chunks are `PRIORITY_LOW` and sit
   in the `soothe-client-python` 50 ms / 10-chunk batcher; the daemon coalescer
   defers `usage_metadata` to 100–200 ms flush windows (or turn end in `batch` mode).
2. **Not correct** — `tui/app/_ui.py` merged two measures with `max()`, wrote a
   backend cumulative total into the *output* slot, added it to a separately-seeded
   baseline (double count), refused to ever decrease, and recorded `total_only`
   usage as input tokens. Compaction overwrote the cumulative counter with a
   context-window size estimate.
3. **Not complete** — cache tokens are parsed nowhere; there is no turn-end total
   to reconcile against; usage-only frames are last-wins/dropped; orphan parallel-wave
   usage is discarded; the standalone clients do not parse `usage_metadata` at all.

Root causes span all three monorepo-owned packages, so the fix is end-to-end:
`soothe` (emits stats), `soothe-daemon` (websocket transport), `soothe-cli` (TUI).

## Goal / acceptance criteria

- **Instant**: the live counter rises within one usage chunk during streaming, in
  both the thinking row and the status bar; no turn-end gating.
- **Correct**: one monotonic cumulative total; backend per-goal totals are not
  mislabeled, not double-counted against the resume baseline, and not reset by
  compaction; `total_only` usage stays a total.
- **Complete**: input / output / cache-read / cache-creation / total are parsed and
  surfaced (step cards, `/context` modal, exit usage table); no usage chunk is
  dropped; a turn-end authoritative total is available.

## Data / event model decision

Keep two complementary channels, fix both, **add no new event type**:

1. **Authoritative per-goal cumulative total** — `total_tokens_used: int` on
   `plan.phase`, `step.completed`, and now `strange_loop.completed`. `LoopState` is
   created fresh per goal (`sloop/strange_loop.py`), so this value is *per-goal*,
   not session-cumulative; the CLI folds it into a session baseline once per goal.
2. **Per-call breakdown** — `token_usage: dict | None` with
   `{input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
   total_tokens}`, carried inside streamed `messages` frames' `usage_metadata`
   (LangChain-standard, already includes `input_token_details.cache_*`) and as an
   optional field on the three catalog events.

Cache tokens are a **subset of input** (`total = input + output`), never re-added.
Trust the provider's `total_tokens` when present.

**Why no dedicated per-LLM-call event**: it would require a host LangChain callback
with `ctx.emit` access (the callback lives in PyPI-only `soothe-nano`), a new event
constant in PyPI-only `soothe-client-python`, and a new golden case across five repos.
The streamed `usage_metadata` already *is* the per-call signal; enriching existing
events + fixing that path is sufficient.

## Binding constraints

- One-way DAG: `soothe-cli` imports only `soothe-client-python`; `soothe-daemon`
  imports `soothe`. `client/*` submodules and `soothe-nano` / `soothe-client-python`
  are PyPI-only and are **not** patched here. The 50 ms batcher is worked around via
  the CLI's own `prepare.py` priority, not by editing the dependency.
- `SootheEvent.to_dict()` is `model_dump(exclude_none=True)`, so the new optional
  `None` fields are omitted on the wire when unset. The `ba5a5a40…` golden fixture
  covers `soothe.wire` codec primitives only — **no golden regeneration**.
- No verbosity-tier changes: the three events are already `NORMAL`/`HIGH` and
  `soothe.cognition.*` progress-visible; `messages` mode is always visible.

## Changes by phase

### Phase 1 — `soothe` (server)

| File | Change |
|---|---|
| `sloop/utils/token_usage_breakdown.py` (new) | `extract_usage_breakdown`, `merge_usage_breakdown`, `TokenUsageAccumulator` — host-side cache-aware extractor, observational only (does not write `total_tokens_used`). |
| `sloop/engine/execute/context_window_manager.py` | Delete `state.total_tokens_used = new_tokens`; keep the `context_percentage_consumed` update. Cumulative consumption stays monotonic. |
| `events/catalog.py` | Add optional `token_usage` to the three events; add `total_tokens_used` to `StrangeLoopCompletedEvent`. |
| `runner/_runner_strange_loop.py` | Per-turn `TokenUsageAccumulator` folds usage from forwarded `stream_event` chunks (deduped by generation id); inject breakdown into the three catalog events; read turn-end total from the raw `completed` payload. |
| `sloop/stations/completion/finalize.py` | Thread `total_tokens_used` into all three `completed` emit payloads. |

`phase_status.py` / `execute.py` emit sites and `executor.py` accumulation are
unchanged — no third writer to the cumulative counter.

### Phase 2 — `soothe-daemon` (transport)

All in `query/stream_delivery.py`:
- `_merge_usage_metadata` sums streaming-chunk usage deltas (no more last-wins),
  including nested `input_token_details` / `output_token_details`.
- Ingest merges `usage_metadata` and `response_metadata.token_usage` into the buffer.
- `_flush_text_buffer` emits a usage-only frame when a buffer has usage but no text.
- Tool-invocation frames carrying usage are no longer dropped (usage preserved).
- Usage-only chunks already pass through instantly via the existing passthrough path;
  no text-coalescing change needed. No `query/engine.py` or visibility changes.

### Phase 3 — `soothe-cli` (TUI)

- **Instant** — `runtime/turn/prepare.py`: usage-only chunks and
  `STRANGE_LOOP_PLAN_PHASE` get `PRIORITY_HIGH` (bypass the dependency batcher).
  `tui/app/_ui.py` `_refresh_token_displays` updates the status bar live instead of
  hiding it; removed the `_on_tokens_hide` / `_hide_tokens` / `hide_tokens` surface.
- **Correct** — `tui/app/_ui.py`: replace the mislabeled output slot with a dedicated
  `_loop_goal_run_tokens`; keep the per-goal + baseline model with `max()` only
  between the two per-goal measures; add `_loop_turn_committed` so turn-end commit
  does not re-add the in-flight stream; `completed` event now reconciles the final
  total. `tui/app/_model.py` reads the ↑/↓ breakdown from `_inflight_turn_stats`.
- **Complete** — `runtime/token_usage.py`: `TokenUsage` value object (cache fields,
  `from_mapping`, `merge`, `has_any`). `runtime/state/session_stats.py`:
  `total_tokens` / cache fields on `ModelStats` / `SessionStats`; `record_request`
  takes `TokenUsage`. `tui/widgets/context_data.py`: cache columns in the `/context`
  modal. `tui/textual_adapter.py`: parse `TokenUsage`, always fold usage into turn
  stats (orphan chunks are never dropped), fix `total_only`, handle `completed`,
  render cache/total columns in the exit usage table. `runtime/token_events_debug.py`:
  `note_completed` counter.

## Backward compatibility

New catalog fields are optional and omitted when unset (`exclude_none=True`); old
clients ignore them. The wire golden fixture is untouched and verified still
`ba5a5a40…`. `LoopState.total_tokens_used` semantics are unchanged (still per-goal
cumulative); compaction no longer corrupts it.

## Risks / follow-ups

- **Cache math**: some LiteLLM/Anthropic paths may report cache tokens outside
  `input_tokens`/`total_tokens`. We trust the provider total and keep cache as a
  subset; verify against a live cache-heavy run, never double-add.
- **PyPI-only**: the dependency batcher and the nano extractor/callback cannot be
  edited here; the fix works around them. A future nano release could emit a
  dedicated per-call usage event.
- **Two accumulation writers** (executor wave-level + nano callback) remain; the
  host breakdown accumulator is observational and does not add a third writer.
- The catalog `token_usage` breakdown is published as an API/wire contract for other
  clients; the TUI's live breakdown comes from streamed `usage_metadata` to avoid
  double-counting a cumulative per-goal snapshot against per-call deltas.
- Standalone `client/{go,rust,typescript,python}` clients remain analysis-only and
  do not parse `usage_metadata`; aligning them is a separate effort in their repos.

## Verification

- `./scripts/verify_finally.sh` (ruff, vulture ≥90%, import-boundary check,
  per-package unit tests, changelog) — green.
- Full unit suites: `soothe` 3117, `soothe-daemon` 1365, `soothe-cli` 1392 passed.
- `tests/unit/wire/test_wire_golden.py` — sha still `ba5a5a40…` (no wire drift).
- New tests: host breakdown/extractor/accumulator, catalog event `exclude_none`,
  compaction monotonicity, coalescer accumulation/flush/tool-usage, CLI priority,
  `SessionStats` cache fields, `completed` reconciliation, `/context` cache format.
- Manual E2E against a cache-heavy model: counter rises within one chunk; no
  backwards jump / double count at step/phase/completion boundaries; `/context`
  shows cache read/write separately; orphan parallel-wave usage still counts;
  resume seeds baseline without double-counting.
