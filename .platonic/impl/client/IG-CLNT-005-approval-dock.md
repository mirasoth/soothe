# Implementation Guide: Approval Dock — Pinned Multi-Approval UI

> Implementation guide for the docked tool-approval UI (`ApprovalDockBar`) in the soothe CLI TUI.
>
> **Crate/Module**: `soothe_cli/tui/widgets/approval_dock.py` (+ `tui/app/_app.py`, `tui/app/_ui.py`, `tui/app/_execution.py`, `tui/textual_adapter.py`, `tui/widgets/messages/structured_ask_user.py`)
> **Related IGs**: IG-CLNT-002 (structured ask-user widget), IG-CLNT-003 (unify ask-user and HITL widget), IG-CLNT-004 (queued goal handoff)
> **Language**: Python 3.11+ (Textual; no new dependencies)
> **Repos**: soothe monorepo, `packages/soothe-cli`

---

## 1. Overview

### 1.1 Purpose

Today approval cards (`StructuredAskUserWidget`) mount inline into the `#messages`
transcript. This has three problems:

1. Cards scroll away with the message flow; a pending approval can be lost in scrollback.
2. Multiple simultaneous approvals stack inline and are hard to distinguish.
3. A 200 ms focus guard continuously steals focus from the chat input, so the user
   cannot type queued goals while an approval pends.

This IG replaces the inline mount with a **docked approval bar** pinned at the bottom
of the message area, directly above the queued-goals bar:

- Multiple pending approvals queue **head-first** (matches the relay inbox FIFO
  pairing; zero host/daemon changes).
- The dock is never flushed by new messages (it is docked, not streamed).
- On a decision, the submitted card **moves into the transcript** as the record
  (same rendering path as reconnect hydration), and the next queued card is
  promoted to interactive **at turn end** (one answer in flight at a time).
- Focus never auto-steals; `Ctrl+R` jumps into the dock (mirrors `Ctrl+Q` for the
  queue bar), so queued goals stay typable while approvals pend.
  **Exception (2026-11-25)**: in manual composer mode, a tool-approval head
  card takes keyboard focus when it lands (and on promote) — the operator is
  the sole decision maker there. Clicking the chat input or Tab returns
  focus; auto/bypass/plan/ask and non-tool-approval cards never steal focus.
  Cursor reactivation at turn/shell teardown (`set_cursor_active(True)`) also
  honors the pending card: cursor reactivation is a weakest-authority
  CHAT_INPUT request through the focus coordinator, which the card's active
  protected-region claim drops (see §3.6; the former
  `_manual_tool_approval_pending` guard helper was superseded and removed).

### 1.2 Decisions (user-approved)

| Decision | Choice |
|---|---|
| Ordering of multiple approvals | Head-first in the UI; queued cards render as waiting rows. No wire changes. |
| Focus while pending | Keybinding entry (`Ctrl+R` / click); no focus steal, ever. |
| Transcript record | The submitted card itself relocates into `#messages` (answered state renders per-tool history rows, identical to reconnect hydration). |

### 1.3 Out of Scope

- Out-of-order answering via `origin_interrupt_id` on the wire (rejected; would
  touch daemon + host relay pairing).
- Parallel-loop sessions (CLI is bound to a single session loop).
- Auto-mode policy changes (auto clarifications never mount UI cards — unchanged).

---

## 2. Architecture

```text
#bottom-app-container (dock: bottom)
 ├─ #approval-dock          NEW — first child, above queued goals
 │   ├─ header: "⏸ Tool approvals (N pending)"
 │   ├─ head card: StructuredAskUserWidget (interactive)
 │   └─ waiting rows: one line each ("· write_file — waiting (answer #1 first)")
 ├─ #queued-goals           unchanged (QueuedGoalsBar)
 ├─ #thinking-status        unchanged
 └─ #input-area             unchanged
```

### 2.1 Data flow

```text
LOOP_CLARIFICATION_REQUESTED
  └─ _mount_manual_clarification_input
       ├─ dedup (origin + step, unchanged)
       └─ dock.push_card(widget)            (was: adapter._mount_message)
            ├─ first card → head (mounted, interactive)
            └─ others  → waiting rows

user decides (head card only)
  └─ StructuredAskUserWidget.Submitted
       └─ _handle_clarification_submitted
            ├─ step-card set_clarification_details (unchanged)
            ├─ duplicate disarm (unchanged)
            ├─ dock.pop_card(widget) → adapter._mount_message(widget)   ← record enters transcript
            ├─ single-slot answer fields (unchanged) → _send_to_agent
            └─ focus_primary_input (unchanged)

turn ends
  └─ _cleanup_agent_task → dock.promote_next()   ← next waiting row becomes interactive
```

### 2.2 Why promote at turn end (not on submit)

The relay inbox is per-loop FIFO and pairs answers with the head request; the wire
carries no interrupt id. Promoting the next card only when the resume turn
completes guarantees at most one answer is in flight, so the existing single-slot
`_clarification_answers_pending` stays correct with zero adapter-queue changes.
The daemon re-emits the next `clarification_requested` during the resume turn;
dedup-by-step prevents a duplicate card whether the request arrives before or
after promotion.

### 2.3 Focus model

- The widget's on-mount auto-focus, 600 ms robust-focus pin, and 200 ms focus
  guard are **removed** (`_start_focus_guard` / `_stop_focus_guard` /
  `_schedule_robust_focus` / `_focus_guard_timer` / `_focus_drifted` become dead
  and are deleted — Rule 19). `_schedule_focus` stays (option/footer navigation).
- App binding `Ctrl+R → action_focus_approvals` mirrors `Ctrl+Q → focus_queued_goals`.
- After a decision, focus returns to the chat input (existing behavior).

---

## 3. Implementation

### 3.1 `widgets/approval_dock.py` (NEW)

`ApprovalDockBar(Vertical)`:

- `push_card(widget) -> bool` (async): dedup by card identity; first card mounts
  as head, others as waiting rows. Returns False for duplicates.
- `pop_card(widget)` (async): removes card/row, promotes next waiting card.
- `promote_next()` (async): promotes first waiting row to head (no-op otherwise).
- `pending_count` / `has_pending` / `head_widget`; `clear_pending()` for
  `/clear` and loop switch.
- `display` toggles off when empty; header shows pending count.

### 3.2 Adapter wiring

- `TextualUIAdapter._approval_dock` reference (set by the app after adapter
  creation; `None` → tests/fallback keep the old inline mount).
- `_mount_manual_clarification_input` pushes to the dock.
- `clear_live_session_ui` calls `dock.clear_pending()`.

### 3.3 App surfaces

- `compose()`: `ApprovalDockBar(id="approval-dock")` first child of
  `#bottom-app-container` (same non-stacking rationale as the queue bar).
- `BINDINGS`: `ctrl+r → focus_approvals` (priority, `show=False`).
- `_ui.py`: `action_focus_approvals` (no-op when dock empty or modal open).
- `_startup.py`: bind `adapter._approval_dock` after adapter creation.

### 3.4 Submit handler + turn-end promotion

- `_handle_clarification_submitted`: after marking the widget submitted,
  `dock.pop_card(widget)` then `adapter._mount_message(widget)` (before the
  `non_empty` guard so abandon also relocates; abandoned cards keep the
  no-send semantics and render as dismissed in the transcript).
- `_cleanup_agent_task`: `dock.promote_next()` after turn cleanup.

### 3.5 Widget focus-guard removal

Delete `_start_focus_guard`, `_stop_focus_guard`, `_schedule_robust_focus`,
`_focus_guard_timer`, `_focus_drifted` and their call sites (on-mount auto-focus
included). Keep `_schedule_focus` and `_refocus_self_no_scroll` (both still used
by in-card navigation).

### 3.6 Unified focus arbitration — `tui/focus.py` (2026-11-27)

Every automatic focus mover used to schedule its own delayed attempt (immediate
fire / `call_after_refresh` / retry timers), racing each other: the dock's
fire-once `_autofocus_head` could be silently beaten by the chat-input
teardown refocus that fired later, and fixed delays duplicated/conflicted with
plan-review and approval-option focus. Replaced by one app-level arbiter,
`FocusCoordinator` (instantiated in `SootheApp.__init__`):

- **Priorities** (`FocusPriority`, lower = stronger): `APPROVAL_HEAD` (manual
  tool-approval head card) > `CLARIFICATION_INPUT` (plan-review / ask_user
  answer fields) > `PRIMARY_INPUT` (lone non-chat input) > `CHAT_INPUT`
  (default convenience). Modal screens are outside the model.
- **Convergence, not fixed delays**: `request_focus(widget, priority)` focuses
  immediately, verifies ownership (target or subtree holds `app.focused`), and
  retries — 3 `call_after_refresh` attempts, then 50 ms timers — within a 1 s
  wall-clock deadline; expiry, target removal, modal open, or supersession all
  cancel cleanly. This is what fixes the manual approval card: a request that
  cannot land yet is never lost.
- **Protected regions**: the dock `claim_region(head, APPROVAL_HEAD)` on
  manual-mode tool-approval push/promote and `release_region` on pop/clear.
  While a stronger claim is active, weaker requests are dropped outright (not
  just delayed), so turn teardown cannot steal focus back; the claim releases
  on answer → `focus_primary_input` works again. User moves (click, Tab,
  Ctrl+R) bypass the coordinator and always win; `Ctrl+R` cancels pending
  requests first.
- **All movers wired**: `ChatTextArea.set_app_focus`/`ChatInput.request_input_focus`
  (CHAT_INPUT), `App._schedule_widget_focus`/`focus_primary_input`
  (CLARIFICATION_INPUT / PRIMARY_INPUT via `_focus_priority_for`),
  `StructuredAskUserWidget._schedule_focus` (CLARIFICATION_INPUT; legacy
  fallback kept for harnesses), `ApprovalDockBar._autofocus_head`
  (claim + request). The app-level `_manual_tool_approval_pending` helper was
  removed (Rule 19) — the claim owns that contract now.
- IG-CLNT-005 contract unchanged: cards never steal focus on mount outside the
  manual tool-approval exception; focus returns via answering/click/Tab/Ctrl+R.

---

## 4. Tests

- New `tests/unit/tui/test_approval_dock.py`: push/dedup/head-first, pop +
  promote, header count, clear_pending, no focus steal, `Ctrl+R` action,
  manual-mode head autofocus (incl. the chat-refocus race), cursor-reactivation
  contract via the coordinator claim.
- New `tests/unit/tui/test_focus_coordinator.py`: priority arbitration, pending
  replacement, claim block/release, convergence-after-focusable, cancel,
  modal drop, unmount self-heal, bounded deadline expiry.
- Rewrite focus-guard tests in `test_structured_ask_user.py`:
  guard/dead-method tests removed; `test_widget_has_focus_after_mount` becomes
  `test_widget_does_not_steal_focus_on_mount`.
- Existing forwarding tests (`_FakeAdapter` has no dock) exercise the fallback
  path unchanged.

## 5. Risks

- `sending_clarification_answer` spinner semantics are untouched (single-slot
  fields unchanged).
- Dock + queue + thinking row + input must fit `max-height: 100%` — waiting rows
  are one line each; the dock has no per-card growth (head card only).
- Focus-model change rewrites two tests; keyboard muscle memory gains `Ctrl+R`.
