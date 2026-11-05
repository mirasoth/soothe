# IG-LOOP-015: Redesign ask_user Triggering

Unify `ask_user` handling so veritas is the single arbiter across skill- and
CoreAgent-triggered calls, remove the `force_manual_origins` config whitelist,
and close the coverage→search_tools spiral observed in loop b047.

## Problem

Two defects, both visible in loop `01a1a6ec-…-b047`:

1. **Coverage manufactures an "ask the user" execution step that the LLM
   never executes.** An Eval step calls `coverage_verdict(complete=false,
   remaining_subtasks=[{description: "present Decision #1 via ask_user"}])`.
   `verdict_tool._record_verdict` queues this as a `DecompositionProposal`;
   reconcile commits it as an ordinary execution step; dispatch hands the
   LLM a step brief that says "present via ask_user." The LLM then spirals
   into 80+ redundant `search_tools` calls and is cancelled on model quota
   failure — the `ask_user` tool is never invoked, no clarification interrupt
   is emitted, and the goal is marked `user_cancelled`. The UI never sees a
   popup because the relay was never engaged.

2. **`force_manual_origins` is a config whitelist that splits the
   clarification path by origin.** `AutoClarificationPolicy` force-routes
   `plan_mode_review` (and any configured origin) straight to the human
   relay, skipping veritas. This predates the unified veritas fast path and
   contradicts the desired posture: veritas should determine every
   `ask_user`, and the only structural human gates (`plan_mode_review`,
   `rail_pause`) should be routed *by veritas*, not by an operator-tunable
   list.

Goals (operator decisions, settled):

- **Unify.** No skill-vs-CoreAgent distinction. Every `ask_user` — whether
  prompted by a skill's GATE-INSTRUCTION, a tool-approval policy, or the
  CoreAgent's own judgment — flows through one veritas-arbitrated path.
- **Bypass mode unchanged.** `AskUserGateMiddleware` keeps holding all
  `ask_user` determination (veritas answers confident calls; defer → retry
  sentinel). The b047 spiral is fixed at its source (coverage) so the
  agent is never handed a "go ask the user" execution step.
- **Auto mode: remove the whitelist.** Delete `force_manual_origins` /
  `DEFAULT_FORCE_MANUAL_ORIGINS`. Veritas determines every origin.
  `plan_mode_review` and `rail_pause` remain human-gate semantics, now
  routed by veritas directly to clarification rather than by config.
- **b047 fix.** When coverage detects a goal blocked on a user decision,
  emit a `ClarificationRequest` straight into the relay → `await_clarification`.
  Never materialize a continuation execution step whose brief is "ask the
  user."

## Solution

### Part A — Coverage emits clarification directly (b047 fix)

Today the `coverage_verdict` tool only queues execution subtasks. Add a
structured `pending_clarification` payload so an Eval step can report "the
goal is blocked on a user decision" and route it through the relay without
creating a step the LLM must re-derive.

`context/decomposition.py`:
```python
class CoverageClarification(BaseModel):
    """A user-decision gate surfaced by an Eval step via coverage_verdict.

    Routed straight to await_clarification; never materialized as an
    execution step (closes the b047 search_tools spiral).
    """
    questions: list[QuestionSpec] = Field(min_length=1)
    reason: str = Field(default="", description="Why the goal is blocked on the user.")

class CoverageVerdict(BaseModel):
    complete: bool
    reasoning: str = ""
    remaining_subtasks: list[ProposedSubtask] = Field(default_factory=list)
    pending_clarification: CoverageClarification | None = None
```

`QuestionSpec` is imported from `coreagent/tools/ask_user.py` so the relay
and TUI render coverage gates identically to live `ask_user` gates.

`eval/verdict_tool.py::_record_verdict`:
- When `verdict.pending_clarification` is set **and** `complete is False`,
  write a `ClarificationRequest` (state dict) into a new verdict-scoped
  clarification sink bound by `decompose.runtime` (mirror
  `current_proposal_sink` → add `current_clarification_sink`). Do **not**
  queue the gate as a `remaining_subtask`; the LLM is instructed (prompt)
  to put user-decision gates into `pending_clarification`, not
  `remaining_subtasks`.
- Return a tool string telling the thread to end ("Coverage verdict
  recorded (blocked on user decision); clarification queued. End thread.")

`stations/decompose/reconcile_node.py::_collect_proposals`:
- Also drain `current_clarification_sink` (and `ctx.scratch` equivalent)
  into a separate `pending_clarifications` list. Reconcile does **not**
  commit these as steps. Instead `ReconcileNode.process` returns them in
  the `NodeResult` payload; the orchestrator routes to
  `await_clarification` when non-empty (same routing key the execute
  station already uses for captured `ask_user` interrupts).

`stations/execute/execute.py` (relay enqueue helper):
- Extract the existing "enqueue ClarificationRequest → project to relay
  channels → route to await_clarification" block (lines 886-928) into a
  reusable helper `_enqueue_pending_clarification(ctx, request, step_id)`.
  Reuse it for both planner-emitted ask_user steps and the new
  coverage-emitted gate (origin `ORIGIN_COVERAGE_GATE`).

New origin `clarification/origins.py`:
```python
ORIGIN_COVERAGE_GATE: Final = "coverage_gate"
"""Eval coverage_verdict user-decision gate; routed straight to the relay."""
```
Add to the `ClarificationOrigin` Literal and `CLARIFICATION_ORIGINS`.
`AutoClarificationPolicy.answer` treats `coverage_gate` like `execute` —
veritas determines (auto mode) / veritas holds (bypass mode).

Routing outcome by mode:
- **Auto:** relay → `await_clarification` → veritas; confident → synthetic
  answer resumes the goal; defer → human popup. The decision the user
  supplies is written back via `relay.record_answers` and consumed by the
  next plan-execute wave as context.
- **Bypass (autopilot):** `AskUserGateMiddleware` posture already applies —
  veritas answers or emits the `(retry)` sentinel. No execution step is
  created, so the search_tools spiral cannot occur. If veritas cannot
  resolve a genuine design decision, the goal proceeds with the retry
  sentinel (best effort) rather than looping.

### Part B — Remove `force_manual_origins`; veritas routes all origins

Delete the configurable whitelist. The two structural human gates stay
human-routed, but as veritas's own routing, not operator config.

`clarification/auto.py::AutoClarificationPolicy`:
- Remove `force_manual_origins`, `requires_manual`, `_force_manual_origins`,
  `_answer_force_manual`, the `force_manual_origins` property.
- `answer()` becomes:
  ```python
  async def answer(self, request):
      if request.origin_node == "tool_approval":
          return await self._answer_tool_approval(request)        # human (RFC-634)
      if request.origin_node in _HUMAN_GATE_ORIGINS:              # {plan_mode_review, rail_pause}
          return await self._answer_human_relay(request)          # veritas routes to clarification
      return await self._answer_veritas(request)                  # execute / coverage_gate / others
  ```
  where `_HUMAN_GATE_ORIGINS = frozenset({ORIGIN_PLAN_MODE_REVIEW, ORIGIN_RAIL_PAUSE})`.
  `_answer_human_relay` is the old `_answer_force_manual` body (delegate to
  `interactive_fallback`, else autopilot retry, else `ClarificationDeferredError`).
- `degrade_to_manual_on_failure` and `autopilot_retry_on_fail` stay — they
  govern veritas *failure* behavior, not origin routing.

`config/models.py`:
- Delete `DEFAULT_FORCE_MANUAL_ORIGINS` (line 1369) and the
  `force_manual_origins` field on `ClarificationConfig` (1410-1418).
- Drop the `force_manual_origins` Literal enum that listed allowed origins.

`stations/sidecars/await_user.py::_mode_for_policy` (lines 339-349):
- Remove the `requires_manual` introspection branch. `_mode_for_policy`
  returns `"manual"` for `InteractiveClarificationPolicy` or for origins in
  `_HUMAN_GATE_ORIGINS`, else `"auto"`. This keeps the
  `clarification_requested` event's `mode` field accurate for the TUI
  without reading a removed method.

`coreagent/builder.py:118`:
- Remove `force_manual_tool_approval="tool_approval" in
  clar_cfg.force_manual_origins`. `tool_approval` is already hardcoded in
  `AutoClarificationPolicy.answer` (the `tool_approval` branch); the
  middleware wiring does not need the config flag.

`clarification/selector.py` / `clarification/runtime_factory.py`:
- Drop the `force_manual_origins` parameter and its forwarding to
  `AutoClarificationPolicy`.

### Part C — Bypass mode unchanged

`AskUserGateMiddleware` is not modified. It already:
- activates in auto **and** bypass mode (`ask_user_gate.py:126`),
- runs veritas inline on every `ask_user` call regardless of origin
  (`_evaluate`, line 216), with `origin_node=ORIGIN_EXECUTE`,
- routes defer → human relay (auto) or `(retry)` sentinel (bypass/autopilot).

The b047 spiral is prevented upstream (Part A) — the agent is never given
an execution step whose brief is "call ask_user," so the LLM never reaches
the search_tools fallback. The gate continues to arbitrate any `ask_user`
the LLM *does* emit.

## Files

| Change | File |
|--------|------|
| `CoverageClarification` + `pending_clarification` field | `context/decomposition.py` |
| `current_clarification_sink` contextvar | `sloop/decompose/runtime.py` |
| Verdict tool routes pending_clarification to sink | `sloop/eval/verdict_tool.py` |
| Reconcile drains sink; routes to await_clarification | `sloop/stations/decompose/reconcile_node.py` |
| Reusable `_enqueue_pending_clarification` helper | `sloop/stations/execute/execute.py` |
| New `ORIGIN_COVERAGE_GATE` origin | `sloop/clarification/origins.py`, `protocol.py` |
| Remove `force_manual_origins` / `requires_manual` / `_answer_force_manual` | `sloop/clarification/auto.py` |
| `_mode_for_policy` uses `_HUMAN_GATE_ORIGINS` | `sloop/stations/sidecars/await_user.py` |
| Delete config whitelist | `config/models.py` |
| Drop builder flag | `coreagent/builder.py` |
| Drop selector/factory param | `sloop/clarification/selector.py`, `runtime_factory.py` |
| Coverage prompt: gates → `pending_clarification`, not subtasks | `prompts/` (coverage verdict tool desc + eval prompt) |

## Cleansing (Rule 19)

Removed in this change (no compat shims, no aliases):
- `DEFAULT_FORCE_MANUAL_ORIGINS`, `ClarificationConfig.force_manual_origins`
  and its Literal enum.
- `AutoClarificationPolicy.force_manual_origins` property,
  `requires_manual`, `_answer_force_manual`, `_force_manual_origins`.
- `selector.py` / `runtime_factory.py` `force_manual_origins` parameter.
- `coreagent/builder.py` `force_manual_tool_approval` flag.
- Tests that assert whitelist behavior (rewritten, not skipped — see below).

## Test plan

Tests live in `packages/soothe/tests/unit/core/loop/`:

1. `clarification/test_auto.py` — rewrite the `force_manual_origins` block
   (lines 455-524): assert `plan_mode_review` and `rail_pause` route to the
   human relay (veritas routing), `execute` and `coverage_gate` route to
   veritas, `tool_approval` routes to human. Assert no `requires_manual`
   attribute exists.
2. `clarification/test_origins.py` — drop `DEFAULT_FORCE_MANUAL_ORIGINS`
   import; assert `ORIGIN_COVERAGE_GATE` is in `CLARIFICATION_ORIGINS`.
3. `clarification/test_runtime_factory.py` — drop
   `force_manual_origins` param assertions; assert `AutoClarificationPolicy`
   builds without it.
4. New `eval/test_verdict_tool_pending_clarification.py` — `coverage_verdict`
   with `pending_clarification` writes to the clarification sink, does **not**
   queue a `remaining_subtask`, and returns the "end thread" string.
5. New `stations/decompose/test_reconcile_coverage_gate.py` — reconcile
   drains the clarification sink and routes to `await_clarification`;
   no `StepNode` is committed for the gate.
6. `stations/execute/test_tool_approval_bridge.py` — remove
   `DEFAULT_FORCE_MANUAL_ORIGINS` usage; assert `tool_approval` still routes
   to human via the hardcoded branch.
7. `sloop/middleware/test_auto_mode.py` — remove
   `force_manual_origins=("plan_mode_review",)` fixtures (line 656).

Verify: `./scripts/verify_finally.sh` (lint, typecheck, tests, vulture).

## Out of scope

- Proactive pre-execution ambiguity detection (veritas deciding whether to
  ask *before* the LLM calls `ask_user`) — explicitly deferred; veritas
  stays reactive in this change.
- `TYPESAFE_API_KEY` outage remediation (separate ops issue; flagged in the
  b047 diagnosis).
- Model quota / blocked-key remediation for `openai/glm-5.2` (ops).
