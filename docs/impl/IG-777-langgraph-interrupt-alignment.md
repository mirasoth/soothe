# IG-777: LangGraph Interrupt Alignment (typed payloads, checkpoint SSOT, batch resume)

**Created**: 2026-09-18
**Status**: Implemented on `feat/opt-interrupt`
**Related**: IG-775 (loop relay), IG-765 (unified ask_user/interrupt_on relay paths), IG-774 (loop-scoped approval allowlist)

## Problem

The relay interrupt pipeline diverges from LangGraph's `__interrupt__` design in four ways:

1. **Classification is shape-sniffed in three places.** `outbox.is_ask_user_interrupt` /
   `is_tool_approval_interrupt` (payload-key predicates), `ClarificationDetector.detect`
   (its own key dispatch), and the executor's residual-interrupt branch all encode the
   same "which payload shape is this" decision independently.
2. **Three representations of "pending interrupt".** The CoreAgent checkpoint's pending
   interrupts, the in-memory `RelayInbox`, and the `relay_state` channel projection can
   drift (worker crash, resolved-elsewhere thread) with only a head-only
   `_check_stale_head` guard. The LangGraph checkpoint is the canonical source; the
   inbox is never reconciled against it.
3. **Head-only resume.** Only the FIFO head is answered per turn; same-thread entries
   (one `GraphInterrupt` may carry multiple `Interrupt` objects) each cost a full
   park → answer → resume round trip. LangGraph's resume-map semantics
   (`Command(resume={id: value, ...})`) resumes every pending interrupt of a thread in
   one invocation — the relay already emits id-keyed payloads but only ever one.
4. **No ops/debug pausing.** LangGraph static breakpoints (`interrupt_before` /
   `interrupt_after` compile flags) are unused; debugging a running loop has no
   step-through.

## Goal

Adopt the mature parts of the LangGraph design without losing soothe-specific
machinery (FIFO fairness, origin taxonomy, orphan recovery):

- One typed classification owner for interrupt payloads.
- The CoreAgent checkpoint is the source of truth: the inbox is reconciled against it
  on hydrate; stale entries are dropped, lost captures are alerted.
- Same-thread entries are resumed with one merged resume-map `Command` when the relay
  holds answers for a consecutive prefix of them.
- Config-gated static breakpoints on the StrangeLoop graph, with parked-turn resume.
- A guard test keeps `GraphInterrupt` from being swallowed by broad `except` handlers
  on the stream path, and `Command(update=..., goto=...)` stays confined to orphan
  recovery.

## Design

### 1. Typed interrupt kinds (`clarification/interrupt_kinds.py`)

```python
class InterruptKind(StrEnum):
    ASK_USER = "ask_user"          # soothe `ask_user` tool: {"type": "ask_user", ...}
    TOOL_APPROVAL = "tool_approval"  # deepagents HITL: {"action_requests": [...]}
    CLARIFICATION = "clarification"  # StrangeLoop InteractiveClarificationPolicy pause
    OTHER = "other"

def classify_interrupt_payload(value) -> InterruptKind | None
```

Single owner of payload → kind. Soothe-owned emitters already tag `type`
(`ask_user` tool, `interactive.py`); the deepagents `action_requests` key is an
external wire contract (PyPI-owned `soothe-deepagents`) — classified as a documented
structural key, the one allowed non-`type` rule. `ClarificationDetector.detect`,
`outbox.build_auto_resume_payload`, and the executor's fetch branch switch to
`classify_interrupt_payload`; the `is_*` predicates are deleted (all call sites
updated, tests included).

### 2. Checkpoint reconciliation (`relay/reconcile.py`)

```python
async def reconcile_inbox_with_checkpoints(core_agent, inbox, *, loop_id, emit) -> int
```

For each inbox entry with a `resume_ticket.thread_id`: read the CoreAgent thread's
pending interrupts via `aget_state`; drop the entry when its `origin_interrupt_id` is
no longer pending (thread advanced / interrupt resolved elsewhere). When a thread's
pending set contains a clarification-kind interrupt id absent from the inbox, log an
alert (capture lost — no auto-recovery). Planner-ask entries (no thread) are skipped.

Called from `node_execute` after `hydrate_from_channels`, materializing a lazy
CoreAgent first when the inbox has thread-bearing entries. Emits `RELAY_RECONCILED`
(catalog-registered) when anything was dropped or alerted. `runner` stays unchanged —
`node_execute` is the single reconcile point on the hydrate boundary.

### 3. Batch resume (resume-map semantics)

Answer slot becomes a list. `relay_state.answers` holds
`[{"interrupt_id": iid, "answer": answer_state}]`. No legacy single-`answer`
read/write path remains: a pre-upgrade parked loop resumes through the
runner's clarification-resume or orphan-goto path, both of which re-record the
answer in the new form before the origin node consumes it, so a stale legacy
key in an old checkpoint is inert.

- `InteractiveClarificationPolicy.try_static_answer(request)` — public wrapper over
  the tool-approval pipeline pre-filter (allow/deny stages; `escalate`/None → not
  statically resolvable). No LLM, no `interrupt()` — safe to call repeatedly inside
  one node invocation.
- `node_await_clarification`: after the head is answered, resolve the maximal
  consecutive same-thread follower prefix via `try_static_answer` and record all
  answers with `relay.record_answers`. Unresolvable follower → stop; it stays queued
  for its own turn (FIFO fairness preserved).
- `LoopRelay.record_answers(pairs)` replaces `record_answer`;
  `consume_answer_batch(relay_state)` replaces `consume_answer`: dequeues the head
  plus the consecutive same-thread prefix that has recorded answers, returns the
  `(request, answer, ticket)` triples.
- `node_execute` merges per-entry `build_clarification_resume_payload` dicts into one
  resume map → a single `Command(resume=merged)` on the head's thread (LangGraph
  matches resume values by interrupt id; unanswered interrupts of the same thread
  re-raise on replay and re-enter the relay).
- Predicates updated to the answers list: `routing._pending_clarification`,
  `routing._has_relay_answer`, `relay.snapshot.snapshot_has_unanswered_pending`.

Per-entry side effects (allowlist recording, ask_user ledger messages) apply to every
consumed entry.

### 4. Static breakpoints (config-gated)

- `LoopDebugConfig` on `agent.loop.debug`: `interrupt_before: list[str]`,
  `interrupt_after: list[str]` (station names). Default empty — zero behavior change.
- `build_strange_loop_graph` validates names against the graph's nodes (unknown →
  warning + drop) and passes them to `compile(checkpointer=..., interrupt_before=...,
  interrupt_after=...)` only when a real checkpointer exists.
- `invoke_strange_loop_graph`:
  - Resume side: when breakpoints are configured and the persisted snapshot has
    `next` non-empty, no interrupts, and no unanswered relay pending → the prior turn
    parked at a breakpoint → invoke with `None` (LangGraph breakpoint resume) instead
    of a fresh `{"last_outcome": None}` input.
  - Park side: after `ainvoke`, the same condition means the turn stopped at a
    breakpoint → emit `LOOP_BREAKPOINT_PAUSED` (catalog-registered) with the pending
    nodes and return; the operator's next turn resumes via the resume side.

### 5. `GraphInterrupt` swallowing guard

`tests/unit/core/loop/relay/test_graph_interrupt_guard.py`:

- **Inventory test**: AST-scans the stream-path modules (`engine/execute/executor.py`,
  `engine/execute/graph_interrupt.py`, `relay/{relay,outbox,inbox,channel,snapshot,
  _adapter}.py`, `stations/execute/execute.py`, `stations/sidecars/await_user.py`,
  `clarification/{interactive,detector}.py`) for `except Exception` / bare handlers
  that neither re-raise nor are on the explicit allowlist. A new broad except on the
  stream path fails CI until consciously allowlisted — the LangChain "never wrap
  `interrupt()` in bare try/except" rule made enforceable.
- **Behavioral tests**: `GraphStreamChunkReader.read_next` propagates a
  `GraphInterrupt` raised by the underlying iterator; the executor capture path
  surfaces (not swallows) interrupts raised after heartbeat sentinels.

### 6. `Command(update, goto)` confinement

`relay/_adapter.py` docstrings already state the boundary; add the explicit
"normal turns use `Command(resume=...)` or a plain input dict only" contract line and
a source-scan test asserting `build_orphan_goto_command` has no references outside
`relay/relay.py` (non-test), and `build_live_interrupt_resume_command` builds a
resume-only `Command`.

## Files

- `packages/soothe/src/soothe/sloop/clarification/interrupt_kinds.py` — new.
- `packages/soothe/src/soothe/sloop/clarification/detector.py` — classify dispatch.
- `packages/soothe/src/soothe/sloop/clarification/interactive.py` — `try_static_answer`.
- `packages/soothe/src/soothe/sloop/relay/outbox.py` — drop `is_*`, classify.
- `packages/soothe/src/soothe/sloop/relay/reconcile.py` — new.
- `packages/soothe/src/soothe/sloop/relay/relay.py` — `record_answers` /
  `consume_answer_batch`.
- `packages/soothe/src/soothe/sloop/relay/channel.py` — answers-list projection.
- `packages/soothe/src/soothe/sloop/relay/snapshot.py`, `relay/events.py` — predicates + event re-export.
- `packages/soothe/src/soothe/sloop/relay/_adapter.py` — boundary docstring.
- `packages/soothe/src/soothe/sloop/orchestrator/routing.py` — answers predicates.
- `packages/soothe/src/soothe/sloop/orchestrator/builder.py` — breakpoint compile flags.
- `packages/soothe/src/soothe/sloop/orchestrator/runner.py` — breakpoint park/resume.
- `packages/soothe/src/soothe/sloop/stations/execute/execute.py` — reconcile + batch consume.
- `packages/soothe/src/soothe/sloop/stations/sidecars/await_user.py` — follower static answers.
- `packages/soothe/src/soothe/sloop/engine/execute/executor.py` — classify usage.
- `packages/soothe/src/soothe/config/models.py` — `LoopDebugConfig`.
- `packages/soothe/src/soothe/events/catalog.py` — `RELAY_RECONCILED`,
  `LOOP_BREAKPOINT_PAUSED` + models + `_reg`.
- Tests under `packages/soothe/tests/unit/core/loop/` (relay, clarification, orchestrator, engine).

## Cleanse (post-impl pass)

- Legacy single-`answer` relay_state path fully removed: `recorded_answers` reads
  only the `answers` list, the orphan-goto no longer pops a legacy key,
  `_relay_state_answers_cleared` sets `answers: []` only, and every test fixture
  uses the record form.
- `interrupt_kinds.is_clarification_interrupt_payload()` replaces the
  kind-tuple check duplicated across executor capture, executor fetch, and
  reconcile.
- The type-discriminator constants (`INTERRUPT_TYPE_ASK_USER`,
  `INTERRUPT_TYPE_CLARIFICATION`) are now the single source of the `type`
  strings: the `ask_user` tool, `InteractiveClarificationPolicy`, and
  `ClarificationDetector.from_interrupt` import them instead of hardcoding.

## Verification

`./scripts/verify_finally.sh` — zero lint, all tests green. New tests:
classification, reconcile drop/keep/alert, batch record/consume prefix + merged map,
await_user static follower batching, breakpoint compile flags + runner park/resume
detection, GraphInterrupt guard inventory + behavioral, adapter confinement scan.
