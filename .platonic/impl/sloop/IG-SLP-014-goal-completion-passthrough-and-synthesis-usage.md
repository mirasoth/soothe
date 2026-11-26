# IG-SLP-014: Goal-Completion Passthrough and Synthesis Usage Follow-up

**Scope**: `packages/soothe/` (completion finalize + tests)
**Source**: Loop `01a21229-ee55-7182-b4a7-523c9c93fd06` replay (2026-11-25)
**Related**: IG-SLP-013 (report first-char latency), IG-LOOP-019 (execution token statistics)

## Problem

After the fast-synthesis-model and eval-aware topology fixes landed, the trivial
goal "create `/tmp/appr-1.txt` containing `hello dock`" still showed two gaps:

1. **Report TTFT ~19s instead of ~0s**. The eval filter worked — the strategy
   now returns `LEDGER_DIRECT` where it previously returned `SYNTHESIZE` — but
   `finalize` re-gates the passthrough text with the typesafe report classifier,
   which correctly rejects a tool recap as "not a substantive report"
   (`invalid`, conf=0.94). `_toolless_single_leaf` excludes tool-backed steps,
   so the only existing bypass does not apply. Result: a full synthesis call on
   the fast model with a 19.021s first-char delay (down from 160.9s, still not
   the zero-synthesis target of IG-SLP-013).
2. **Synthesis tokens never reach the persisted total**. Execute waves
   accumulate usage in `LoopState.total_tokens_used`; the goal-completion
   synthesis stream is a direct LLM call on an isolated checkpoint thread and
   its chunks are only consumed for text. `update_goal_tokens` therefore
   refreshes the goal snapshot with the pre-synthesis number, so the checkpoint
   and the `completed` wire event both miss every goal's final LLM call.

## Solution

1. **Structural passthrough gate** — add `_simple_verified_leaf` in
   `finalize.py`: skip report validation when the CE DAG has exactly one
   completed non-eval root leaf, no failed steps, no decomposition
   dependencies, no subagent completions, no earlier deliverable in
   `execution.outcome["prior_ai_output"]`, and tool calls within the new
   `CompletionRulesConfig.ledger_direct_passthrough_max_tool_calls` (default 4,
   `0` disables). The gate is purely structural (same vocabulary as
   `_ledger_direct_eligible` in `planning_completion.py`), resolves the
   operator's rules via `_completion_rules_from`, and only applies when the
   strategy already chose `LEDGER_DIRECT`. Replans, multi-step/parallel/failed
   DAGs, tool-heavy steps, eval-dependent goals and subagent work keep the
   classifier and its suppression bar. No content or keyword heuristics are
   introduced.
2. **Fold synthesis usage into the loop total** — observe the tagged synthesis
   chunks with `TokenUsageAccumulator` (dedupes by generation id, so a terminal
   usage-only chunk cannot double-count), then add the observed
   `total_tokens` to `LoopState.total_tokens_used` before
   `update_goal_tokens` and the `completed` emit. Capture the pre-stream total
   and add only the un-accounted remainder, so a token-usage callback that
   already folded the same call in cannot double-count. With no usage metadata
   the snapshot is `None` and accounting is unchanged.

## Changes

### `packages/soothe/src/soothe/config/models.py`

- `CompletionRulesConfig.ledger_direct_passthrough_max_tool_calls` (default 4).

### `packages/soothe/src/soothe/sloop/stations/completion/finalize.py`

- Import `CompletionRulesConfig` and `TokenUsageAccumulator`.
- `_completion_rules_from(strange_loop)`: typed config resolution with the
  built-in default as fallback.
- `_simple_verified_leaf(nodes, rules)`: deterministic structural gate.
- `bypass_validation` becomes `_toolless_single_leaf(...) or _simple_verified_leaf(...)`;
  log names the structural bypass.
- `SYNTHESIZE` branch observes usage chunks and reconciles the loop total before
  the terminal emit.

### Tests (`packages/soothe/tests/unit/`)

- `core/loop/engine/execute/test_executor_toolless_gates.py`: single
  tool-backed leaf with an eval node passes; multi-leaf, failed step, subagent
  completion, dependency, and over-cap tool count all reject.
- `core/loop/orchestrator/test_goal_completion_ledger.py`: finalize bypasses
  the classifier for a single verified tool-backed step; a synthesis stream
  carrying `usage_metadata` raises the `completed` total and the goal snapshot.

## Verification

- Targeted: `packages/soothe/tests/unit/core/loop/orchestrator/test_goal_completion_ledger.py`,
  `packages/soothe/tests/unit/core/loop/engine/execute/test_executor_toolless_gates.py`,
  `packages/soothe/tests/unit/sloop/test_token_usage.py`.
- `./scripts/verify_finally.sh` before any commit.
- Live replay after `soothe-nano>=2.0.14` is installed and the daemon is
  restarted: trivial goal completes with `action=ledger_direct passthrough`
  (~0s report latency), and a synthesis-requiring goal reports real provider
  usage with a terminal total larger than its last execute-wave snapshot.
