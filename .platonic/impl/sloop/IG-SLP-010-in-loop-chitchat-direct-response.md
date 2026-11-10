# IG-SLP-010: Delete Intake Classification — In-Loop Direct Response

**Scope**: `packages/soothe/` (sloop intention + preprocess stations + orchestrator + runner + decompose gates + completion finalize + daemon protocol touchpoints + tests), plus in-tree client updates
**Related**: IG-SLP-009 (short-chain fast finalize / LEDGER_DIRECT), IG-SLP-008, IG-LOOP-017 (typesafe classifier)
**Evidence**: loop `01a1bf01-30fd-7d22-a326-ae74c58b6921` (log #6921), 2026-11-09 15:23

## Problem

The loop graph classifies every user input *before* the agent ever sees it:
`START → INTAKE → ENTER_LOOP`, with `route_after_preprocess` ending the graph
on `chitchat`. This pre-loop classifier is an irreversible routing decision made
by a weaker model that cannot answer the question it is routing.

Loop 6921 demonstrates the failure end to end:

1. User asks **"what model are you using"** as a follow-up inside an active loop.
2. TypeSafe intent classifier votes at conf **0.53** — drift guard rejects it,
   falls back to the LLM intake path.
3. LLM intake classifies `is_task=False, confidence=high` with reasoning
   *"I'll introduce myself as Soothe."* — the intake prompt's SOCIAL category
   ("greeting, thanks, **identity**, date/time, small talk") plus its
   `"who are you?"` / `"what is ur name"` few-shot examples absorb any
   identity-adjacent question, including model/capability questions the prompt
   never covers.
4. `route_after_preprocess → END (chitchat fast-path)`. The user gets a canned
   social reply; the main agent — which could actually answer — never runs.
   The misroute repeated identically on retry.

Beyond misrouting, the classifier stack costs real latency and surface area:

- Up to **two extra LLM round trips per turn** (intake classify + social reply
  generation) even when the verdict is correct.
- **Two parallel classification stacks** to maintain (TypeSafe `Choice` + LLM
  structured JSON) whose only outputs are a 4-way label and a language tag.
- A dedicated social-reply station, intake prompt fragments with identity and
  vendor-denylist rules, generic fallback pickers, ledger projection for the
  intake prompt, and a chitchat fast-path persistence side-channel in the
  runner — all existing solely to serve the pre-loop verdict.
- The chitchat path is **non-streaming**: the canned reply is emitted as one
  chunk after the classifier finishes.

## Solution

Delete pre-loop intake classification. **Every user input enters the agent
loop.** The main model sees the input with full CE ledger context and:

- For a conversational input (greeting, identity, meta/capability questions,
  small talk): answers in **one step with zero tool calls**. No decomposition
  (typesafe decompose gate already returns no-split), no Eval (new structural
  skip), no synthesis (LEDGER_DIRECT passthrough). The agent's answer streams
  to the user directly.
- For a task input: the loop behaves exactly as today.

This is strictly more capable than the classifier: the model that answers *is*
the decision-maker, so "what model are you using" gets a real, context-aware
answer (subject to the system prompt's identity policy) instead of a canned
"I'm Soothe." There is no misroute class left to fix, because routing stops
being a prediction and becomes an action.

The chitchat exchange persists to the ledger through the **normal**
RECORD_PROGRESS machinery — no special-case chitchat persistence. Latency is
preserved structurally, not by a classifier (see Latency Analysis).

### Surviving routing carrier

The only intake verdict worth keeping is the **client-forced scope**
(`loop_input.intake_scope`: minimal|simple|complex, daemon protocol field) —
an explicit external override, not a prediction. It survives, remapped onto
`RoutingClassification` + `TaskComplexity` (both already in
`soothe_nano.protocols.routing`, already wire-shared, already in the serde
allowlist). The LLM/TypeSafe auto-classification and `IntentClassification`
die.

## Changes

### Deleted — `packages/soothe/src/soothe/sloop/intention/`

Remove the entire auto-classification stack:

- `classifier.py` (`IntentClassifier` facade), `intake_classifier.py`
  (`IntakeClassifier`, task fallback), `coordinator.py`,
  `typesafe_intent.py`, `social_reply.py`, `chitchat_fallbacks.py`,
  `structured_methods.py` (INTAKE_JSON_FIRST_METHODS),
  `invoke_config.py` (intake-specific invoke config).
- `models.py`: delete `IntakeLabel`, `IntentClassification`,
  `IntakeConfidence`, `IntakeScope`, `ResponseLanguage`,
  `intent_classification_from_intake`,
  `intent_classification_from_intake_scope`,
  `build_loop_routing_classification`, `derive_task_complexity_from_intake`.
  Keep only what clarification/decompose/eval still import.
- `prompts.py` and `src/soothe/prompts/fragments/intake/` (system fragment,
  social reply fragment) — delete wholesale. The assistant identity block and
  vendor denylist live in `soothe.identity` / main system prompt and stay.

`parse_intake_scope` moves to the daemon-side mapping below or to a small
routing helper — the daemon protocol validation
(`soothe_daemon/protocol/intake_scope.py`) imports it from intention.models
today; repoint it.

### `packages/soothe/src/soothe/sloop/orchestrator/builder.py` + `routing.py`

- Remove the `INTAKE` node. New entry: `START → ENTER_LOOP`.
- `route_after_preprocess` loses the `fast_path → END` branch; it routes
  DELEGATE | DISPATCH only (rename to `route_after_enter` — semantic, not
  numeric, naming).
- `ENTER_LOOP` (`stations/preprocess/enter_loop.py`): delete the chitchat
  fast-path branch and `intent_route="fast_path"`. The wired-subagent branch
  stays, driven by `routing_classification` built from the client-forced scope
  or explicit `preferred_subagent` only.
- Delete `stations/preprocess/intake.py` (`node_intent_classify`,
  reasoning/cognition-card helpers, "Interpreting goal" status label).

### `packages/soothe/src/soothe/sloop/state/schemas.py`

- Remove `intent: Any | None` from loop state (line ~771) and
  `response_language` (intake-derived). Add `routing_classification`
  population from forced scope in the runner (below).

### `packages/soothe/src/soothe/runner/__init__.py` + `_runner_strange_loop.py` + `_runner_phases.py`

- Delete `IntentClassifier` construction; the fast model stays (decompose,
  eval, clarification still use it).
- Forced scope (`intake_scope` param): map directly to
  `RoutingClassification`/`TaskComplexity` and set
  `loop_state.routing_classification` — the "pre-classified" skip path in
  INTAKE disappears with the node.
- `_runner_phases.py`: delete `_run_chitchat`, `_save_chitchat_to_state`,
  `_save_chitchat_to_ledger`, `_finalize_chitchat_loop`, and the
  chitchat-piggyback event handling. Trim the msgpack serde allowlist of
  `IntakeLabel` / `IntakeConfidence` / `IntakeScope` / `ResponseLanguage`
  (keep `RoutingClassification`, `TaskComplexity`).
- `strange_loop.py`: drop `intent_classifier` plumbing; forced-scope intent
  override becomes a routing_classification override.
- `structural_continuation.py`: delete `should_bypass_chitchat_fast_path`
  and `chitchat_may_finalize_checkpoint` with their call sites.
- Ledger projection: remove the intake goal-completion projection in
  `soothe/prompts/plan_ledger_projection.py` (only consumer was intake).

### `packages/soothe/src/soothe/sloop/stations/decompose/dispatch.py`

- `task_complexity` source becomes `routing_classification.task_complexity`
  (forced scope) or `None`. `None` already falls back per
  `schemas.py` ("None falls back to goal-level intent" → now: falls back to
  the typesafe decompose gate's own split/no-split verdict, which already runs
  for every goal).

### `packages/soothe/src/soothe/sloop/stations/decompose/root_eval.py`

Replace `intake_label` gating with two signals:

1. **Complexity gates** (forced scope only): `TaskComplexity.MINIMAL` → skip
   Eval, finalize; `SIMPLE` → `decide_eval_required` (drop its
   `intake_label` arg); `COMPLEX` → current behavior.
2. **New structural skip** (unforced turns): when the goal ran as a single
   leaf with **zero tool calls**, no decomposition children, and no user
   interrupt — skip Eval and finalize. This is what makes in-loop chitchat
   fast: a conversational answer takes the structural exit with **no Eval LLM
   call**. The plan/ask interaction-mode Eval prohibition is untouched (this
   path is about conversation turns, not read-only modes).

### `packages/soothe/src/soothe/sloop/stations/completion/finalize.py`

- LEDGER_DIRECT passthrough already covers single-leaf chains (IG-SLP-009).
- Add the same structural bypass to the report-validation typesafe call:
  single leaf + zero tool calls → the step output **is** the direct reply;
  pass it through without the validation round trip. Task outputs keep
  validation.

### Language handling

- `loop_state.response_language` and the intake language verdict die.
  EXECUTE and synthesis prompts already contain the conversation; add one line
  to the synthesis prompt: mirror the user's conversational language.
  `PRIOR_RESPONSE_LANGUAGE` inheritance dies with the intake prompt.
- `decide_eval_required` and other `intake_label`-taking helpers drop the
  argument.

### Execute prompt

- One paragraph in the execute/system prompt: conversational inputs that need
  no tools are answered directly in one step — no preamble, no plan narration.
  This replaces the intake prompt's social-reply instructions.

### Wire / events

- Delete events: `intent_classified`, `intent_classified_reasoning`,
  `intent_fast_path`. (Grep shows no CLI or client consumers today; the TUI
  "Interpreting goal" cognition card is the only UX loss and it described the
  classifier, not the work.)
- Ledger chitchat messages now record with phase `execute` / final assistant
  message under `goal_completion`. Clients' `DEFAULT_DELIVERABLE_PHASES`
  already includes `goal_completion`; `chitchat` stays in the constant as a
  tolerated legacy phase until the next client release removes it.
- In-tree clients (go/python/rust/typescript): remove intent-event handling if
  present, regenerate the wire golden fixture (new sha), bump per the client
  adaptation process.

### Migration / compatibility

- Old checkpoints carrying `intent` payloads: field is removed from the
  schema; msgpack allowlist drops dead enums silently and the field is
  tolerated as absent. No migration script.
- `loop_input.intake_scope` protocol field is unchanged — clients that force
  scope keep working.

## Latency Analysis

| Turn type | Today | After |
|---|---|---|
| Chitchat (correct route) | typesafe 0.3s (often untrusted) → intake LLM 3–5s → social-reply LLM 2–4s → END. Non-streaming. | decompose gate ~0.3–1s → EXECUTE main-model turn, **streaming** (TTFB ~1s) → structural Eval skip (0 LLM) → LEDGER_DIRECT + structural bypass (0 LLM). |
| Chitchat (misrouted, loop 6921) | Same path, wrong canned answer; user retries → full cost again. | Impossible — no pre-loop verdict to be wrong. |
| Task | intake LLM 3–5s **before** the loop starts. | Loop starts immediately. Strictly faster. |

Wall-clock for chitchat is comparable or better, perceived latency is better
(streaming vs. single chunk), and every task turn saves the classifier trip.

## Test Plan

- Delete: `test_intent_classification.py`, `test_chitchat_fast_path.py`,
  `test_typesafe_intent.py`, `test_social_response_resolution.py`,
  intake cases in `test_intent_classify_display.py` /
  `test_route_after_preprocess.py` / `test_intake_routing.py`.
- Add (unit, per Test Location rule):
  - Forced `intake_scope` → `RoutingClassification` mapping and downstream
    dispatch/root_eval gates.
  - Single-leaf zero-tool execution → root_eval structural skip → LEDGER_DIRECT
    passthrough with no Eval/validation LLM calls (assert via LLM call counters).
  - Single-leaf **with** tool calls → Eval path unchanged.
  - Graph builder: `START → ENTER_LOOP`; no INTAKE node.
  - Legacy checkpoint with `intent` payload loads and drops the field.
- Integration: run `./scripts/verify_finally.sh`; regenerate wire golden
  fixture; manual loop test asking "what model are you using" mid-task must
  produce a real answer through EXECUTE.

## Non-Goals

- No change to clarification, decompose, eval-decision, or wired-subagent
  mechanics beyond the argument/field renames listed above.
- No change to the daemon `intake_scope` protocol contract.
- The assistant identity / vendor-denylist policy stays in the system prompt;
  this IG changes *who* applies it (the main model), not the policy.
