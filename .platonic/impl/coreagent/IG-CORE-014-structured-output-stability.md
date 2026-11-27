# IG-CORE-014 — Structured-output stability for non-json_schema endpoints

## Problem

Loop `01a21bfe…` (plan mode) hung for ~9 minutes in `assess_step_close` before
failing: 8 consecutive structured LLM calls (44–104 s each, 8–13 k output
tokens) all failed schema validation. Root cause chain (reproduced live
against the active `hunyuan` profile, `openai/hy4-flash` via tokenlink):

1. **P1 — schema never reaches the model.**
   `ChatLitellmModel.with_structured_output` ignores the `method` kwarg. For
   providers with `supports_json_schema=False` (all custom OpenAI-compatible
   endpoints) the fallback `_StructuredOutputRunnable` sends **no tools, no
   `response_format`, and no schema in the prompt** — the model has no idea
   which fields are expected, so it invents field names
   (`goal_closed`/`confidence`/`reason` instead of
   `goal_portion_complete`) or emits broken JSON.
   Control experiment: embedding the schema JSON in the prompt +
   `response_format={"type": "json_object"}` returned exactly-correct fields
   5/5 times with ~600 completion tokens.

2. **P2 — method chain is a false fallback ladder.**
   `invoke_structured_chat` walks `function_calling → None → json_schema →
   json_mode` × 2 attempts each. On `ChatLitellmModel` all four methods land
   on the *same* fallback runnable, so the storm is 8 equivalent re-rolls of
   a broken call, each paying full thinking-model latency.

3. **P3 — all-default schemas defeat post-validation.**
   `StepCloseReport` had defaults on every field → empty `required`, no
   `additionalProperties: false` → hallucinated payloads *pass* jsonschema
   and yield default-filled garbage; only randomly-worse payloads fail and
   trigger the P2 storm.

## Fix

| # | Layer | Change |
|---|-------|--------|
| N1 | nano `llm/registry.py` | `ProviderCapabilities.supports_json_object: bool = True` (json_object is the most widely honored constraint across OpenAI-compatible endpoints) |
| N2 | nano `llm/provider.py` | Fallback structured output = append a compact schema instruction message + attach `response_format={"type": "json_object"}` when `supports_json_object`; applies to both `ChatLitellmModel` and `MultiModelChatModel` |
| N3 | nano `llm/structured.py` | (a) collapse the method ladder to a single fallback method for capability-flagged nano models (`capabilities.supports_json_schema is False` → one method × repair attempts, not 4×2); (b) validation-failure debug logs carry the error detail and received keys; (c) new `deadline_s` total wall-clock budget that aborts the ladder with `StructuredOutputError` |
| N4 | nano `llm/traced.py` | `ainvoke_structured_traced` forwards `deadline_s` |
| S1 | soothe `context/models.py` | `StepCloseReport.goal_portion_complete` / `.early_exit` become required (no default) → hallucinated payloads are rejected immediately with a field-name repair hint |
| S2 | soothe `sloop/eval/step_close_report.py` | `assess_step_close` passes `deadline_s=120` (fail-closed fallback already exists; the assessment must never hold the loop hostage) |

## Expected outcome

- Planning-phase assessment calls on hy4-flash return correct fields in one
  attempt (~10 s) instead of a 9-minute storm; worst case the deadline
  bounds the wait at 120 s and the loop proceeds fail-closed.
- Hallucinated payloads can no longer silently pass validation.

## Tests

- nano: fallback runnable embeds schema + json_object; method-ladder
  collapse; deadline abort; error-detail logging.
- soothe: `StepCloseReport` required fields (update the two bare-constructor
  tests); `assess_step_close` passes the deadline.

## Verification

- `soothe-nano`: `make lint` green; `make test-unit` 2256 passed / 46 skipped
  (7 new tests); live re-probe against the real `hunyuan:hy4-flash` endpoint:
  full `invoke_structured_chat` returns exactly-correct `StepCloseReport`
  fields 6/6 rounds in 3.2–4.6 s (baseline: 44–104 s per attempt with
  hallucinated fields, 8-attempt storm).
- `soothe`: `./scripts/verify_finally.sh` — lint / vulture / soothe /
  soothe-daemon green; the 2 soothe-cli failures
  (`test_stream_cards_use_horizontal_inset_padding`) come from a parallel
  session's in-flight TUI CSS WIP (`widgets/messages/assistant.py`), not from
  this change.

## Release order (blocking)

`soothe` resolves `soothe-nano` from PyPI. The `deadline_s` kwarg requires
soothe-nano ≥ 2.1.1, so the landing order is:

1. Publish soothe-nano 2.1.1 (Unreleased section already updated).
2. Bump `packages/soothe/pyproject.toml` floor to `soothe-nano>=2.1.1` and
   re-lock (`uv lock`), then restart the daemon (editable installs pick up
   the soothe-side source change, but the venv's nano wheel does not until
   re-synced).
