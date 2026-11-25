# IG-LOOP-018: ask_user Channel Closure + Approve & Remember

**Created**: 2026-11-25
**Status**: Implemented (verify_finally.sh green)
**Related**: IG-LOOP-009 (loop-scoped allowlist), IG-LOOP-014 (veritas decision fast
path), IG-CLNT-005 (approval dock), IG-LOOP-015 (ask_user triggering)

## Problem

Two independent gaps surfaced while auditing the ask_user channel and the
human tool-approval flow:

### WS-A — ask_user channel logic black holes

Every `ask_user` question should terminate in exactly one of: a veritas
auto-answer, a human answer, an explicit "unanswered" sentinel, or a park.
Audited holes (all in `ask_user_gate.py` unless noted):

- **H1 `"(retry)"` fake answer.** autopilot auto-mode defers, veritas call
  exceptions (autopilot), and ALL bypass defers feed the literal string
  `(retry)` back as the ask_user tool result — nobody answered, no event,
  the question is silently swallowed (`:242-249`, `:259-267`).
- **H2 malformed args pass through.** `_extract_questions` returning `None`
  lets the call reach the real `ask_user` tool; the interrupt it raises is
  uncapturable (`executor.py:1175-1183` logs "uncapturable ask_user; ending
  CoreAgent stream without auto-resume") — the step stream terminates
  (`:282-295`).
- **H3 silent dismissal.** A resumed clarification with no answer slice
  renders "Clarification dismissed" with no event (`:198-207`).
- **H4 answer fabrication.** `_distribute_answers` pads missing per-question
  answers by duplicating the last one (`:326-339`).
- **H5 bypass pays veritas per question** but its defers still hit H1 —
  inconsistent with the "no judgment" bypass posture of the tool gate.
- **H6 observability.** Only `CLARIFICATION_AUTO_ANSWERED` exists; every
  non-answer outcome is invisible on the wire.

Operator decisions (2026-11-25): autopilot defer default = **park** (option a);
bypass = **B-a** (keep the veritas fast path, honest sentinel on defer).

### WS-B — repeated human approvals re-ask per exact signature

`manual_scope=all` asks the human once per distinct gated-action signature. A
session editing 50 files mints 50 dock cards. The grant machinery already
supports generalized records — `{"tool","prefix"}` for `run_command` and
`{"rule"}` families (`dynamic_rules.py`) — but:

- Human approvals record **exact signatures only** (`execute.py:632-643`), and
  the mint condition is `ans.answers[0] == "approve"` — so an `Allow`/`Yes`
  answer token (mapped to an approve decision by `outbox.answer_to_decision`)
  mints **nothing** (decision/grant vocabulary asymmetry).
- Multi-tool batches mint records for **every** action request keyed off
  `answers[0]` alone — a batch `[approve, reject]` mints a grant for the
  rejected tool too.
- The decision-option vocabulary is scattered: `outbox._APPROVE_TOKENS`,
  `detector._format_action_request` options, executor mint condition, TUI
  `_TOOL_APPROVAL_ACTIONS`, veritas `_APPROVE_TOKENS` — no shared module.

## Fix

### Design invariants

1. **Closure** — every ask_user question ends in veritas-answer / human-answer
   / explicit-unanswered-sentinel / park. Never silently swallowed.
2. **Veritas is the only auto-answer source** — any synthetic ToolMessage
   answer comes from veritas (confidence + event); everything else declares
   itself "not an answer".
3. **One decision vocabulary** — a single module owns the tool-approval
   answer tokens, their grant generalization level, and the card options;
   every surface (detector, outbox, executor, TUI) consumes it.

### WS-A — ask_user gate restructure (`ask_user_gate.py`)

- **Validation leg, all modes.** Malformed/empty `ask_user` args are stripped
  with an instructive error ToolMessage ("re-emit with valid questions") in
  auto, bypass, AND manual (manual previously passed them through to the
  uncapturable-interrupt death). Valid calls in manual still pass through to
  the native human interrupt untouched.
- **Bypass B-a.** Veritas fast path stays (confident → inline answer).
  Defer/exception outcomes render an honest sentinel — "(bypass: unanswered —
  no human surface; decide autonomously or switch mode)" — instead of
  `(retry)`, plus an event.
- **Autopilot defer default = park.** `ClarificationConfig.autopilot_retry_on
  _fail` default flips `True → False`: veritas defers without a human
  hard-defer (goal parks in `awaiting_clarification`, resumable by a later
  channel/TUI answer) instead of the retry sentinel. The sentinel stays
  available as an explicit opt-in, and its text becomes honest too.
  Constructor defaults in `AutoClarificationPolicy` / `AskUserGateMiddleware`
  / `selector` intentionally stay `True` — production always threads the
  config value; the config is the single behavioral source.
- **H4.** `_distribute_answers` pads missing slots with an explicit
  "(no answer provided for this question)" marker instead of duplicating the
  last answer (single-answer broadcast across calls stays — station parity).
- **H6.** New event `CLARIFICATION_UNANSWERED` (`soothe.internal.clarification
  .unanswered`) with `{kind, mode, questions}` — emitted for bypass sentinels,
  autopilot opt-in retries, dismissals, and malformed calls. Station-side
  retry answers (opt-in) remain log-only (no stream writer in policy scope).

### WS-B — Approve & Remember shared module

New `sloop/clarification/approval_options.py` — the one decision vocabulary:

- Tokens: `approve` family (unchanged) + **remember family** — canonical wire
  label `"Approve & remember"` (the card option label IS the answer token,
  normalized to lowercase; `approve_remember` / `always` also accepted).
- `is_approve_answer(answer)` — approve ∪ remember tokens; consumed by the
  executor mint condition (fixes the `Allow`/`Yes` asymmetry).
- `is_remember_answer(answer)` — remember tokens only.
- `decision_options(tool_name, args)` — the option list for a pending action
  (Approve / Approve & remember / Reject); the remember option is offered only
  when the action has a generalizable grant.

`dynamic_rules.py` gains:

- `remember_record(tool, args)` — the generalized grant: `run_command` →
  `{"tool","prefix"}` (first command token, same as the veritas-leg mint);
  path tools → `{"tool","dir"}` (parent directory of `file_path`); `None`
  when not generalizable.
- `grant_hit` honors `{"tool","dir"}` records for path tools: exact dirname
  equality or a structural `dir + "/"` prefix of the file path (the whole
  subtree under the recorded directory; no normalization, no `..`/symlink
  collapsing — Rule 9).

Consumers:

- **detector** `_format_action_request` renders options from
  `decision_options` — every human-decision surface (manual escalations, auto
  safety escalations, deferred tool approvals) gets the same options because
  they all render through this one formatter. Card compaction: options are
  label-only (the remember description was dropped — the label is
  self-explanatory and the card must stay compact), and the question is the
  short "Approve this action?" (the header right above already carries the
  tool name and argument preview).
- **outbox** `answer_to_decision` maps remember tokens → `"approve"` HITL
  decision (the resume payload carries `"remember": True` for audit; the HITL
  middleware ignores extra keys). `build_clarification_resume_payload` uses
  the shared token sets.
- **executor** (`stations/execute/execute.py`) mints per
  `(action_request, answer)` pair — only tools whose own answer is
  approve/remember get a record (fixes the batch `[approve, reject]` bug);
  remember answers mint `remember_record` (falling back to the exact
  signature), plain approves keep `approval_record`; escalated-rule family
  overrides unchanged.
- **TUI** (`soothe-cli`) — zero protocol change: options travel in the
  question spec; the widget renders them dynamically. Display-only updates:
  `_TOOL_APPROVAL_ACTIONS` gains the label; answered rows render
  "Approved (remember)". `comment_option_index` is label-scanned at mount
  ("refine"/"edit"), so the extra option cannot shift the Edit index.
- Veritas-leg minting (`LoopGrantStore.record`) already generalizes
  (command prefix) and is untouched.

### Follow-up (2026-11-25): manual-mode deny-rule escalation

Operator decision: in manual mode a **user deny-rule hit escalates to the
human** instead of rejecting inline (human sovereignty — the operator may
override their own rule; safety failures already escalate).

- `tool_approval_pipeline.py` stage 1: `_matching_rule` returns the matched
  rule; deny results carry a synthesized `rule_id =
  "deny_rule:<tool>:<pattern>"`. An allowlist record `{"rule": <that exact
  id>}` (stamped only by a human approval of the escalated deny hit, via the
  existing `escalated_rule_id` → executor mint path) **approves at stage 1**
  — direct approve, never fall-through (a rule record alone must not reach
  the safety/default stages). Signature/prefix/dir grants and nano
  rule-family records never override a deny.
- `auto_mode.py` `_evaluate`: `decision == "reject"` with stage
  `deny_rule` + manual mode + not bypass → escalate verdict (payload picks
  up the deny rule id through `escalated_rule_id`). Auto/bypass behavior
  unchanged; `active_in_bypass` + manual keeps deny absolute.
- Executor: zero changes — the deny rule id flows through the established
  `escalated_rule_id` → `{"rule": …}` mint, and the pipeline's stage-1 exact
  match consumes it.

## Out of scope

- Station-side (policy) stream events for opt-in retry answers.
- Any change to safety stages / bypass tool-gate short-circuit (deny rules
  ARE now manual-escalatable — see the follow-up above).
- Directory-grant UI affordances beyond the option label (no per-file
  previews in the card).
- Headless CLI (`--mode`) vocabulary — unaffected.

## Verification

- `tests/unit/sloop/middleware/test_ask_user_gate.py`: matrix —
  {auto-TUI, auto-autopilot, bypass, manual} × {confident, defer, exception,
  malformed, dismissed}; park-default (config False) vs opt-in retry.
- `tests/unit/core/loop/clarification/`: `approval_options` token table,
  `dynamic_rules` remember/dir matching, `outbox` remember decode,
  executor-mint semantics (via outbox + dynamic_rules composition).
- TUI: `test_clarification_mode_*` untouched; widget display-mapping tests
  for "Approved (remember)".
- Full: `PYTHONPATH=src pytest tests/unit/sloop tests/unit/core/loop/clarification`
  + ruff + verify_finally.sh.
