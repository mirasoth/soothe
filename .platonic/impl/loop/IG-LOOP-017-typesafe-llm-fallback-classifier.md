# IG-LOOP-017: TypeSafe Classifier → LLM Fast-Model Fallback

## Problem

When the TypeSafe classifier backend is unavailable, every station that depends
on it silently degrades to its "keep the tool / keep the default" fail-safe —
which for several stations means **no decision is produced at all**. Loop
`01a1bde2-…-83a1` made this visible:

- `TYPESAFE_API_KEY` was unset, so `TypeSafeClassifier(**client_kwargs)` raised
  a pydantic `value_error` ("TypeSafe API key is required").
- `build_typesafe_classifier` caught it and returned `None`
  (`typesafe_client.py:106-108`), emitting a WARNING per call.
- Every consumer hit `if classifier is None: ... return None` and bailed.
- `[decompose] reset runtime ... clarifications_queued=0` on every step — even
  `WUD-EVAL` and `JQQ-EVAL` where `proposals_queued=1`.
- Zero `clarification_requested` / `await_clarification` / `relay.inbox` events.
- The clarification popup never fired for the whole loop.

`typesafe_client.py` documents the invariant as "unavailable is normal … the
caller degrades to its existing LLM-based behaviour instead of blocking." But
for most stations there is **no** existing LLM behaviour behind the `None` —
the `None` path is the fail-safe default (`"split"` / `"keep tool"` /
`"complex fallback routing"`), not an LLM classification. The TypeSafe
classifier is the only thing that produces a `Choice` verdict; without it the
decision is skipped, not substituted.

IG-LOOP-015 explicitly deferred this as out-of-scope ("`TYPESAFE_API_KEY`
outage remediation — separate ops issue"). This doc picks up that deferred
item.

## Goal

When the TypeSafe backend is unavailable (missing key, transport error, bad
endpoint, missing dependency), substitute a **structural LLM classifier** built
on the configured `fast` model role — same `ainvoke(ClassifierRequest) ->
ClassifierResponse` interface, same `Choice` answer shape (`choice`,
`confidence`, `probabilities`) — so the 6 shared-helper consumers keep
producing verdicts instead of bailing to the fail-safe.

Non-goals (see Out of scope):

- Veritas (`subagents/veritas/decision_model.py`) already has its own
  "Jev first, LLM second" fast path with a circuit-breaker; it is unaffected.
- Making the LLM verdict as calibrated as TypeSafe. The LLM path is a
  best-effort stand-in; `verdict_trusted` still gates low-confidence and
  near-tie verdicts, so untrusted LLM verdicts fall back exactly as untrusted
  TypeSafe verdicts do.

## Design

### `LLMChoiceClassifier` adapter

New module `sloop/clarification/llm_classifier.py`. A single class that
implements the `TypeSafeClassifier` contract used by every consumer:

```python
class LLMChoiceClassifier:
    """TypeSafe-compatible `Choice` classifier backed by the fast LLM model.

    Stand-in for `TypeSafeClassifier` when the TypeSafe backend is
    unavailable. Resolves the `fast` model role via
    `soothe_config.create_chat_model("fast")` and answers `ClassifierRequest`s
    with structured output, producing `ChoiceAnswer`s with `choice`,
    `confidence`, and `probabilities` so `verdict_trusted` works unchanged.
    """

    def __init__(self, model: BaseChatModel, *, timeout: float = 8.0): ...
    async def ainvoke(
        self, input: ClassifierRequest, config: RunnableConfig | None = None, **_: Any
    ) -> ClassifierResponse: ...
```

`ainvoke` behaviour:

1. Build a single prompt from `input.state` (the decision context dict) plus
   each question's `Choice.instructions` and `Choice.criteria` (the label →
   description map). One prompt covers all questions in the request so there
   is one LLM round trip per `ainvoke` (TypeSafe makes one round trip too).
2. Request structured output with a JSON schema shaped as
   `{questions: {<id>: {choice: <label>, confidence: <0..1>, probabilities: {<label>: <0..1>, ...}}}}`.
   The schema's `enum` for each `choice` and `probabilities` key is derived
   from the question's `criteria` keys, so the LLM cannot invent labels.
3. Wrap each answer into `ChoiceAnswer(type="choice", choice=..., confidence=...,
   probabilities=...)` and assemble a `ClassifierResponse` exposing
   `.answers: dict[str, ChoiceAnswer]` keyed by question id.
4. On any LLM/parse failure, return a `ClassifierResponse` with **no answers**
   (`{}`). Consumers already treat `answers.get(id) is None` as "no answer;
   keep the default" (`decompose_typesafe.py:120-122`), so a failed LLM call
   degrades to the same fail-safe as a failed TypeSafe call — never raises.

Prompt construction follows `intake_classifier._classify_llm`'s precedent
(`SystemMessage` + `HumanMessage`, structured output via the shared
`invoke_structured_chat` / `await_with_llm_call_policy` helpers from
`soothe_nano.llm`) so rate-limit policy, observability tags, and retry policy
are consistent with the rest of the host. **No keyword heuristics** (Rule 9) —
the labels come from each consumer's `criteria` dict, the LLM does the
classification.

### `build_classifier` (renamed from `build_typesafe_classifier`)

`typesafe_client.py`:

- Rename `build_typesafe_classifier` → `build_classifier`. The name no longer
  lies: the returned object may be a `TypeSafeClassifier` **or** an
  `LLMChoiceClassifier`.
- Resolution order:
  1. If `resolve_classifier_provider` returns `None` (disabled / unconfigured /
     unsupported provider), return `None` — unchanged.
  2. Try `TypeSafeClassifier(**client_kwargs)`. On success, return it.
  3. On any construction failure (missing key, bad endpoint, missing dep),
     build an `LLMChoiceClassifier` from `soothe_config.create_chat_model("fast")`
     and return it. Log a WARNING once per process: `[classifier] typesafe
     unavailable; using LLM fast-model fallback (<model>)`. The fallback always
     engages — there is no opt-out.
  4. If the fast model cannot be resolved, return `None` — no fallback is ever
     worse than the fail-safe default.
- Delete the `build_typesafe_classifier` name (Rule 19 — no alias, no shim).

### Config

No new config field. The LLM fallback is unconditional — a TypeSafe outage
always falls back to the `fast` model role. `ClassifierConfig` lives in PyPI
`soothe_nano` (not editable in this monorepo) and is unchanged; the `fast`
model role is already part of `SootheConfig`'s model-role router.

### Consumer call sites (6 — mechanical rename + import)

All import `build_typesafe_classifier` and call it as
`build_typesafe_classifier(soothe_config)`. Rename to `build_classifier` at:

| File | Decision |
|------|----------|
| `rails/rail_typesafe.py` | LoopRail auto-pick |
| `sloop/decompose/decompose_typesafe.py` | finish vs split |
| `sloop/engine/completion/scenario_typesafe.py` | synthesis style |
| `sloop/intention/typesafe_intent.py` | intent routing label |
| `sloop/eval/typesafe_eval_decision.py` | coverage audit gate |
| `sloop/stations/decompose/root_eval_typesafe.py` | re-audit vs finalize |

No behavioural change at the call sites — they already call
`classifier.ainvoke(ClassifierRequest(...))` and read
`response.answers[id].{choice,confidence,probabilities}` then
`verdict_trusted`. The only change is the import name.

### Veritas is unaffected

`subagents/veritas/decision_model.py` constructs its `TypeSafeClassifier`
directly (not via `build_classifier`) and already implements "Jev first, LLM
second" with a circuit-breaker for permanent HTTP statuses (`_PERMANENT_HTTP_
STATUSES`). It is explicitly out of scope.

### `intake.py` is unaffected

`stations/preprocess/intake.py:124` (`if classifier is None`) guards the
`IntentClassifier` (the LLM-based intake classifier from
`intake_classifier.py`), not the TypeSafe classifier. Unchanged.

## Files

| Change | File |
|--------|------|
| Rename `build_typesafe_classifier` → `build_classifier`; add fallback resolution | `sloop/clarification/typesafe_client.py` |
| New `LLMChoiceClassifier` adapter | `sloop/clarification/llm_classifier.py` |
| Rename import + call (6 sites) | `rails/rail_typesafe.py`, `sloop/decompose/decompose_typesafe.py`, `sloop/engine/completion/scenario_typesafe.py`, `sloop/intention/typesafe_intent.py`, `sloop/eval/typesafe_eval_decision.py`, `sloop/stations/decompose/root_eval_typesafe.py` |
| Update `__all__` | `sloop/clarification/typesafe_client.py` |
| Tests: rename monkeypatch target + new fallback tests | `packages/soothe/tests/unit/sloop/eval/*.py`, `tests/unit/sloop/intention/*.py`, new `tests/unit/sloop/clarification/test_llm_classifier.py` |

## Cleansing (Rule 19)

Removed in this change (no compat shims, no aliases):

- The name `build_typesafe_classifier` — deleted from `__all__`, the module,
  and every import site. `build_classifier` is the only name.
- No `_v1` / `@deprecated` / `old = new` shims.

## Test plan

Tests live in `packages/soothe/tests/unit/sloop/`:

1. New `clarification/test_llm_classifier.py`:
   - `ainvoke` with a two-label `Choice` question returns a
     `ClassifierResponse` whose `.answers[id]` has `choice` ∈ labels,
     `confidence` ∈ [0,1], `probabilities` keyed by label.
   - LLM / structured-output failure → `.answers == {}` (no raise).
   - `choice` enum is locked to the `criteria` keys (LLM cannot emit a label
     not in the question).
2. Rename monkeypatch target `build_typesafe_classifier` → `build_classifier`
   in: `test_typesafe_eval_decision.py`, `test_rail_typesafe.py`,
   `test_scenario_typesafe.py`, `test_decompose_typesafe.py`,
   `test_root_eval_typesafe.py`, `test_typesafe_intent.py`.
3. New `test_build_classifier_fallback.py`:
   - TypeSafe construction raises → returns `LLMChoiceClassifier`.
   - TypeSafe raises + fast model unresolved / returns None → returns `None`.
   - Disabled classifier (`enabled=False`) → returns `None` (no LLM attempt).
   - The once-per-process WARNING fires exactly once across two engagements.
4. Existing "build_classifier raising should not block" tests
   (`test_rail_typesafe.py`, `test_root_eval_typesafe.py`,
   `test_decompose_typesafe.py`) — kept as-is. They monkeypatch
   `build_classifier` itself to raise, which bypasses the internal fallback
   (the fallback lives inside `build_classifier`); the consumer's try/except
   still catches the raise and returns its fail-safe default, which is the
   behaviour those tests assert. The fallback path is covered by (3).

Verify: `./scripts/verify_finally.sh` (lint, typecheck, tests, vulture).

## Observability

The 83a1 diagnosis showed the TypeSafe outage was only visible as a repeated
WARNING that operators could easily miss. `build_classifier` logs a single
WARNING per process when it engages the LLM fallback:

```
[classifier] typesafe unavailable (<reason>); using LLM fast-model fallback (<model spec>)
```

This makes "the calibrated backend is off, decisions are best-effort LLM" a
discoverable condition rather than a silent degradation.

## Out of scope

- Veritas fast path (`subagents/veritas/decision_model.py`) — already has LLM
  fallback + circuit-breaker; separate code path.
- Proactive pre-execution ambiguity detection (deferred in IG-LOOP-015).
- Calibrating the LLM `confidence` / `probabilities` to match TypeSafe's
  distribution-shift semantics. The LLM path is best-effort; `verdict_trusted`
  remains the gate.
- `TYPESAFE_API_KEY` provisioning itself (that is the operator's env/config
  fix; this change makes its absence non-fatal to the decision path).
