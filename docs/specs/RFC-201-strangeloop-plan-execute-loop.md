# RFC-201: StrangeLoop Plan-Execute Loop Architecture

**RFC**: 201
**Title**: StrangeLoop Plan-Execute Loop Architecture (Consolidated)
**Status**: Implemented (Partially Superseded)
**Partially Superseded By**: RFC-220 (§loop driver), RFC-222 (GoalEngine daemon-ownership), RFC-225 (loop-centric model), RFC-904 (recursive step decomposition — upfront plan waves)
**Kind**: Architecture Design
**Created**: 2026-04-17
**Authors**: Soothe Team
**Updated**: 2026-08-19
**Depends on**: RFC-000, RFC-001, RFC-100
**Related**: RFC-203, RFC-207, RFC-213, RFC-219, RFC-220

---

## Supersession Summary

| Section | Status | Superseded By |
|---------|--------|---------------|
| §Loop driver | Deprecated | RFC-220 (LangGraph orchestrator) |
| §GoalEngine integration | Modified | RFC-231 (loop-rail daemon-ownership model; absorbs legacy RFC-222) |
| §Thread continuation | Replaced | RFC-225 (loop-centric derivation) |
| §Goal completion flow | Extracted | RFC-219 (GoalCompletionModule) |
| §Plan assessment | Enhanced | RFC-213 (two-phase reasoning) |
| §Plan-Execute upfront plan waves | Partially superseded | RFC-904 (goal-as-root + do-or-decompose) |

**Preserved sections**: CoreAgent delegation, evidence accumulation (until RFC-904 cutover retires evidence stations)

---

## Abstract

This RFC defines Layer 2 of Soothe's three-layer execution architecture: agentic goal execution for single-goal completion through iterative refinement. Layer 2 uses a **Plan → Execute** loop where the LLM performs planning, progress assessment, and goal-distance estimation in a single structured response (PlanResult), then executes steps via Layer 1 CoreAgent. This RFC consolidates the core loop architecture including Plan-Execute loop structure, AgentDecision batch execution model, and PlanResult goal-directed evaluation model.

---

## Architecture Position

### Three-Layer Model

```
Layer 3: Autonomous Goal Management (RFC-200) → Layer 2 (PERFORM stage)
Layer 2: Agentic Goal Execution (this RFC) → Layer 1 (Execute phase)
Layer 1: CoreAgent Runtime (RFC-100) → Tools/Subagents
```

**Layer 2 Responsibilities**:
- Single-goal focus with iterative refinement
- LLM-driven reasoning through PlanResult
- Evidence accumulation and goal-directed evaluation
- Adaptive execution with strategy reuse
- Context isolation and execution bounds
- Layer 1 delegation via CoreAgent execution

### Integration with Layer 3

**StrangeLoop Goal Pull Architecture** (Inverted Control Flow):

StrangeLoop actively queries GoalEngine for goal assignment and reports execution results. GoalEngine provides goal state service, never invokes StrangeLoop.

**Integration Pattern**:

```python
# StrangeLoop initialization (run_with_progress)
async def run_with_progress(...):
    # PULL: StrangeLoop queries GoalEngine for current goal
    goal_engine = config.resolve_goal_engine()
    current_goal = goal_engine.get_next_ready_goal()
    
    if not current_goal:
        return None
    
    # Execute Layer 2 loop (StrangeLoop drives)
    state = LoopState(
        current_goal_id=current_goal.id,
        goal_text=current_goal.description,
        ...
    )
    
    plan_result = await self.run_iteration(state)
    
    # REPORT: StrangeLoop reports result to GoalEngine
    if plan_result.status == "done":
        goal_engine.complete_goal(current_goal.id, plan_result)
    elif plan_result.status == "failed":
        evidence = EvidenceBundleBuilder().build_from_plan_result(...)
        await goal_engine.fail_goal(current_goal.id, evidence)
    
    return plan_result
```

**Integration Contract**:

| Trigger | StrangeLoop Action | GoalEngine Response |
|---------|------------------|---------------------|
| Goal assignment | `get_next_ready_goal()` | Return DAG-satisfied goal |
| Goal completion | `complete_goal(goal_id, plan_result)` | Update goal status |
| Goal failure | `fail_goal(goal_id, EvidenceBundle)` | Apply BackoffReasoner |

**Architectural Principle**: StrangeLoop owns execution timing, GoalEngine provides goal state service (inverted control flow, no active PERFORM delegation).

### Integration with Layer 1

**Layer 2 → Layer 1**: `result = await core_agent.astream(input, config)` for step execution.

**Layer 1 → Layer 2**: CoreAgent returns streaming execution results for evidence accumulation.

### Adaptive final user response (IG-199)

When the Plan phase returns `status: done`, StrangeLoop must produce the user-visible completion text. Two strategies exist:

1. **Reuse last Execute assistant text** (`ledger_direct`): After each Execute wave, assistant-visible text is recorded in the CE ledger. Used only when `final_response: auto` and structural gates pass (single plan wave, no hard failures, step count within cap, last-wave tool calls ≤ `ledger_direct_max_tool_calls`). See RFC-219 / IG-580.
2. **Final thread synthesis** (`synthesize`): A CoreAgent turn produces a consolidated report. Used when the planner sets `require_goal_completion=True`, DAG complexity vetoes apply, the last wave exceeded the tool-call budget, or ledger text is empty.

Configuration (`agent.loop.final_response`): `auto` (default) applies the structural policy above; `always_synthesize` always runs the report turn. Legacy alias: `adaptive` → `auto`. Removed modes: `always_last_execute` (pre-IG-299).

### Architectural Role Clarification

**Important**: StrangeLoop is the **Layer 2 Plan → Execute loop runner**, not a consciousness module or knowledge accumulator. Its responsibilities are execution orchestration and iterative refinement, not knowledge persistence.

**Architectural Separation**:
- **StrangeLoop**: Plan → Execute loop runner (iterations)
- **ContextProtocol**: Consciousness/knowledge ledger (unbounded context accumulation)
- **GoalEngine**: Goal lifecycle manager (DAG management, goal status)
- **Executor**: StrangeLoop component for thread coordination

**Why This Matters**: Brainstorming sessions sometimes confuse StrangeLoop with "consciousness" because it maintains execution history. However, consciousness (unbounded knowledge with bounded projections) lives in ContextProtocol, not StrangeLoop. StrangeLoop's history is iteration-scoped execution state, not global knowledge accumulation.

### Retrieval authority (StrangeLoop versus ContextProtocol)

**Architectural clarification**: Brainstorming sessions sometimes assign "unbounded retrieval authority" to StrangeLoop. RFCs clarify the ownership boundary:

**ContextProtocol ownership** (RFC-001, RFC-302):
- Append-only ledger semantics (unbounded knowledge accumulator)
- Persistence hooks (thread-level restore/persist)
- **Retrieval module implementation** (RFC-302 `ContextRetrievalModule`)
- Retrieval algorithm evolution behind stable API

**StrangeLoop operational authority** (this RFC):
- **When** to retrieve (iteration start, thread switch, goal dependency)
- **For which goal** (goal-centric retrieval via `retrieve_by_goal_relevance()`)
- **How** retrieved entries combine with GoalContextManager output and Plan/Execute prompts

**Integration**: StrangeLoop calls `ContextProtocol.get_retrieval_module().retrieve_by_goal_relevance(goal_id, execution_context, limit)` when building Plan/Execute context. Retrieval algorithm implementation details stay encapsulated in ContextProtocol, preserving architectural separation.

**Integration Pattern Example**:

```python
# StrangeLoop.Executor calls ContextProtocol retrieval module
retrieval = context.get_retrieval_module()
relevant_history = retrieval.retrieve_by_goal_relevance(
    goal_id=state.current_goal_id,
    execution_context={"iteration": state.iteration},
    limit=10,
)
# Combine with GoalContextManager output for Plan/Execute context
```

**Reference**: RFC-302 defines canonical retrieval API. RFC-001 §28-62 references RFC-302 as single authoritative retrieval specification.

### Dual Trigger Synchronization Ordering

StrangeLoop and GoalEngine (RFC-200) stay synchronized through **ordered complementary triggers** with precise timing guarantees.

**Trigger Types**:

**REACTIVE Trigger** (Event-Bound):
- Timing: Fired after execution boundaries (completion, failure, step completion)
- Purpose: Push evidence to GoalEngine immediately
- Direction: StrangeLoop → GoalEngine (push)
- Examples: complete_goal(), fail_goal(), event emission

**PULL Trigger** (Need-Based):
- Timing: Fired before decisions requiring goal context (Plan, after backoff, iteration boundaries)
- Purpose: Query GoalEngine for authoritative state
- Direction: StrangeLoop → GoalEngine (query)
- Examples: get_goal(), get_next_ready_goal(), ready_goals()

**Ordered Sync Sequence (Per Iteration)**:

| Step | Trigger | When | StrangeLoop Call | Purpose |
|------|---------|------|----------------|---------|
| 1 | **PULL #1** | Before Plan | `get_goal(goal_id)` | Get goal state (priority, dependencies) |
| 2 | PLAN | - | - | LLM reasoning with goal context |
| 3 | EXECUTE | - | - | Run steps, collect evidence |
| 4 | **REACTIVE #1** | Goal completion | `complete_goal()` | Mark goal completed |
| 5 | **REACTIVE #2** | Execution failure | `fail_goal(evidence)` | Handoff failure evidence |
| 6 | **PULL #2** | After backoff | `get_goal(goal_id)` | Check updated goal status |
| 7 | **REACTIVE #3** | Step completion | `emit_event()` | Observability (optional) |
| 8 | **PULL #3** | Before next iteration | `ready_goals()` | Check DAG consistency |

**Critical Ordering Constraints**:

1. **PULL before Plan (mandatory)**: Planning requires authoritative goal state. Violation: stale goal context, wrong priority order.
2. **REACTIVE after execution (immediate)**: GoalEngine needs evidence for DAG decisions. Violation: state stale during reflection.
3. **PULL after backoff (before continuing)**: Backoff may reset goal status. Violation: StrangeLoop continues on inactive goal.
4. **PULL before iteration boundary**: Reflection may add dependencies. Violation: executing goal with unsatisfied dependencies.

**Race Condition Handling**:

- **External DAG mutation**: PULL #1 detects status change → abort iteration
- **Parallel threads**: Each thread pulls independently, GoalEngine atomic updates
- **Backoff while executing**: PULL #2 detects goal "pending" → end iteration
- **Reflection adds dependency**: PULL #3 detects goal not ready → defer iteration

**Contract**: Synchronization is intentionally hybrid (PULL + REACTIVE) with ordering guarantees for consistency.

### Execute-time packaging (`config.configurable` versus TaskPackage)

The normative interchange for passing execution hints into CoreAgent today is **LangGraph `config.configurable`** (see Executor integration in this RFC). A **documentary alternative** is assembling a single **`TaskPackage`** object (goal briefing, history snippets, backoff evidence, and `StepAction`) and mapping it into config before `astream`. Either pattern satisfies "direct provisioning" from design brainstorms; promoting `TaskPackage` to a required wire type would be a separate RFC change.

---

## Plan-Execute Loop Model

> **Removed 2026-10-07 — superseded by RFC-220 / RFC-904.** The imperative
> `while iteration < max_iterations: PLAN → EXECUTE` driver and the upfront
> plan-wave iteration flow (planner emits full PlanResult waves; reuse/replan
> decisions across waves) are obsolete. Layer 2 is a compiled LangGraph
> `StateGraph` keyed by `loop_id` (RFC-220); upfront plan waves are replaced by
> goal-as-root + recursive `decompose_task` (RFC-904).

---

## AgentDecision Model

> **Removed 2026-10-07 — superseded by RFC-904.** The batch plan-wave step
> emission model (`StepAction` / `AgentDecision` with 1-or-N steps,
> `execution_mode: parallel|sequential|dependency`, adaptive step granularity,
> cross-wave Step Anchor Registry wiring) is obsolete. Step creation folds into
> executor-bound `decompose_task` proposals reconciled by CE (RFC-904
> §`decompose_task` Tool / §CE Reconciliation). Step DAG lineage and
> `dependencies` continue to be normative under CE (RFC-624).

---

## PlanResult Model

> **Removed 2026-10-07 — superseded by RFC-904 / RFC-213.** The single-LLM-call
> `PlanResult` schema combining planning + progress assessment + goal-distance
> estimation + next steps (`status`, `goal_progress`, `confidence`,
> `plan_action`, `decision: AgentDecision`, `next_steps_hint`) is obsolete.
> Assessment folds into the RFC-905 Eval thread; plan generation folds into
> `decompose_task` (RFC-904). Goal-completion synthesis policy survives in
> RFC-219.

---

## PLAN Phase

> **Removed 2026-10-07 — superseded by RFC-220 / RFC-904 / RFC-213.** The
> iteration-scoped PLAN phase (reuse-vs-new planning logic, GoalContext
> construction for Plan via dependency-driven retrieval, Plan Metrics
> Enhancement) is obsolete. Planning folds into DISPATCH / THREAD do-or-decompose
> with CE-reconciled proposals (RFC-904); the two-phase assess+generate pair is
> removed (RFC-213). Goal-context construction and dependency-driven retrieval
> remain normative under CE projection (RFC-624 §3).

---

## EXECUTE Phase

### Hybrid Execution Modes

```python
async def execute(decision: AgentDecision, state: LoopState):
    if decision.execution_mode == "parallel":
        # RFC-207: All steps use parent thread_id (langgraph handles concurrency)
        results = await asyncio.gather([
            execute_step(step, thread_id=state.thread_id)
            for step in decision.steps
        ])
    elif decision.execution_mode == "sequential":
        combined_input = build_sequential_input(decision.steps)
        results = await core_agent.astream(combined_input, thread_id)
    elif decision.execution_mode == "dependency":
        results = await execute_dag_steps(scheduler, core_agent, thread_id)
```

### Context Isolation (Simplified by RFC-207)

**Subagent Steps**: Task tool creates isolated thread branches automatically (`{thread_id}__task_{uuid}` internally)

**Tool-Only Steps**: Use parent thread context (langgraph handles concurrent execution safely)

**Thread Safety**: Langgraph's atomic state updates and message queue prevent conflicts

**No Manual Thread ID Generation**: Executor passes parent thread_id to CoreAgent for all executions

### Execution Bounds

**Two-Layer Constraint**: Prevents runaway subagent loops.

**Soft Constraint**: Schema/prompt defines "one delegation = one call; retry = explicit second step"

**Hard Constraint**: `max_subagent_tasks_per_wave` cap (default 2) stops stream early. Cap hit signals metrics to Plan for replan/continue decision.

### Layer 1 Integration

**CoreAgent Config Injection**: Executor passes execution hints via `config.configurable` (thread_id, subagent hint, expected output).

**CoreAgent Responsibilities**:
- Execute tools/subagents
- Consider execution hints
- Apply middlewares
- Manage thread state
- Return streaming results

**Layer 2 Controls**:
- What to execute (AgentDecision.steps)
- Execution suggestions (subagent and expected-output hints)
- Timing and sequencing
- Thread isolation (automatic via RFC-207)
- Execution bounds (soft + hard cap)
- Metrics aggregation

---

## Contamination Prevention

### Cross-Wave Isolation

**Problem**: Wave 1 output contaminates Wave 2 delegation (e.g., research output causes translation language detection failure).

**Solution**: Thread isolation for delegation steps. Subagent sees only explicit task input, no prior wave outputs or conversation history.

**Mechanism** (simplified by RFC-207): Task tool automatically creates isolated thread branch for subagent delegations.

### Output Duplication Prevention

**Problem**: Subagent output streamed to TUI, then main model repeats it verbatim.

**Solution**: Output contract suffix (anti-repetition instructions) + metrics-driven Plan prevents premature `continue`.

**Mechanism**: Layer 2 contract suffix in executor. Better Plan decisions (metrics-aware) reduce post-delegation summary tendency.

### Execute-Phase Output Suppression Contract (IG-304)

Execute-phase assistant prose is internal orchestration output and should not be emitted as user-facing output events. StrangeLoop must:

1. keep tool activity observable via message-mode tool chunks/events,
2. emit final user-facing answer text through goal-completion output events only,
3. avoid relying on client-side suppression to hide execute-phase prose.

### Premature Continue Detection

**Problem**: Plan decides `continue` after satisfactory Execute output, triggering unnecessary iteration.

**Solution**: Structured metrics inform Plan of wave completion status. Output length, subagent count, cap hit signal done vs continue criteria.

**Mechanism**: `<SOOTHE_WAVE_METRICS>` section in Plan prompt. Model judges based on metrics pattern + goal text.

---

## Failure Evidence Handoff to GoalEngine

StrangeLoop does not own backoff policy. It produces high-fidelity execution evidence and hands it to GoalEngine, which owns backoff reasoning and DAG restructuring (encapsulated).

**Ownership Boundary**:
- **StrangeLoop (`RFC-201`)**: Produce execution evidence via EvidenceBundleBuilder, call GoalEngine.fail_goal()
- **GoalEngine (`RFC-200`)**: Define and execute GoalBackoffReasoner policy internally, apply BackoffDecision
- **Shared contract**: EvidenceBundle (RFC-200 §14-22) with structured + narrative fields
- **Encapsulation**: StrangeLoop never calls BackoffReasoner directly

### EvidenceBundle Contract

**EvidenceBundle Data Model** (RFC-200 §14-22 canonical structure):

**Structured Field**: Machine-readable execution metrics from LoopState wave tracking (§236-245)
- iteration: int
- wave_tool_calls: int (last_wave_tool_call_count)
- wave_subagent_tasks: int (last_wave_subagent_task_count)
- wave_errors: int (last_wave_error_count)
- wave_output_length: int (last_wave_output_length)
- wave_hit_subagent_cap: bool
- goal_progress: float
- confidence: float
- plan_status: str

**Narrative Field**: Natural language synthesis for GoalBackoffReasoner
- Synthesized from: PlanResult.reasoning, evidence_summary, user_summary
- Wave metrics pattern analysis: tool/subagent counts, error patterns, resource constraints

**Source**: "layer2_execute" or "layer2_plan" (evidence producer stage)
**Timestamp**: Evidence emission time

### Handoff Integration Architecture

**StrangeLoop → GoalEngine Flow**:

1. **Build Evidence**: StrangeLoop Executor constructs EvidenceBundle from execution context (PlanResult + LoopState wave metrics)
2. **Handoff**: REACTIVE trigger #2 - StrangeLoop calls GoalEngine.fail_goal(goal_id, evidence)
3. **GoalEngine Processing** (encapsulated):
   - Build GoalContext snapshot from goal DAG
   - Call BackoffReasoner.reason_backoff() with goal context + evidence
   - Apply BackoffDecision (DAG restructuring, reset backoff target to "pending")
   - Persist DAG mutation
4. **Next Iteration**: PULL #2 checks updated goal status → abort if "pending"

**Architectural Guarantee**: StrangeLoop hands off evidence, GoalEngine owns backoff reasoning (clear ownership boundary, no circular dependency).

---

## Stream Events

| Event | Description |
|-------|-------------|
| `soothe.cognition.strange_loop.started` | StrangeLoop execution began |
| `soothe.cognition.strange_loop.reasoned` | Plan/assessment progress summary event |
| `soothe.cognition.strange_loop.step.started` | EXECUTE step began |
| `soothe.cognition.strange_loop.step.completed` | EXECUTE step completed |
| `mode="messages"` + loop-tagged AI + `phase="goal_completion"` (and related phases) | Streaming / final user-visible answer text (IG-317; not `soothe.output.*`) |
| `soothe.cognition.strange_loop.completed` | Loop completed lifecycle event |

**Contract note**: Message-mode tool telemetry chunks remain visible during execute; plain execute-phase assistant prose is daemon-suppressed for stdout. User-visible completion prose is forwarded on the **messages** wire with **`phase`**, not as separate `soothe.output.goal_completion.*` custom event types. **RFC-500** defines how the Textual TUI maps loop-tagged AI (`execute_step` → step card, subagent scope → task card, `goal_completion` → `AssistantMessage`).

---

## Configuration

```yaml
agentic:
  enabled: true
  max_iterations: 8

  # Thread isolation for sequential Execute
  sequential_act_isolated_thread: true
  sequential_act_isolate_when_step_subagent_hint: true

  # Execution bounds
  max_subagent_tasks_per_wave: 2  # safety cap

  # Output contract
  layer2_output_contract_enabled: true

  planning:
    adaptive_granularity: true
  judgment:
    evidence_threshold: 0.7
```

---

## Implementation Status

- ✅ Plan → Execute loop implemented
- ✅ AgentDecision batch execution model
- ✅ PlanResult goal-directed evaluation
- ✅ Iteration-scoped planning
- ✅ EXECUTE → CoreAgent integration
- ✅ Thread isolation pattern
- ✅ Subagent task cap tracking
- ✅ Output contract suffix
- ✅ Prior conversation for Plan
- ✅ Metrics aggregation in executor
- ✅ LoopState wave metrics schema
- ✅ Metrics-driven Plan prompts
- ✅ Token tracking with tiktoken fallback
- ✅ Evidence-driven Plan messages

---

## References

- RFC-000: System conceptual design
- RFC-001: Core modules architecture
- RFC-100: CoreAgent runtime
- RFC-200: Layer 3 Goal management and backoff authority
- RFC-203: StrangeLoop State & Memory Architecture
- RFC-207: StrangeLoop Thread Management & Goal Context
- RFC-213: StrangeLoop Reasoning Quality & Robustness

---

## Changelog

### 2026-10-07
- Removed superseded Plan-Execute Loop Model (imperative while-driver), AgentDecision Model (batch plan-wave emission), PlanResult Model (plan-wave assessment), and PLAN Phase (upfront planning) sections (per RFC-220 / RFC-904 / RFC-213). CoreAgent delegation, evidence handoff, stream events, and Layer 1/3 integration retained as normative.

### 2026-04-29
- Aligned stream event table with `soothe.cognition.strange_loop.reasoned`; clarified execute-phase suppression and tool-telemetry visibility.

### 2026-04-17
- Consolidated legacy Layer 2 loop/decision/result RFC fragments into this unified core loop architecture with batch execution model, PlanResult goal-directed evaluation, contamination prevention, and metrics-driven planning.

---

*Layer 2 agentic execution through Plan → Execute loop with context isolation, execution bounds, metrics-driven planning, and goal-directed evaluation.*