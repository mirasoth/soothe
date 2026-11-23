# IG-CLNT-004: Queued Goal Handoff Across HITL Turns

**Created**: 2026-11-23
**Status**: Implemented
**Related**: IG-CLNT-001 (goal completion card), RFC-622 (clarification answer wire)

## Problem

A goal submitted while another goal is running is held in the TUI-local
`_pending_messages` queue and shown by `QueuedGoalsBar`. When the running goal
pauses on a tool-approval (or any clarification) card, the daemon ends the turn
stream while the goal itself is still suspended on `await_clarification`. The
TUI treated that stream end as "goal finished" and consumed the queue:

- `_cleanup_agent_task()` either drained the queue head — which
  `execute_task_textual()` then sent with `clarification_answer=True`, turning
  the queued goal into an approval answer — or called
  `_attach_to_live_daemon_turn()`, which popped the local queue head, mounted a
  plain `UserMessage` echo and attached with `skip_daemon_send_turn=True`. The
  daemon never received that goal.
- In both cases the queue became empty, the bar disappeared, and the goal was
  never executed after the approval was answered.

The UI also had two competing representations of a queued goal: the docked
`QueuedGoalsBar` (no title, one faint top border) for normal goals, and an
in-transcript `QueuedUserMessage` card for a few slash commands. The latter was
pinned to the bottom of `#messages` by `_mount_before_queued()`, so a queued
item looked like a normal chat message with no separating border.

## Goal

1. A queued goal survives any HITL pause and starts exactly once, as a real
   `send_turn`, after the paused goal actually completes.
2. One UI representation: `QueuedGoalsBar`, with an explicit label and border.
3. No local queue head is consumed by attach-only paths.

## Design

### 1. Turn-end gate

`_queue_drain_blocked()` is true while an unsubmitted
`StructuredAskUserWidget` is mounted (`_has_active_qa_widget()`) or the adapter
has `_clarification_pending`. `_cleanup_agent_task()` returns before both the
attach branch and `_process_next_from_queue()` when blocked, so the queue and
the bar stay intact through the pause. The clarification-answer turn clears the
adapter flag in `execute_task_textual()` and marks the widget submitted in
`_handle_clarification_submitted()`, so the *next* cleanup (the real goal
completion) opens the gate.

`_process_next_from_queue()` repeats the gate as a defensive check because the
background event consumer (`_history.py`) can also reach cleanup.

### 2. Local goals always send a turn

`from_queue` flows through `_process_message()` → `_handle_user_message()` →
`_send_to_agent()` → `_run_agent_task()`, including the manual `Ctrl+Q`
`submit_queued_goal_at_index()` path. The automatic
`_daemon_loop_is_live()` → `skip_daemon_send_turn` conversion in
`_run_agent_task()` is skipped for `from_queue` turns and for turns carrying
`_clarification_pending`: a lagging daemon status must not turn a brand-new
local goal or a pending approval answer into an attach-only reader.

`_attach_to_live_daemon_turn()` no longer pops `_pending_messages`. It is now
only a reader for a daemon-owned successor turn and attaches with an empty
prompt. `_cleanup_agent_task()` enters that branch only when the adapter has an
explicit `_plan_approve_follow_on_pending` signal or when the local queue is
empty; a non-empty local queue always goes to `_process_next_from_queue()`.

`_run_agent_task()`'s `finally` also checks a `turn_generation` token so a stale
worker cannot clear the running state owned by a newer turn.

### 3. Single UI representation

`QueuedGoalsBar` renders a `Queued goals (N)` header, numbered rows and a full
`round` border (`ascii` in ASCII mode) that separates the queue from the
transcript and the thinking row. focus styling keeps the double border.

`QueuedUserMessage`, `_queued_widgets`, and `_mount_before_queued()` are
removed. `MessageType.QUEUED_USER` remains part of the shared transcript
protocol; on resume it maps to `UserMessage` because queue state is not
persisted. Slash commands that open a URL no longer mount an ephemeral
in-transcript card while busy — their deferred output is enough.

## Verification

- Unit: `tests/unit/tui/test_queued_goal_hitl_handoff.py` covers gate-closed
  cleanup, post-completion drain, blocked defensive drain, local queue with a
  live daemon loop, and stale-worker generation.
- Updated: `test_live_daemon_attach.py` (attach no longer consumes local goals,
  `from_queue` forces send), `test_queued_goals_bar.py` (header/border render).
- `./scripts/verify_finally.sh`: all checks passed (format, lint, vulture,
  full workspace test suite, wiki changelog sync).
- Widget mount test (`run_test()`) proves the full-border CSS parses and the
  labelled header renders in a live Textual app.
- Not run in this session: interactive daemon E2E, which needs a running daemon
  plus a live TUI. Repro: goal A triggers tool approval → queue goal B while the
  card is up → answer Disapproved → goal A completes → goal B starts once, with
  the labelled, bordered queue bar visible until then.
