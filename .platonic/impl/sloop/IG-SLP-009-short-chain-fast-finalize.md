# IG-SLP-009: Short-Chain Fast Finalize

**Scope**: `packages/soothe/` (config + planning completion + eval decision + root_eval + messages + finalize + synthesis + tests)
**Source**: Derived from RFC-SLP-004
**Related RFCs**: RFC-SLP-003, RFC-LOOP-009

## Problem

Loop 46a7 wasted ~81s + 6 tool calls because the pipeline forced three
unnecessary layers after the first step already produced the deliverable:
(1) the typesafe eval decision was text-only and couldn't see structured
completion signals, so the LLM fallback misread the BRAINSTORM `ask_user`
gate as incompleteness; (2) `final_response` defaults to
`always_synthesize`, forcing synthesis and skipping `LEDGER_DIRECT`
entirely; (3) `LEDGER_DIRECT`'s gate (`plan_wave_count <= 1`) excluded
the 2-step linear chain; (4) no typesafe check that the final step output
is a valid goal report before passthrough.

## Solution

An integrated fast-finalize path with four layers:

1. **Config removal**: remove `final_response` / `always_synthesize`.
   `LEDGER_DIRECT` is always the first candidate.
2. **DAG-topology gate**: replace `plan_wave_count <= 1` with
   `_is_linear_chain(dag, max_depth)` checking single root, forward
   fanout <= 1, chain depth <= N, no decomposition children.
3. **Enhanced typesafe eval**: add structured signals
   (`close_report_requires_eval`, `step_kind`, `ask_user_interrupt`,
   `artifact_produced`) to the typesafe classifier state.
4. **Typesafe report validation**: before `LEDGER_DIRECT` passthrough, a
   typesafe classifier validates "is the final step output a valid goal
   report?" — if valid, passthrough; if not, synthesize.

## Changes

### `packages/soothe/src/soothe/config/models.py`

- Remove `AgenticFinalResponseMode` type alias.
- Remove `final_response` field and `_normalize_final_response` validator
  from the loop config model.
- Remove `normalize_agentic_final_response_mode` function.
- Existing configs with `final_response: always_synthesize` are silently
  ignored (Pydantic `extra=ignore` already handles unknown fields on
  update; the field removal just stops recognizing it).

### `packages/soothe/src/soothe/context/planning_completion.py`

- Remove the `always_synthesize` early-return branch in
  `determine_completion_strategy`.
- Add `_is_linear_chain(dag_nodes, max_depth: int) -> bool` helper:
  - Single root: exactly one node with `parent_step_id is None`.
  - No parallel branches: build forward-fanout map from `dependencies`;
    no node appears in more than one other node's `dependencies` list.
  - Chain depth <= `max_depth`: longest path in the dependency graph.
  - No decomposition children: no node has `parent_step_id` pointing to
    another node in the DAG (decomposition creates parent->child
    relationships that are not linear continuations).
- Update `_ledger_direct_eligible`: replace `plan_wave_count <= 1` with
  `_is_linear_chain(dag_nodes, max_depth=rules.linear_chain_max_depth)`.
  Accept `dag_nodes: dict[str, StepNode]` (or a `StepDAG`) as a new
  parameter.
- The `has_dag_dependencies` parameter is now derived from
  `_is_linear_chain` (parallel branches imply DAG dependencies). Keep it
  for backward compat but it's redundant when `_is_linear_chain` is used.

### `packages/soothe/src/soothe/config/models.py` (CompletionRulesConfig)

- Add `linear_chain_max_depth: int = Field(default=3, ge=1, le=10)`
  to `CompletionRulesConfig`.

### `packages/soothe/src/soothe/context/planning_step_planner.py`

- Update `determine_completion_strategy` adapter to pass the `StepDAG`
  nodes (or the `DagPlanningContext` topology info) to
  `determine_completion_strategy` so `_is_linear_chain` can run.
- Remove `plan_wave_count` from the call if it's no longer used (check
  `_dag_requires_synthesis` — it still uses `plan_wave_count >= 2` for
  the replan detection gate, so keep it there but remove it from
  `_ledger_direct_eligible`).

### `packages/soothe/src/soothe/sloop/eval/typesafe_eval_decision.py`

- Add structured fields to the `ClassifierRequest.state` dict:
  - `close_report_requires_eval: bool` — `any(n.close_report.requires_eval for n in completed_steps)` if close reports exist, else `True` (fail-safe).
  - `step_kind: str` — `last_completed_step.kind` value.
  - `ask_user_interrupt: bool` — whether the last step's execution output contains an `ask_user` / `AskUserQuestion` tool call.
  - `artifact_produced: bool` — whether the last step's outcome text matches file-path patterns.
- Update `_CRITERIA` dict to reference these signals (see RFC §5.3).
- These fields are extracted by the caller (`eval_decision.py`) and
  passed as keyword args to `decide_eval_coverage_typesafe`.

### `packages/soothe/src/soothe/sloop/eval/eval_decision.py`

- Add `_extract_structured_signals(step_history: list[StepNode]) -> dict[str, Any]`:
  - `close_report_requires_eval`: from `StepCloseReport.requires_eval`.
  - `step_kind`: from the last completed step's `kind`.
  - `ask_user_interrupt`: scan `step.execution.output_messages` for
    `ask_user` / `AskUserQuestion` tool call names.
  - `artifact_produced`: regex scan of `str(step.execution.outcome)` for
    file-path patterns (`\.platonic/`, `\.py$`, `\.md$`, etc.).
- Pass these to `_classify_eval_coverage` -> `decide_eval_coverage_typesafe`
  as keyword arguments.

### `packages/soothe/src/soothe/sloop/stations/decompose/root_eval.py`

- No structural change needed — `decide_eval_required` already receives
  `step_history=list(goal.steps.nodes.values())`. The structured signal
  extraction happens inside `eval_decision.py`.

### `packages/soothe/src/soothe/sloop/utils/messages.py`

- Add `last_step_output_content(state: LoopState, goal_steps: dict[str, StepNode] | None = None) -> str`:
  1. Find the last completed step (by `plan_iteration` desc, then `id` desc).
  2. Extract AI text from `step.execution.output_messages` (concatenate
     entries where `role == "assistant"` or `type == "ai"`).
  3. If empty, fall back to `last_ledger_ai_content(state)`.

### NEW `packages/soothe/src/soothe/sloop/engine/completion/report_validation_typesafe.py`

Typesafe report validation gate, following the pattern of
`scenario_typesafe.py` / `typesafe_eval_decision.py`:

```python
async def validate_report_typesafe(
    *,
    user_goal: str,
    step_output: str,
    step_kind: str | None = None,
    soothe_config: SootheConfig | None = None,
) -> bool | None:
    """Decide whether the final step output is a valid goal report.

    Returns True (valid -> passthrough), False (invalid -> synthesize),
    or None (unavailable/untrusted -> synthesize, fail-safe).
    """
```

- **Question ID**: `report_valid`
- **Choices**: `valid` / `invalid`
- **Asymmetry**: `valid` (suppress synthesis) requires
  `suppress_min_confidence`; `invalid` just needs trusted verdict.
- **Fallback**: None -> caller synthesizes.

### `packages/soothe/src/soothe/sloop/stations/completion/finalize.py`

- In the `LEDGER_DIRECT` branch (around line 657):
  1. Call `last_step_output_content(state, goal_steps)` instead of
     `last_ledger_ai_content(state)`.
  2. If content is non-empty, call `validate_report_typesafe(...)`.
  3. If `valid` -> use the step output as `final_output` (passthrough).
  4. If `invalid` or `None` (unavailable/untrusted) -> fall back to
     `SYNTHESIZE` (set `action = CompletionStrategy.SYNTHESIZE`).
  5. If step output is empty, fall back to `last_ledger_ai_content`, then
     to `SYNTHESIZE` (existing behavior).
- `goal_steps` comes from `ctx.ce.get_goal(ctx.ce_goal_id).steps.nodes`
  or from `state.step_results` when CE is not bound (tests).

### `packages/soothe/src/soothe/sloop/engine/completion/synthesis.py`

- No changes needed. Synthesis remains the fallback when
  `LEDGER_DIRECT` passthrough is rejected or unavailable.

## Tests (`packages/soothe/tests/unit/`)

### `tests/unit/sloop/eval/test_typesafe_eval_decision.py`

- Structured signals are correctly extracted from `StepNode` list:
  - `close_report_requires_eval=False` when all close reports have
    `requires_eval=False`.
  - `step_kind="brainstorm"` for BRAINSTORM-mode steps.
  - `ask_user_interrupt=True` when execution output contains an
    `ask_user` tool call.
  - `artifact_produced=True` when outcome text mentions file paths.
- Typesafe classifier receives the structured fields in
  `ClassifierRequest.state`.

### `tests/unit/context/test_planning_completion.py`

- `_is_linear_chain` returns True for single-step DAG, 2-step linear
  DAG, 3-step linear DAG.
- `_is_linear_chain` returns False for parallel DAG (two steps depend on
  one), decomposition DAG (child steps), depth > max_depth.
- `_ledger_direct_eligible` uses `_is_linear_chain` instead of
  `plan_wave_count <= 1`.
- `determine_completion_strategy` no longer has `always_synthesize`
  early-return; always tries `LEDGER_DIRECT` first.

### `tests/unit/core/loop/engine/completion/test_report_validation_typesafe.py`

- `validate_report_typesafe` returns True when classifier says `valid`
  (above suppression bar).
- Returns False when classifier says `invalid`.
- Returns None when classifier unavailable / untrusted / below bar.
- Passthrough suppression bar is applied to `valid` direction only.

### `tests/unit/core/loop/orchestrator/test_goal_completion_ledger.py`

- `LEDGER_DIRECT` with step output content + valid typesafe verdict ->
  passthrough (no synthesis stream).
- `LEDGER_DIRECT` with step output content + invalid typesafe verdict ->
  falls back to SYNTHESIZE.
- `LEDGER_DIRECT` with empty step output -> falls back to
  `last_ledger_ai_content`, then SYNTHESIZE.
- `LEDGER_DIRECT` with typesafe unavailable -> SYNTHESIZE (fail-safe).

### `tests/unit/sloop/utils/test_last_step_output_content.py`

- Returns AI text from the last completed step's `execution.output_messages`.
- Falls back to `last_ledger_ai_content` when step output is empty.
- Returns empty string when no steps or no output messages.

## Migration / Compatibility

- **Breaking**: `final_response` config field is removed. Configs that
  set `final_response: always_synthesize` will silently lose that
  setting (Pydantic ignores unknown fields on load). The behavior
  changes from "always synthesize" to "try LEDGER_DIRECT first" — this
  is the intended improvement.
- **No schema migration**: no `StepNode` or `StepCloseReport` schema
  changes. All structured signals are extracted from existing fields.
- **No persistence migration**: no stored state format changes.

## Coding Plan

### Task 1: Config removal
**Files**: `config/models.py`
Remove `AgenticFinalResponseMode`, `final_response` field, validator,
`normalize_agentic_final_response_mode`. Add
`CompletionRulesConfig.linear_chain_max_depth`.

### Task 2: DAG topology gate
**Files**: `context/planning_completion.py`, `context/planning_step_planner.py`
Add `_is_linear_chain`, update `_ledger_direct_eligible`, remove
`always_synthesize` branch, pass DAG nodes to strategy.

### Task 3: Enhanced typesafe eval signals
**Files**: `sloop/eval/typesafe_eval_decision.py`, `sloop/eval/eval_decision.py`
Add `_extract_structured_signals`, pass to typesafe path, update criteria.

### Task 4: LEDGER_DIRECT content source
**Files**: `sloop/utils/messages.py`
Add `last_step_output_content` helper.

### Task 5: Typesafe report validation
**Files**: NEW `sloop/engine/completion/report_validation_typesafe.py`
Implement `validate_report_typesafe` following `scenario_typesafe.py` pattern.

### Task 6: Finalize wiring
**Files**: `sloop/stations/completion/finalize.py`
Wire up `last_step_output_content` + `validate_report_typesafe` in the
`LEDGER_DIRECT` branch.

### Task 7: Tests
All test files listed above.

### Execution Order

1. Task 1 (config) — foundation, no runtime impact
2. Task 2 (DAG topology) — changes strategy selection
3. Task 3 (typesafe eval signals) — enhances eval decision
4. Task 4 (content source) — new helper
5. Task 5 (report validation) — new module
6. Task 6 (finalize wiring) — integrates all layers
7. Task 7 (tests) — verify each layer + integration

### Verification

- `./scripts/verify_finally.sh` before every commit.
- Unit tests: `pytest packages/soothe/tests/unit/sloop/eval/ packages/soothe/tests/unit/context/ packages/soothe/tests/unit/core/loop/engine/completion/ packages/soothe/tests/unit/core/loop/orchestrator/`
- Lint + typecheck per AGENTS.md.
