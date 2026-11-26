# IG-LOOP-019: Execution Token Statistics Fixes (TUI / Daemon / Strange Loop)

**Created**: 2026-11-25
**Status**: Implemented
**Related**: IG-LOOP-016 (TUI token accounting), IG-761 (actual-first estimation), RFC-450 (wire protocol)

## Problem

Execution token statistics were wrong across all three layers, observed as a
2m26s loop showing "347 tokens" (exact, no "+"):

1. **Provider usage never reached the loop** — `ChatLitellmModel` never requested
   `stream_options={"include_usage": True}` for OpenAI-compatible endpoints,
   never read `chunk.usage`, and never attached `usage_metadata` to streamed
   chunks or non-streaming AIMessages. Every consumer therefore fell back to
   content-based estimates over the step's own streamed messages, which structurally
   exclude the system prompt and conversation context — two orders of magnitude
   undercount (the "347").
2. **Estimates rendered as exact** — wire events (`plan_phase_status`,
   `step_completed`, `completed`) carried `total_tokens_used` with no provenance,
   so estimated totals displayed without the "+" approximate marker.
3. **Checkpoint undercount** — `record_iteration` snapshots
   `goal.tokens_used` during `record_progress`, before the goal-completion
   synthesis call adds its tokens to `LoopState.total_tokens_used`; the finalize
   station skips re-recording on the terminal iteration, so
   `checkpoint.total_tokens_used` permanently misses every goal's final
   synthesis tokens (DB and wire disagree on every completed goal).
4. **ACP per-step tokens = cumulative loop total** — `step.tokens_used` was
   assigned the running total, so every settled step reported the whole-loop
   number.
5. **Terminal corrections swallowed** — the TUI's monotonic guard ignored any
   lower authoritative total, even at turn end.
6. **Plan-mode status events carried no totals** — `plan_synthesis_*` /
   `plan_refinement_*` mapped to label-only `StrangeLoopPlanPhaseStatusEvent`
   (no `total_tokens_used`), logging "plan_phase missing total_tokens_used"
   anomalies and freezing the counter during synthesis.

## Fixes

| Layer | File | Change |
| --- | --- | --- |
| soothe-nano | `llm/provider.py` | request `stream_options` for `openai/`-prefixed models (self-heal retry without it on rejection); capture per-chunk `usage`; emit terminal usage-only `AIMessageChunk`; attach `usage_metadata` on `_generate`/`_agenerate` |
| soothe-nano | `utils/token_usage.py` | actual-path sums dedupe by message id (last chunk of a generation wins — cumulative-usage providers no longer double-count) |
| soothe | `sloop/state/schemas.py` | `LoopState.token_usage_estimated` (sticky) + `token_usage_source()` |
| soothe | `sloop/engine/execute/executor.py` | `_aggregate_wave_metrics` sets the provenance flag on estimated waves |
| soothe | `phase_status.py` / `execute.py` / `finalize.py` ×3 / `plans/plan_mode_review.py` ×4 | all payloads gain `token_usage_source`; plan-mode events also carry `total_tokens_used` |
| soothe | `sloop/state/sloop_manager.py` | `update_goal_tokens()` refreshes a goal's snapshot (monotonic) |
| soothe | `stations/completion/finalize.py` | refresh goal tokens after synthesis, before the `completed` emit |
| soothe | `events/catalog.py`, `runner/_runner_strange_loop.py` | `token_usage_source` field on the three events; runner forwards it + plan-mode totals |
| soothe-daemon | `channels/acp.py` | `settle` reports per-step delta (`loop_total − prev`), clamped at 0 |
| soothe-cli | `tui/app/_ui.py`, `tui/textual_adapter.py` | `source="completed"` force-applies (corrections); `token_usage_source="estimated"` sets the sticky approximate marker |

## Semantics decisions

- **Provenance is sticky**: a total that mixes any estimated wave with real
  usage stays `estimated` — the display keeps the "+" marker until the loop
  scope resets. Honest about accuracy, not per-wave flip-flopping.
- **Terminal force-apply**: only `completed`-source merges bypass the monotonic
  guard; mid-turn merges stay monotonic (stale/out-of-order events exist).
- **Per-step ACP delta attribution** assigns tokens since the previous settle
  to whichever step settles next — approximate under parallel waves but never
  negative and never the whole-loop total.
- **Daemon coalescer merge untouched**: its sum-of-deltas semantics is pinned
  by `test_stream_delivery_token_usage.py` (current provider family emits
  usage once per generation); changing to id-dedup there would contradict the
  pinned contract. The nano-side dedup covers the accounting path.

## Verification

- nano: `tests/unit/llm/test_provider_stream_usage.py` (20) +
  `tests/unit/utils/test_token_usage.py`; llm+utils suites 420 passed; ruff
  check/format green.
- soothe: sloop `test_token_usage.py` (14), `core/loop/{core,plans,orchestrator,clarification}`
  (576), runner suite (349+), all green; ruff check/format green.
- soothe-daemon: `test_acp_channel.py` 171 passed.
- soothe-cli: tui/runtime token suites 170 passed.
- Follow-up (IG-SLP-014): synthesis stream usage now folds into
  `LoopState.total_tokens_used` before the terminal emit, so the goal token
  refresh persists a real delta instead of the pre-synthesis number.

Not done (deferred): periodic `plan_phase_status` tick during the finalize
synthesis (counter now updates via stream usage once real usage flows);
removing the dead TUI `aupdate_loop_state` persistence write.
