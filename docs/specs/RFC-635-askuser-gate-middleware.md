# RFC-635: AskUserGateMiddleware — Inline Veritas Fast Path for ask_user

**RFC**: 635
**Title**: AskUserGateMiddleware — Inline Veritas Fast Path for `ask_user`
**Status**: Implemented
**Kind**: Implementation Interface Design
**Created**: 2026-09-20
**Last Updated**: 2026-09-20
**Authors**: Soothe Team
**Depends on**: RFC-622 (CoreAgent Clarification Relay), RFC-623 (Veritas Auto-Mode Robustness), RFC-634 (AutoModeMiddleware)
**Supersedes**: ---

---

## 1. Abstract

In auto clarification mode, every `ask_user` tool call costs a full durable
round trip: tool `interrupt()` → executor capture → `await_clarification`
station → `AutoClarificationPolicy._answer_veritas` LLM call → resume payload
→ `Command(resume=...)` — even when veritas answers confidently and no human
ever sees the question. RFC-635 introduces `AskUserGateMiddleware`, a host
`AgentMiddleware` that calls veritas inline in its `aafter_model` hook (before
the tool executes) and answers confident questions with a synthetic
`ToolMessage` — zero interrupts, zero graph hops. The gate only ever inlines
"I have an answer"; defer/failure questions fall through to the existing
station path with a `gate_deferred` marker so veritas never runs twice,
preserving every RFC-622/623 contract including the seven-day
`awaiting_clarification` park.

---

## 2. Design invariants

1. **只内联"有答案"，永不内联"没答案"** — the gate answers confident
   veritas results inline; defer/failure questions are stripped from the
   tool-call list and re-emitted as the gate's own `ask_user`-shaped
   interrupt carrying a `gate_deferred` marker.
2. **Park semantics untouched.** The seven-day `hard_defer` lives in the
   goal state machine (`ContextEngine.mark_awaiting_clarification`,
   `BLOCKED_STATES`, out-of-band `answer_clarification`, stale sweeper) —
   all outside the CoreAgent graph the gate runs in. The gate physically
   cannot break it.
3. **No double LLM.** The marker tells `AutoClarificationPolicy` to skip its
   veritas call and go straight to the fallback ladder (human when attached,
   autopilot retry sentinel, hard defer).
4. **Wire-shape compatibility.** The gate's interrupt payload matches the
   `ask_user` tool's (`{"type": "ask_user", "questions": [...]}`) so the
   detector, relay, TUI, and resume payload builder are unchanged; its
   synthetic `ToolMessage` uses the tool's own `_format_answers` rendering
   so the model's contract is identical.

## 3. Decision table (per `ask_user` tool call, auto mode only)

| Veritas result | Human attached | Autopilot |
|---|---|---|
| Confident answer (`classify == None`) | strip + synthetic `ToolMessage` (answers) — **no interrupt** | same |
| Defer / low confidence / answer-is-question / veritas failure | strip + gate `interrupt()` with `gate_deferred` marker → station → human relay (interactive pause) | retry sentinel inline (`(retry)` per question, no interrupt); retry disabled → gate `interrupt()` + marker → station → `ClarificationDeferredError` → **park** |
| Gate exception / missing context / manual mode / gate disabled | leave the call — the tool's own interrupt and today's flow run unchanged | same |

Fail-safe: any evaluation error leaves the call untouched (today's
authoritative station path remains fully intact).

## 4. Context plumbing

The gate's veritas call needs `LoopStateView` parity with the station's. The
executor already receives the per-step `clarification_loop_state_view`
(assembled by `node_execute` via `_build_loop_state_view`) — it now writes it
into the graph `configurable` (`soothe_veritas_loop_view`) alongside the
existing `workspace` / `thread_id` keys, plus `soothe_loop_id` for Langfuse
trace correlation. Missing view → gate no-op (fail-safe to station).

## 5. History accounting

Station-mediated answers are appended to `loop_state.clarification_history`
by `await_clarification` (capped at 20 entries) so later veritas calls see
prior Q&A. Gate inline answers bypass the station, so the gate emits a
`clarification_auto_answered` custom chunk via the node `stream_writer`
(same channel as RFC-634's `tool_auto_gate.rejected`); `node_execute`
consumes it from the stream and appends the same entry shape
(`questions` / `answers` / `source="veritas"` / `confidence`).

## 6. Config

```yaml
agent:
  clarification:
    ask_user_gate:
      enabled: true   # default on (migration); false restores pure-station behavior
```

Model, prompt, `max_context_steps`, and confidence threshold reuse the
existing `agent.veritas` / `agent.clarification.auto_min_confidence` values —
no new knobs.

## 7. Known edges

- Multiple `ask_user` calls in one AIMessage: confident ones inline-answer,
  deferred ones bundle into a single gate interrupt; resume answers
  distribute positionally across the deferred calls' questions (matching the
  station's batch-resume padding semantics).
- `rail_pause` origin is untouched (host-side, no CoreAgent graph — the
  station's veritas path remains its only answer path).
- Manual clarification mode: gate no-ops; the tool interrupts and the
  station routes to `InteractiveClarificationPolicy` exactly as today.

## 8. Test plan

- Gate unit tests: decision table, batch mix, answer distribution, retry
  sentinel, marker payload shape, `_format_answers` parity, fail-safe,
  manual-mode no-op, missing-view no-op.
- Policy tests: `gate_deferred` marker skips veritas and routes the fallback
  ladder.
- Wiring tests: builder installs the gate; executor writes the new
  configurable keys.
