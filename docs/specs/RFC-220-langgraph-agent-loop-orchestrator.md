# RFC-220: LangGraph Agent Loop Orchestrator

**RFC**: 220
**Title**: LangGraph Agent Loop Orchestrator
**Status**: Implemented
**Kind**: Architecture Design
**Created**: 2026-05-05
**Authors**: Soothe Team
**Updated**: 2026-08-11
**Depends on**: RFC-000, RFC-001, RFC-100, RFC-604, RFC-218
**Supersedes**: RFC-201 §loop driver (imperative Plan → Execute driver)
**Partially Superseded By**: RFC-903 (node lifecycle, node folds, typed route contract); RFC-904 (recursive step decomposition — plan/eval/execute station spine)
**Related**: RFC-207, RFC-211, RFC-213, RFC-214, RFC-219

---

## Abstract

Layer 2 single-goal execution **must** be implemented as a **compiled LangGraph `StateGraph`** (the **Loop Graph**). The historical imperative `while`-loop driver described in RFC-201 is **removed**; there is **no** backward-compatible execution path, feature flag, or dual orchestrator.

The Loop Graph orchestrates assess → optional bounded evidence gathering → plan generation → **evidence validation** → execute → persistence → goal completion. Each graph invocation is keyed **only** by **`loop_id`** for LangGraph checkpointing and configurable routing. **CoreAgent** (Layer 1) remains a separate **CompiledStateGraph** keyed by **`thread_id`**. These two checkpoint namespaces **must never** share the same LangGraph thread/checkpoint key.

This RFC also mandates **evidence-bound plan steps**: every planned step references validated evidence identifiers before Execute proceeds.
It also defines graph-entry **intent classification** so conversational fast paths and normal loop execution share one topology.

> **Supersession note (RFC-904):** The assess → evidence → plan-generate → commit → execute → record → check_limits spine is replaced by DISPATCH / THREAD / RECONCILE / ROOT_EVAL. Identity rules (two graphs, two keys), CoreAgent isolation, and checkpoint keying in this RFC remain normative. See RFC-904.

> **Implementation Note (2026-08-11):** The LangGraph `StateGraph` orchestrator is fully implemented at `packages/soothe/src/soothe/sloop/orchestrator/builder.py` (`graph = StateGraph(LoopGraphState)`). No imperative `while`-loop driver remains — all `while` keyword occurrences in `sloop/` are iterators, network retries, and stream normalization. No backward-compatible execution path, feature flag, or dual orchestrator exists.

---

## Motivation

1. **First-class orchestration**: LangGraph provides native routing, checkpoint boundaries, stream modes, and interrupt semantics for the goal runner.
2. **Explicit isolation**: Today’s risk of conflating conversation thread state with loop-scoped state is eliminated by normative ID rules.
3. **Grounded planning**: Plan-generate must not emit unconstrained steps; a small bounded tool phase plus programmatic validation enforces traceability from steps to evidence (extends RFC-604 discipline).

---

## Guiding Principles

1. **Cut-over only** — One orchestrator implementation; remove the imperative loop and obsolete entry points that depended on it.
2. **Two graphs, two keys** — Loop Graph checkpoint identity = `loop_id`; CoreAgent checkpoint identity = `thread_id`. No exceptions.
3. **Protocol reuse** — Keep `PlannerProtocol` / assess + plan structured outputs (RFC-604); keep Executor semantics for parallel / sequential / dependency execution unless a future RFC narrows them.
4. **Evidence before commitment** — Steps are not executable until validation passes (or an explicit repair cycle completes within caps).

---

## Supersedes and Obsolete Surface

When RFC-220 is **Implemented**:

- RFC-201 remains valid for **conceptual** Layer 2 responsibilities (single goal, PlanResult, delegation to CoreAgent) but its **imperative loop construction** is **obsolete**.
- Code paths that expose “StrangeLoop as a hand-written async generator loop” are **deleted**, not deprecated.
- Documentation that describes Layer 2 as a Python `while` loop **must** be updated to the Loop Graph model.

Downstream specs (RFC-203, RFC-214, RFC-207, RFC-217) **must** be reconciled in the same implementation batch so they do not assume the removed driver.

---

## Architecture Position

```
Layer 3 (RFC-200) → delegates single goal to Layer 2
Layer 2 (this RFC) → CompiledStateGraph (Loop Graph), thread_id = loop_id for THIS graph only
    └── Execute node → invokes CoreAgent CompiledStateGraph with thread_id = conversation thread
Layer 1 (RFC-100) → tools / subagents
```

---

## Normative Identity and Isolation Rules

| Artifact | Checkpoint / LangGraph `thread_id` | Purpose |
|----------|-----------------------------------|---------|
| **Loop Graph** | **`loop_id`** | Resume orchestration, iteration boundaries, loop persistence correlation |
| **CoreAgent graph** | **`thread_id`** | Conversation transcript, Layer 1 checkpoint under `data/threads/{thread_id}/` |

**Hard requirements:**

1. The **string** passed to the Loop Graph’s LangGraph checkpointer as configurable `thread_id` **must** equal **`loop_id`** (semantic name in docs: **loop checkpoint key**).
2. The **`thread_id`** field on **`LoopState`** is **only** the CoreAgent conversation identifier passed into Execute; it **must not** be used as the Loop Graph’s checkpoint key.
3. Implementations **must** document the pair `(loop_id, thread_id)` on each run for debugging; tests **must** fail if a developer wires CoreAgent’s checkpoint key to `loop_id` or the Loop Graph’s key to `thread_id`.

Files on disk remain aligned with existing layout: loop runtime under **`$SOOTHE_HOME/data/loops/{loop_id}/`**, thread runtime under **`data/threads/{thread_id}/`** (RFC-803).

---

## Loop Graph Topology

> **Removed 2026-10-07 — superseded by RFC-904 / RFC-903.** The historical
> plan/eval/execute station spine (`init_or_resume`, `iteration_start`,
> `intent_fast_path`, `bounded_evidence_gather`, `plan_assess`,
> `plan_pre_generate`, `plan_generate`, `validate_evidence_bindings`,
> `execute`, `record_iteration`, `goal_completion`) is replaced by the
> DISPATCH / THREAD / RECONCILE / ROOT_EVAL work-queue (RFC-904) on the
> `LoopNode` / `RouteDecision` contract (RFC-903). See RFC-904 §Topology.

---

## State and Schemas

### Loop graph state

- Carries **`LoopState`** fields required for planning and execution.
- Adds **`evidence_ledger: list[EvidenceEntry]`** (exact shape defined in implementation; must include stable **`evidence_id`**, provenance, and compact summary).
- Adds **`validation_feedback`** / **`repair_round`** counters for bounded repair.

### Step schema extension

Each **`StepAction`** includes **`evidence_refs: list[str]`** (non-empty when the ledger for this iteration is non-empty).

Validation **rejects** plans where any step violates binding rules.

---

## Bounded Evidence Gathering

> **Removed 2026-10-07 — superseded by RFC-904.** The `bounded_evidence_gather`
> station and its evidence cap / allowlist / gather-output contract are
> obsolete; evidence grounding folds into THREAD self-hygiene prompts and the
> executor `read_only_streak_limit` backstop (RFC-904 §`decompose_task` Tool).

---

## Persistence Strategy

**Cut-over simplification:** Implementation **may** consolidate loop orchestration persistence into **one** authoritative mechanism:

- Preferred: LangGraph checkpointer for the Loop Graph keyed by `loop_id`, with `StrangeLoopStateManager` adapted as the serializer/deserializer for loop checkpoint rows **or** migration of stored rows into the LangGraph store — **single writer**, no duplicate iteration records.

Exact consolidation is specified in the Implementation Guide; this RFC requires **no** duplicate conflicting sources of truth after implementation completes.

---

## Streaming and Observability

The runner **consumes** `compiled.astream` (and compatible modes) from the Loop Graph and maps stream chunks to existing progress contracts (`RFC-614`, event catalog). Execute-phase suppression rules (e.g. IG-304) **remain**; breaking changes to client payloads are allowed **only** if RFC-614 / event catalog updates ship in the same change batch.

Intent classification executed in the graph entry node **must** attach Langfuse metadata consistent with loop tracing (`component=strange_loop.intent_classification`, `phase=strange_loop_graph`, `loop_id`, `thread_id`) so classifier spans are correlated with the same session trace as plan/execute nodes.

---

## Configuration

New configuration keys are introduced for evidence caps, allowlists, repair bounds, and gather skip policies. **`config/config.template.yml` and `config/develop/nano.yml` must be updated together** when defaults are added.

---

## Testing Obligations

- Unit tests per graph node with mocked CoreAgent and planner.
- Integration tests: resume mid-goal, max iterations, fatal execute error, validation failure repair paths.
- **Isolation tests**: assert Loop Graph `thread_id` ≠ CoreAgent `thread_id` at runtime; assert checkpoint DB paths / keys do not collide.

---

## Non-Goals (this RFC)

- Replacing deepagents / CoreAgent internals.
- Changing GoalEngine protocol shapes.
- Preserving API compatibility with pre-RFC-220 runner entrypoints.

---

## Implementation

Follows [IG-394](../archive/impl/IG-394-langgraph-agent-loop-orchestrator.md) and [IG-396](../archive/impl/IG-396-rfc-220-loop-graph-topology-langfuse.md): implement Loop Graph + delete imperative loop, reconcile dependent RFCs and docs in the same merge series, run `./scripts/verify_finally.sh`, and update RFC status to **Implemented**.

---

## Summary

RFC-220 normatively defines Layer 2 as a **LangGraph Loop Graph** keyed by **`loop_id`**, strictly isolated from CoreAgent’s **`thread_id`** graph, with **mandatory evidence-bound steps** and **no backward compatibility** with the imperative RFC-201 loop driver.

---

## Changelog

### 2026-10-07
- Removed superseded Loop Graph Topology (plan/eval/execute station spine) and Bounded Evidence Gathering sections (per RFC-904 / RFC-903). Identity/isolation rules, checkpoint keying, and CoreAgent isolation retained as normative.
