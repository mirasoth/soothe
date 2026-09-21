# RFC-634: AutoModeMiddleware — Inline Tool-Approval Gate

**RFC**: 634
**Title**: AutoModeMiddleware — Inline Tool-Approval Gate (Phase 1)
**Status**: Implemented
**Kind**: Implementation Interface Design
**Created**: 2026-09-20
**Last Updated**: 2026-09-20
**Authors**: Soothe Team
**Depends on**: RFC-622 (CoreAgent Clarification Relay), RFC-623 (Veritas Auto-Mode Robustness), Multi-Stage Tool-Approval Pipeline draft (2026-08-27)
**Supersedes**: The station-side tool-approval pipeline evaluation (partial)

---

## 1. Abstract

Tool-approval decisions for the four mutating tools (`edit_file`, `write_file`,
`delete`, `run_command`) are currently resolved across three hops: the deepagents
`HumanInTheLoopMiddleware` (HITL) interrupts via `when_*` predicates, the
`await_clarification` station routes the request to a policy, and the policy's
`ToolApprovalPipeline` (`AutoClarificationPolicy._answer_tool_approval` /
`InteractiveClarificationPolicy` pre-filter) evaluates deny rules, safety
checks, and the loop allowlist — then resumes the HITL interrupt with a
decision. Every deterministic verdict (deny-rule reject, safety escalate,
default approve) pays a full graph-interrupt round trip even though no human
input is required.

RFC-634 introduces `AutoModeMiddleware`, a host `AgentMiddleware` installed on
the CoreAgent graph that becomes the sole tool-approval HITL. It evaluates every
gated tool call in its `after_model` hook (before tools execute), resolves
deterministic verdicts inline (deny-rule / autopilot-safety rejects become
error `ToolMessage`s without any interrupt; auto-mode approvals execute
silently), and emits the same `{"action_requests": [...]}` interrupt payload
HITL used whenever a human decision is genuinely needed. The station-side
pipeline evaluation, the `interrupt_rules.py` `when_*` predicates, the veritas
tool-approval prompt variants, and the dual-model fallback wiring are removed.
The three-layer clarification architecture (protocol / station / policy) is
unchanged for all non-`tool_approval` origins and for the human-relay leg.

---

## 2. Background

### 2.1 Current flow (pre-RFC-634)

```
LLM tool call → HITL.after_model (when_* predicate)
  ├─ safe            → execute (no interrupt)
  └─ dangerous       → interrupt({"action_requests": [...]})
        → executor captures → await_clarification station
        → policy.answer()
             ├─ AutoClarificationPolicy._answer_tool_approval
             │     pipeline.evaluate(): deny→reject / safety→escalate→human
             │     / allowlist→approve / default→auto-approve
             └─ InteractiveClarificationPolicy pre-filter (manual mode)
        → resume Command → HITL processes decisions
```

### 2.2 Why the middleware must *be* the HITL

Middleware-hook topology in `create_agent`/`create_deep_agent`:

- `HumanInTheLoopMiddleware` works via the **`after_model`** hook (not
  `wrap_tool_call`): it inspects the last `AIMessage`'s tool calls, calls
  `interrupt()`, and rewrites the tool-call list per the returned decisions.
- `after_model` hooks execute in **reverse middleware-list order**: host
  middleware (merged after the core stack) runs *before* HITL's `after_model`.
- A `wrap_tool_call` middleware can therefore never pre-empt the HITL
  interrupt — it only sees calls that already survived (or were approved
  through) it.

Conclusion: to inline-resolve verdicts *before* any interrupt, the gate must
implement `after_model` itself and the builder must stop passing
`interrupt_on` for the four mutating tools (in agent mode `fs_permissions` is
`None`, so no other HITL source exists and HITL is not installed at all).

### 2.3 Parity matrix (decision semantics preserved)

| Scenario | Pre-RFC-634 (station pipeline) | RFC-634 (middleware) |
|---|---|---|
| Deny rule match | interrupt → station → static reject → resume reject | inline instructive reject (no interrupt) |
| Safety hit, human attached | interrupt → station → escalate → human relay | interrupt (same payload shape) → station → human relay |
| Safety hit, autopilot | interrupt → station → instructive reject | inline instructive reject (no interrupt) |
| Safety hit, prior rule-family approval | when_* suppressed / station allowlist override | allowlist override evaluated inline |
| Ambiguous, auto mode | interrupt → station → default approve → resume approve | allow inline (no interrupt) |
| Ambiguous, manual mode (`all`) | interrupt → station → human relay | interrupt → station → human relay |
| Ambiguous, manual mode (`ambiguous_only`) | station pre-filter auto-approves | allow inline |
| Exact-signature allowlist approval | when_* suppressed | allow inline |

New capability (behavioral tightening, documented): deny rules now reject
gated calls that the old `when_*` heuristics would have let execute silently
(e.g. an in-workspace path matching a deny pattern) — the gate evaluates every
gated call, not only interrupt-worthy ones.

---

## 3. Design

### 3.1 Decision table (middleware, per gated tool call)

Ordered; first match wins.

| # | Condition | Action |
|---|---|---|
| 0 | tool not in `inline_gate.tools` / gate disabled | allow (pass through) |
| 1 | bypass mode and not `active_in_bypass` | skip deny/safety; allow |
| 2 | deny rule match | **inline instructive reject** |
| 3 | allowlist signature match | allow |
| 4 | safety check hit (nano `OperationSecurity`) | |
| 4a | ├─ rule-family allowlist override | allow |
| 4b | ├─ no human attached (autopilot) | **inline instructive reject** |
| 4c | └─ human attached | **interrupt** (human decides) |
| 5 | no rule/safety match | |
| 5a | ├─ bypass mode | allow |
| 5b | ├─ clarification mode `auto` | allow (default approve) |
| 5c | ├─ manual mode, scope `ambiguous_only` | allow |
| 5d | ├─ manual mode, scope `all`, human attached | **interrupt** |
| 5e | └─ manual scope `all`, autopilot | allow (headless parity: today's retry-sentinel path resolves to execute) |
| 6 | `tool_approval` in `force_manual_origins`, human attached | **interrupt** (every gated call) |

Fail-safe: any evaluation exception → the tool call is kept and a WARNING is
logged (the downstream FS permission layer and nano operation guard still run
as backstops). A missing `workspace` configurable makes path deny rules
unresolvable → treated as no match (absolute patterns like `/etc/**` still
match); a missing clarification mode falls back to the build-time
`default_mode`; missing `human_attached` defaults to `False` (autopilot — the
strict direction for safety hits).

### 3.2 Interrupt protocol (unchanged on the wire)

The middleware emits exactly the payload shape the executor's
`ClarificationDetector` and the relay already understand:

```python
{
    "action_requests": [{"name": ..., "args": ..., "description": ...}],
    "review_configs": [{"action_name": ..., "allowed_decisions": ["approve", "reject"]}],
    "escalated_rule_id": "<rule_id or None>",   # NEW: safety rule for allowlist override
}
```

- Resume: `Command(resume={interrupt_id: {"decisions": [...]}})` — the same
  HITL decisions shape `build_clarification_resume_payload` produces. The
  middleware maps decisions positionally onto its re-evaluated action list,
  tolerating a mid-flight allowlist change (missing trailing decisions default
  to `approve`, mirroring the station's padding rule).
- Decision handling mirrors `HumanInTheLoopMiddleware._process_decision`:
  `approve` → keep, `reject` → strip + error `ToolMessage`, `respond` →
  synthetic success `ToolMessage`, `edit` → rewrite name/args.
- On the resume pass the node re-executes; evaluation is deterministic so the
  rebuilt action list matches what the human answered.

### 3.3 Module structure

```
soothe/
├── sloop/middleware/
│   ├── auto_mode.py             NEW   AutoModeMiddleware (after_model gate)
│   └── auto_mode_context.py     NEW   per-run configurable readers
├── sloop/clarification/
│   ├── auto.py                  EDIT  remove _answer_tool_approval + pipeline param
│   ├── interactive.py           EDIT  remove pipeline pre-filter; rule_id from metadata
│   ├── tool_approval_pipeline.py EDIT  evaluate_action() single-call API
│   ├── interrupt_rules.py       REMOVED (when_* heuristics absorbed by the gate)
│   ├── detector.py              EDIT  pass escalated_rule_id into request metadata
│   ├── selector.py              EDIT  drop pipeline/manual_allow_rules params
│   └── runtime_factory.py       EDIT  drop pipeline + dual-model wiring
├── coreagent/builder.py         EDIT  remove interrupt_on; install the gate
├── subagents/veritas/prompts.py EDIT  remove tool_approval prompt variants (dead path)
├── config/models.py             EDIT  InlineGateConfig; remove VeritasFallbackConfig
├── events/catalog.py            EDIT  TOOL_AUTO_GATE_REJECTED
└── sloop/engine/execute/executor.py + stations/execute/execute.py
                                EDIT  plumb clarification_mode / human_attached
                                      into configurable
```

### 3.4 Per-run context (configurable keys)

| Key | Writer | Reader |
|---|---|---|
| `workspace` (existing) | executor | gate (path rules) |
| `tool_approval_allowlist` (existing) | executor | gate (signature + rule overrides) |
| `soothe_interaction_mode` (existing) | executor | gate (bypass detection) |
| `soothe_clarification_mode` (NEW) | executor | gate (auto/manual) |
| `soothe_human_attached` (NEW) | executor | gate (escalate vs inline reject) |

`node_execute` derives the mode from the live policy type
(`InteractiveClarificationPolicy` → manual, `AutoClarificationPolicy` → auto)
and human attachment from relay presence, passing both to the `Executor`.

### 3.5 Config

```yaml
agent:
  clarification:
    tool_approval:
      enabled: true
      inline_gate:
        enabled: true
        tools: [edit_file, write_file, delete, run_command]
        active_in_bypass: true   # deny rules still reject in bypass mode
      manual_scope: all          # unchanged semantics, now enforced by the gate
      deny_rules: [...]          # unchanged
```

`veritas_fallback` is removed: with the gate resolving every deterministic
case inline and routing the rest to the human relay, veritas never sees
`tool_approval` requests, so the fast-model fallback wiring and slim prompts
are dead paths.

### 3.6 Observability

- Structured INFO log per verdict: `[auto_mode] <decision> tool=<name>
  stage=<stage> rule_id=<...> signature=<truncated>` — grep-aggregatable like
  `[veritas] call_stat`.
- `TOOL_AUTO_GATE_REJECTED` catalog event, emitted via the node
  `stream_writer` as a custom chunk (payload: stage, reason, rule_id, tool,
  truncated signature). TUIs render it if they understand it; unknown custom
  chunks are dropped gracefully.

---

## 4. What is intentionally NOT in Phase 1

- No LLM classifier in the gate (the typesafe `AutoModeMiddleware` parity —
  calibrated-probability risk classification — is a Phase 2 extension point:
  the decision table's stage 5b would consult a classifier before allowing).
- No changes to non-`tool_approval` origins (`execute`, `plan_mode_review`,
  `rail_pause`): veritas auto-answering, defer taxonomy, and the auto→manual
  upgrade path are untouched.
- Subagent scope: the gate is installed on the main agent graph only (same
  scope as the old `interrupt_on` wiring).
- No rule persistence / "always allow" TUI affordance (loop-scoped allowlist
  recording on human approval is unchanged, in `node_execute`).

## 5. Known edges

- Mid-flight allowlist change between interrupt and resume can shrink the
  re-evaluated action list; decisions are applied positionally with
  `approve`-padding (the old `when_*` predicates had the same re-evaluation
  property, but silently dropped the resume payload instead).
- In `plan`/`ask` modes the FS-permission-derived HITL may still exist for
  read-path tools; the gate composes with it (runs first, disjoint tool set).

## 6. Test plan

- Gate unit tests: every decision-table row, bypass matrix, fail-safe on
  evaluator error, decision processing (approve/reject/respond/edit), message
  rewrite (strip + artificial `ToolMessage`s), non-gated calls untouched.
- Pipeline: `evaluate_action` stage semantics (deny → allowlist signature →
  safety + rule override).
- Policy tests: `tool_approval` origin routes to interactive fallback (auto) /
  relay (manual); pipeline params removed.
- Wiring: builder installs the gate and no longer passes `interrupt_on`;
  executor writes the new configurable keys.
- Prompt tests: tool-approval prompt variants removed.
