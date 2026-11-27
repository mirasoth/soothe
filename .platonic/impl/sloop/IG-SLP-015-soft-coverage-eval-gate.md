# IG-SLP-015: Soft coverage-eval gate (L1 signal softening + L2 decision-model gate)

## Context

ROOT_EVAL inserts a coverage Eval step (`kind="eval"`) when the action tree goes
green. Three gates coexist today (`root_eval.py`, first-Eval branch):

| Complexity | Gate | Hardness |
|---|---|---|
| MINIMAL | skip | hard skip |
| SIMPLE | typesafe classifier → LLM `decide_eval_required` (fail-safe run) | soft |
| COMPLEX | force Eval | hard |
| other / unknown, single leaf, `eval_required()` False | unconsumed audit PCE signals → force; else skip | hard |

Measured incident (loop `01a21c45-776f`, 2026-11-27): a 37.5 s silent vision tool
call fired PCE `stalled conf=0.68`; `stalled` unconditionally forces an audit, so a
9.6 s Eval + 5.8 s summary ran even though the monitor's immediately following
signal was `goal_likely_complete conf=0.90`. ~15 s wasted on a goal the monitor
itself was 90% confident was complete.

## Design

Two independently landable layers; both land here.

### L1 — Signal-level softening (no LLM)

1. `stalled` loses its unconditional audit right (`context/models.py`):
   `AUDIT_REQUIRING_SIGNAL_KINDS` drops `stalled`; a new
   `STALLED_AUDIT_MIN_CONF = 0.75` bar applies via `requires_audit()`. Rationale:
   `stalled` is a silence/heartbeat heuristic — long tool calls (vision, batch
   jobs) fire it at low confidence routinely. `coverage_gap` / `runaway_step` /
   `scope_drift` are semantic detections and keep their unconditional right.
2. Recency override in `_pce_audit_signals` (`root_eval.py`): an older `stalled`
   signal is suppressed when a newer unconsumed `goal_likely_complete` with
   `conf ≥ GOAL_LIKELY_COMPLETE_AUDIT_BAR` exists — the monitor's later verdict
   supersedes its earlier noise. Suppressed signals stay unconsumed (they remain
   evidence lines in the eval envelope).

### L2 — Decision-model gate for the medium/single-leaf branch

The non-SIMPLE, non-COMPLEX single-leaf branch no longer hard-forces on audit
signals. Instead the audit signals (after L1 softening) become evidence for the
existing soft chain `decide_eval_required` (typesafe Choice classifier first —
zero LLM round trip — then structured LLM decision; fail-safe runs the Eval).

`decide_eval_required` grows an optional `pce_evidence: list[str]` parameter:
the lines are added to the typesafe classifier state (`pce_signals`) and to the
LLM human message as an untrusted-evidence section. The asymmetric trust gate is
unchanged and deliberately so: an `eval` verdict is trusted at any confidence
(same direction as the fail-safe), a `no_eval` verdict must clear
`suppress_min_confidence` — a wrong skip ships an unaudited goal, a wrong run
costs seconds.

On decision, the audit signals are consumed (`consumed_by="root_eval"`) whether
the gate runs or skips the Eval, mirroring the SIMPLE path's semantics.

COMPLEX single-leaf goals keep the hard force (multi-area work warrants the
audit), and the multi-leaf `eval_required()` path is untouched.

## Cost model

- Eval step ≈ 10 s LLM verdict + (pre-IG change) ≈ 5 s summary + insert/dispatch
  overhead; eval steps now also skip the completion summary (executor
  `kind="eval"` early return).
- False skip: unaudited goal — mitigated by the asymmetric classifier bar and by
  keeping semantic gap signals hard.
- L1 alone deterministically removes the stalled-noise class; L2 covers the
  residual gray zone with a calibrated probability.

## Tests

- `test_root_eval_pce.py`: stalled below bar does not force; stalled above bar
  alone forces; newer high-confidence `goal_likely_complete` suppresses older
  `stalled`.
- New `test_root_eval_soft_gate.py`: soft gate consulted only on the
  medium/unknown single-leaf path; skip → finalize (signals consumed, no eval
  node); run → insert; COMPLEX bypasses the model; L1-guaranteed skips do not
  call the model.
- `test_eval_decision.py`: `pce_evidence` reaches the LLM prompt and typesafe
  state.
