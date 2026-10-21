# RFC Namings

This document defines the terminology and naming conventions used in this project.

**Last Updated**: 2026-08-19

> Note: Also covers start-phase intake & branch routing terms (RFC-LOOP-025),
> LoopRail streaming slice / worktree terms (RFC-LOOP-017 §9, RFC-LOOP-018), and
> recursive step decomposition terms (RFC-SLP-002).
## Core Terminology

### Core Module Architecture

| Term | Definition | Introduced In |
|------|------------|---------------|
| CoreAgent | Foundation runtime for Soothe's execution architecture. Handles tool/subagent execution via LangGraph CompiledStateGraph, created by `create_soothe_agent()`. Operates at the lowest level with Model → Tools → Model loop. | RFC-CORE-001 |
| StrangeLoop | Single-goal execution through iterative Plan-Execute cycles. Agentic goal execution for single-goal completion via iterative refinement. Operates at the middle level with Plan → Execute → Assess loop (max ~8 iterations). | RFC-LOOP-001 |
| GoalEngine | Autonomous goal management with multi-goal DAGs, scheduling, and long-running workflows. Operates at the highest level with Goal → PLAN → PERFORM → REFLECT loop. Daemon-owned singleton service. | RFC-LOOP-017 |
| LoopState | Persistent execution state across plan-execute cycles in StrangeLoop. Contains plan, progress, metrics, and execution context. LangGraph state schema. | RFC-LOOP-001 |

**Naming Convention**: Use concrete module names (CoreAgent, StrangeLoop, GoalEngine) instead of abstract "Layer N" terminology. This improves clarity and follows AGENTS.md terminology rules.

### Domain Terms

| Term | Definition | Introduced In |
|------|------------|---------------|
| Orchestrator | The Soothe agent instance created by `create_soothe_agent()`. Wires together all protocols and delegates to deepagents. | RFC-000 |
| Thread | One continuous agent conversation/execution. Has a unique ID, persistable state, and metadata. | RFC-000 |
| Delegation | Routing work to a subagent (local or remote) via deepagents' `task` tool. | RFC-000 |
| Parallel Delegation | Routing work to multiple subagents concurrently via multiple `task` tool calls in a single CoreAgent turn. Each subagent gets isolated thread branch automatically. | RFC-613 |
| Explore Agent | Specialized subagent for targeted filesystem searches using LLM-orchestrated iterative tool selection. Adapts strategy dynamically based on findings. | RFC-613 |
| Search Thoroughness | Configurable search depth levels: quick (3 iterations, minimal reading), medium (6 iterations, selective reading), thorough (10 iterations, deep analysis). | RFC-613 |
| Search Strategy | LLM-generated plan for filesystem search including priority directories, file patterns, content keywords, and search type classification. | RFC-613 |
| Match Validation | LLM assessment of found candidates against search target, ranking by relevance ("high", "medium", "low") and returning top 3-5 matches with brief descriptions. | RFC-613 |
| Context Ledger | The orchestrator's unbounded, append-only accumulation of `ContextEntry` items. Distinct from conversation history. | RFC-000, RFC-001 |
| Context Projection | A bounded, purpose-scoped view of the context ledger, assembled to fit within a token budget. | RFC-000, RFC-001 |
| Long-Term Memory | Cross-thread persistent knowledge managed by `MemoryProtocol`. Explicitly populated, semantically queryable. | RFC-000, RFC-001 |
| Plan / Step | A structured decomposition of a goal. Steps have execution hints and statuses. | RFC-000, RFC-001 |
| Policy Profile | A named configuration of permitted actions (e.g., `readonly`, `standard`, `privileged`). | RFC-000, RFC-001 |
| Permission Set | A collection of structured `Permission` objects with scope-aware matching logic. | RFC-000, RFC-001 |
| Concurrency Policy | Configuration controlling parallel execution limits for steps, subagents, and tools. | RFC-000, RFC-001 |

### Technical Terms

| Term | Definition | Introduced In |
|------|------------|---------------|
| Protocol | A Python `Protocol` or abstract base class defining a runtime-agnostic interface. NOT a network protocol. | RFC-000 |
| `ContextProtocol` | Protocol for cognitive context accumulation and projection. | RFC-001 |
| `ContextEntry` | A unit of knowledge in the context ledger (source, content, timestamp, tags, importance). | RFC-001 |
| `ContextProjection` | A bounded view of the context ledger for a specific purpose (entries, summary, token count). | RFC-001 |
| `MemoryProtocol` | Protocol for cross-thread long-term memory (remember, recall, forget). | RFC-001 |
| `MemoryItem` | A unit of long-term knowledge (id, content, tags, importance, metadata). | RFC-001 |
| `PlannerProtocol` | Protocol for goal decomposition, plan creation, reflection, and revision. | RFC-001 |
| `LLMPlanner` | Unified planner using two-phase architecture (`StatusAssessment` + conditional `PlanGeneration` → `PlanResult`) for token efficiency; IG-372/IG-329 prompt and schema alignment. Replaces SimplePlanner, ClaudePlanner, AutoPlanner after IG-150 consolidation. | RFC-001, RFC-LOOP-021 |
| `PolicyProtocol` | Protocol for permission checking and enforcement. | RFC-001 |
| `Permission` | A structured permission with category, action, and scope (e.g., `Permission("shell", "execute", "!rm")`). | RFC-001 |
| `PolicyMiddleware` | deepagents `AgentMiddleware` that enforces `PolicyProtocol`. | RFC-001 |
| `ContextMiddleware` | deepagents `AgentMiddleware` that manages `ContextProtocol` integration. | RFC-001 |
| `DurabilityProtocol` | Protocol for thread lifecycle management and state persistence. | RFC-001 |
| `IdentityProtocol` | Protocol for AKSK-based authentication and JWT token management. Provides user creation, AKSK provisioning, token issuance/validation, and external channel identity mapping. | RFC-PROT-007 |
| `IdentityMiddleware` | First middleware in stack, validates JWT tokens or resolves external sender_id to user_id before PolicyMiddleware. | RFC-PROT-007 |
| `AKSKPair` | Access Key / Secret Key credential pair for authentication. Access key format: `AK-{16 chars}`, Secret key format: `SK-{32 chars}`. | RFC-PROT-007 |
| `TokenClaims` | JWT payload structure containing jti, user_id, aksk_id, token_type, issued_at, expires_at. | RFC-PROT-007 |
| `ExternalIdentityMapping` | Mapping from external channel sender_id to soothe user_id for workspace isolation on external channels. | RFC-PROT-007 |
| `ThreadInfo` | Data model for thread state (id, status, timestamps, metadata). | RFC-001 |
| `ConcurrencyPolicy` | Data model controlling parallel execution of steps, subagents, and tools. | RFC-001 |
| `StepResult` | Data model for a completed plan step's output and status. | RFC-001 |

### TUI Step Card Terms (RFC-CLNT-009)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Step card | Textual widget `CognitionStepMessage` aggregating one plan step's main-agent tools, task delegations, execute prose, and footer status. | RFC-CLNT-001, RFC-CLNT-009 |
| Activity tree | `#step-cognition-subagent-notes` body: capped tool previews, task branches, branch Running lines. Rendered by `StepActivityTree`. | RFC-CLNT-009 |
| `StepToolRow` | Dataclass for one tool invocation on a step card (phase, args, parent, `is_task_row`). | RFC-CLNT-009 |
| `StepRowIndex` | Single-pass classification of all `StepToolRow` entries (main, orphan, task children, totals). Built by `StepRowClassifier`. | RFC-CLNT-009 |
| Surface sync | `_sync_step_card_surface()` — unified repaint of activity tree, footer, optional tools panel, and running timer. | RFC-CLNT-009 |
| Total tool count | Footer stat: distinct non-task tool rows on the step (main + subgraph + orphan). Not main-agent-only. | RFC-CLNT-009 |
| Goal-bound display snapshot | Immutable, collapsed display record written when a goal completes (`running → idle`). Loop history recovery concatenates frozen snapshots for completed goals plus the live card tail for the active goal. | RFC-CLNT-011 |
| Loop-scoped router profile override | TUI `/model-router` command that selects a named `router_profiles` entry for the current StrangeLoop. Subsequent turns resolve chat `ModelRouter` roles from the selected preset. `/clear` or a new loop drops the override. | RFC-LOOP-026 |

### Progress Event Terms

| Term | Definition | Introduced In |
|------|------------|---------------|
| Progress Event | A `soothe.*` custom event dict emitted via the LangGraph stream for protocol observability. Follows the 4-segment naming convention `soothe.<domain>.<component>.<action>`. | RFC-DAEM-001 |
| Event Domain | The second segment of a progress event type string. One of: `lifecycle`, `protocol`, `tool`, `subagent`, `output`, `error`. Enables structural classification without heuristics. | RFC-DAEM-001 |
| `SootheEvent` | Pydantic `BaseModel` base class for all typed progress events. Subclassed by domain base classes (`LifecycleEvent`, `ProtocolEvent`, `ToolEvent`, `SubagentEvent`, `OutputEvent`, `ErrorEvent`). | RFC-DAEM-001 |
| `EventRegistry` | Central registry mapping event type strings to `EventMeta` (model, domain, verbosity, summary template) and handler callables. Provides O(1) dispatch. | RFC-DAEM-001 |
| `EventRenderer` | Protocol for rendering progress events. Implementations: `CliEventRenderer` (stderr text), `TuiEventRenderer` (Rich Text), `JsonlEventRenderer` (passthrough). | RFC-DAEM-001 |
| `EventMeta` | Frozen dataclass holding metadata for a registered event type: type string, model class, domain, component, action, verbosity category, and summary template. | RFC-DAEM-001 |

### Tool Interface Terms (RFC-CORE-002)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Single-Purpose Tool | A tool that performs exactly one operation with direct naming (e.g., `run_command`, `read_file`). Replaces unified dispatch tools for better LLM tool selection. | RFC-CORE-002 |
| Unified Dispatch Tool | DEPRECATED pattern. A tool that routes to multiple operations via mode/action parameters (e.g., `execute(mode="shell")`). Replaced by single-purpose tools due to cognitive load. | RFC-CORE-002 |
| Surgical Editing | Line-based file modification using tools like `edit_lines`, `insert_lines`, `delete_lines`. Safer than full-file rewrites. | RFC-CORE-002 |
| Python Session | Persistent IPython InteractiveShell instance keyed by thread_id. Enables variable persistence across `run_python` calls. | RFC-CORE-002 |
| Session Manager | Singleton managing Python sessions with thread_id isolation, cleanup, and thread-safe execution. | RFC-CORE-002 |
| Structured Error | Error response with standardized format: error, details, suggestions, recoverable, auto_retry_hint. Provides actionable guidance for LLM recovery. | RFC-CORE-002 |

### Loop-Rail Terms (RFC-203)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Loop-Rail Mode | Job-scoped, event-driven autonomous operation (dreaming retired; no-rail jobs use CE opportunistic dispatch). | RFC-203, RFC-LOOP-017 |
| Dreaming Mode | *(Retired — IG-LOOP-013)* Legacy idle state for memory consolidation, indexing, goal anticipation, and health monitoring. No-rail jobs now use CE opportunistic dispatch. | RFC-203 |
| Consensus Loop | Layer 3 validation of Layer 2 completion judgment with send-back capability and budget. | RFC-203 |
| Send-Back Budget | Per-goal limit on Layer 3 rejections (default: 3 rounds). Independent from Layer 2 iteration budget. | RFC-203 |
| Channel Protocol | Message-centric protocol for user ↔ Soothe communication. Loop-rail control uses HTTP REST; platform channels use RFC-DAEM-008. | RFC-203 |
| CriticalityEvaluator | Module in GoalEngine that determines if a proposed goal requires user confirmation (MUST status). | RFC-203 |
| SchedulerService | Independent service in `core/goal_engine/scheduled_tasks.py` for time-based task execution (delay, cron, recurrence). | RFC-203 |
| Goal Relationship | Connection between goals: `depends_on` (hard), `informs` (soft), `conflicts_with` (mutual exclusion). | RFC-203 |
| Context Envelope | Rich context package sent from Layer 3 to Layer 2 containing world info, goals, memory, instructions. | RFC-203 |
| Same-Cron Conflict | Multiple tasks with identical cron expression. Resolved by sequential execution, ordered by creation/priority. | RFC-203 |
| Critical Message | Channel message requiring acknowledgment (e.g., blocker_alert, MUST goal confirmation). Retries with backoff. | RFC-203 |

### Entity Model Consolidation Terms (RFC-SDK-003)

| Term | Definition | Introduced In |
|------|------------|---------------|
| ExecutionState | Thin facade holding execution-only runtime fields (iteration, max_iterations, wave metrics, context window stats) with CE-backed properties for goal/step data. Replaces LoopState. | RFC-SDK-003 |
| Job | Root GoalNode with `parent_id=None` submitted to LoopRailService. Single entry point for DAG visualization and status queries. | RFC-SDK-003, RFC-DAEM-005 |
| GoalNode | Unified entity model combining goal lifecycle, retry/backoff semantics, and workspace metadata. CE's atomic unit of persistence. | RFC-SDK-002, RFC-LOOP-017 §17, RFC-SDK-003 |
| LoopRail | Job-scoped, event-driven workflow pattern consumed only by LoopRailService; mutates the CE DAG via catalog verbs. CE never reads rail YAML. | RFC-LOOP-017 |
| Slice catalog | Flat SoT of leaf slice specs on `RailJobState` after WavePlan ingest (`wave_slices` / rich `slices` / `decompose_plan`). | RFC-LOOP-017 §9, RFC-LOOP-018 |
| Streaming spawn | Loop-rail creates maker goals for unspawned catalog slices whose slice `depends_on` are satisfied; pool fills under concurrency with no wave/stage CE barrier. | RFC-LOOP-017 §9 |
| Spawn-ready | Predicate / verb semantics (`spawn_wave_makers` / `slices_ready_to_spawn`): materialize only currently ready slices. | RFC-LOOP-017 §8–§9 |
| Job branch | Per-job integration git branch (`job/<id>`); host merges maker branches here; land on `main`/`master` only at job complete. | RFC-LOOP-017 §9 |
| WavePlan | Flat planner deliverable: leaf slice ids and/or rich `slices[]` with optional peer-slice `depends_on`; nested wave trees forbidden. | RFC-LOOP-018 |
| StepNode | Execution step entity within GoalNode's embedded StepDAG with lineage tracking (plan_iteration, reasoning_trace). | RFC-SDK-002, RFC-SDK-003 |
| LedgerManager | Unified message ledger replacing LoopWorkingMemory and loop_messages list, with phase-scoped retrieval and bounded projection. | RFC-SDK-002, RFC-SDK-003 |
| CheckpointEnvelope | Consolidated checkpoint structure storing CE GoalStepDAG snapshot and ExecutionState fields, eliminating duplicate StrangeLoop checkpoint schemas. | RFC-SDK-003 |

### Layer 2 Execution Terms (RFC-200)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Context Isolation | Thread isolation for delegation steps where subagents receive only explicit task input, no prior conversation history. Prevents cross-wave contamination. | RFC-200 |
| Thread Isolation | Automatic isolation provided by task tool for subagent delegations. Tool executions use parent thread_id with langgraph concurrent safety. Simplified in RFC-LOOP-003. | RFC-200, RFC-LOOP-003 |
| Execution Bounds | Two-layer constraint preventing runaway subagent loops: soft constraint (schema/prompt) and hard constraint (subagent task cap). | RFC-200 |
| Wave Metrics | Structured metrics collected per Act wave (tool_call_count, subagent_task_count, output_length, error_count, context_window) informing Reason decisions. | RFC-200 |
| Subagent Task Cap | Maximum subagent delegations per Act wave (default 2). Stops stream early on cap hit, signals metrics to Reason. | RFC-200 |
| Output Contract | Layer 2 anti-repetition instructions preventing main model from pasting full subagent output after streaming. | RFC-200 |
| Manual Thread ID Generation (deprecated) | Old pattern where executor created isolated thread IDs (`{thread_id}__l2act{uuid}`, `{thread_id}__step_{i}`) and manually merged results. Removed in RFC-LOOP-003. | RFC-200 (deprecated), RFC-LOOP-003 |
| Outcome Metadata | Structured dict replacing full tool result content in StepResult. Contains type, tool_call_id, success_indicators, entities, size_bytes, optional file_ref. Enables Layer 2 reasoning without content bloat. | RFC-LOOP-004 |
| Tool Call ID | Unique identifier from LangChain for each tool invocation (format: `call_<uuid>`). Guaranteed unique even for same tool called multiple times. Used for file cache naming. | RFC-LOOP-004 |
| Tool Result Cache | File system cache for large tool results (>50KB) at `~/.soothe/runs/{thread_id}/tool_results/{tool_call_id}.json`. Optional, cleaned up after thread completion. | RFC-LOOP-004 |
| Minimal Data Contract | Design principle where Layer 2 receives only outcome metadata from Layer 1, not full tool result content. Layer 1 owns final report generation. | RFC-LOOP-004 |

### Prior-Progress Digest Terms (RFC-LOOP-016)

| Term | Definition | Introduced In |
|------|------------|---------------|
| `PriorProgressDigest` | Compact, typed snapshot of the most recent execute wave (`iteration`, `wave_index`, `steps_completed`, `steps_failed`, `tool_calls`, `evidence_excerpts`, `derived_progress_hint`). Produced once per wave by the executor, stashed on `LoopState.prior_progress`, consumed by `plan_assess` and `plan_generate` as grounding. Overwrite-only (K=1). | RFC-LOOP-016 |
| `ToolCallHead` | One tool invocation captured from the most recent wave: `{name, head}` where `head` is the first non-empty line of the tool message content, stripped and truncated at 120 chars. | RFC-LOOP-016 |
| `<PRIOR_PROGRESS>` | XML block appended to the plan-context envelope when `state.prior_progress` is present and not stale; renders `iter/wave/done/failed/hint`, up to 8 `tools` lines, and up to 3 `evidence` lines. Hard-capped at 600 chars. | RFC-LOOP-016 |
| `derived_progress_hint` | Deterministic `"none"\|"low"\|"medium"\|"high"` label computed by `_update_prior_progress` from wave success/failure counts and evidence-text heuristics (digits, table glyphs, completion keywords). Shown verbatim inside `<PRIOR_PROGRESS>`; never overrides `StatusAssessment.goal_progress` in code. | RFC-LOOP-016 |
| `Step Anchor Registry` | Plan-generate envelope section listing completed/pending/failed steps with composite ids, outcome snippets, next local id range, and cross-wave dependency rules. Built from CE `GoalNode.steps` or `LoopState.step_results`. | RFC-SDK-002 §3.1, IG-539 |
| `Plan DAG Normalizer` | Deterministic post-processor for `AgentDecision` dependencies: resolves bare suffix tokens to composite ids, drops invalid refs, breaks in-plan cycles, forces `execution_mode="dependency"`. Runs in `_finalize_generated_plan_result` and `resolve_decision`. | RFC-SDK-002 §3.1, IG-539 |
| `continues_from` | Optional `PlanGenerateStep` field listing completed composite step ids from prior plan waves; merged into runtime `StepAction.dependencies` at conversion. | IG-539 |
| `_update_prior_progress()` | Executor helper invoked from `_append_parallel_wave_ledger`. Reads the just-finished wave's `steps`, `gather_results`, and `step_messages`; writes `state.prior_progress`. Pure-function over wave outputs; no I/O. | RFC-LOOP-016 |
| Digest staleness | The envelope omits `<PRIOR_PROGRESS>` when `prior_progress.iteration < state.iteration - 1`. Prevents showing a snapshot from a long-past iteration as if it described the current state. | RFC-LOOP-016 |
| Assessment-reasoning contract | The `plan_assess_instructions.xml` paragraph requiring `StatusAssessment.assessment_reasoning` to (a) summarize `<PRIOR_PROGRESS>` evidence when present and (b) never restate the user query. Closes the RFC-LOOP-016 prompt gap. | RFC-LOOP-016 |

### Continuation Discriminator Terms (RFC-LOOP-015)

| Term | Definition | Introduced In |
|------|------------|---------------|
| `continuation_assess` | Iter=0 LLM call in `plan_assess` for continuation queries (`continue_loop_mode` AND `goal_history >= 2`). Reads the new query against persisted prior goals (RFC-LOOP-014 enrichment) and emits a `ContinuationAssessment` that routes to either bootstrap or `plan_generate`. Replaces the structural `continue_loop_plan_bootstrap_allowed()` heuristic. | RFC-LOOP-015 |
| `ContinuationAssessment` | Pydantic structured output of the `continuation_assess` LLM call: `{action: "bootstrap" | "plan_generate", reasoning, goal_progress}`. | RFC-LOOP-015 |
| `LOOP_CONTINUATION_ASSESS_PROMPT` | Prompt template that surfaces prior goals (`goal_text`, `goal_completion` preview, `step_count`, `current_plan.next_action`) plus available capabilities to the discriminator LLM. | RFC-LOOP-015 |
| `PlanResult.terminal_after_execute` | Boolean field on `PlanResult` asserting that the plan's single step IS the goal completion. When True, `route_after_record_iteration` routes directly to `goal_completion`, skipping the iter=1 status check. Set by the bootstrap path; default False elsewhere. | RFC-LOOP-015 |
| Bootstrap action | `ContinuationAssessment.action == "bootstrap"` — the assess LLM judges that the new query can be answered using prior loop context with no new tools or steps. Triggers a single-step terminal plan via `build_continue_loop_bootstrap_plan(..., terminal_after_execute=True)`. | RFC-LOOP-015 |
| Plan-generate action | `ContinuationAssessment.action == "plan_generate"` — the assess LLM judges that the new query needs multiple steps, new tools, or cross-domain work. Routes to the standard `plan_generate` node. | RFC-LOOP-015 |
| Post-execute fast exit | The `record_iteration → goal_completion` conditional edge that fires when `ctx.scratch.plan_result.terminal_after_execute` is True. Eliminates the redundant iter=1 `plan_assess` LLM call on bootstrap paths. | RFC-LOOP-015 |

### Loop Continuity & Goal Record Terms (RFC-LOOP-014)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Loop | A continuous conversational unit identified by `loop_id`. Spans many goals and survives across user turns until the user starts a new loop (`/clear`). The unit of continuity for agentic intent. | RFC-LOOP-003, RFC-LOOP-014 |
| `continue_loop_mode` | Boolean derived once in `StrangeLoop` immediately after `state_manager.load()`. True when the loaded checkpoint has prior goals and is alive (`status ∈ {running, idle}`). Replaces the prior `continue_thread_mode` flag. | RFC-LOOP-014 |
| Intent Type | Two-value LLM classification: `quiz` (greeting / thanks / trivia answerable without tools) or `agentic` (everything else). Whether an agentic query continues a loop is derived structurally, not classified. | RFC-LOOP-014 |
| Quiz Fast-Path | Pre-stream short-circuit when `IntentClassification.intent_type == "quiz"`; uses the LLM's piggybacked `quiz_response` to skip the agent loop entirely. | RFC-LOOP-014 |
| Idle (loop status) | `StrangeLoopCheckpoint.status == "idle"` — loop is alive between goals. Renamed from the legacy value `ready_for_next_goal`; legacy persisted values are coerced on load. | RFC-LOOP-014 |
| Goal Record | `GoalExecutionRecord` — durable per-goal log inside `StrangeLoopCheckpoint.goal_history`. Carries the latest plan DAG (`current_plan`), accumulated `step_results`, `evidence_ledger`, `completed_step_ids`, `plan_revision_count`, the orchestration `loop_messages` ledger, and final output. Sufficient to recover the goal's plan DAG with execution overlay without external lookup. | RFC-LOOP-003, RFC-LOOP-014 |
| Plan DAG Recoverability | Invariant that the full DAG of any persisted goal — nodes, edges, execution mode, planner metadata, done-node overlay, per-node outcomes — is recoverable from `GoalExecutionRecord` alone. | RFC-LOOP-014 |
| `_LOOP_CONTINUATION_GUIDE` | System-prompt section injected by `system_prompt` when `state["continue_loop_mode"]` is `True`. Renamed from `_THREAD_CONTINUATION_GUIDE`. | RFC-LOOP-014 |
| `seed_loop_ledger_from_prior_goal()` | Seeds a new goal's `loop_messages` from the immediately prior completed goal in the same loop. Runs unconditionally for any same-loop new goal. Renamed from `seed_continue_thread_ledger_from_prior_goal()`. | RFC-LOOP-014 |

### Start-Phase Intake & Branch Routing Terms (RFC-LOOP-025)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Intake LLM | Two-pass fast-model classification: Pass 1 social vs task, Pass 2 scope (`trivial \| simple \| complex`). Runs overlapped with pre-graph IO. Replaces the binary `IntentClassifier` LLM + its `_is_likely_agentic` heuristic bypass. Intake never selects a specialist (IG-669). | RFC-LOOP-025 |
| Wired-subagent route | When slash `preferred_subagent` resolves to an allowlisted specialist, StrangeLoop sets `intent_route=wired_subagent` and runs `invoke_wired_subagent`. Intake-only specialists (`browser_use`, `deep_research`, `academic_research`) direct-invoke then `goal_completion`. `planner` additionally writes `.soothe/plans/` and pauses on `planner_subagent_review` (Approve/Reject/More comments) per RFC-LOOP-027 — not StrangeLoop `plan_generate`/`plan_assess`. On Approve, route to DISPATCH and ground the root THREAD with the approved plan (RFC-SLP-002). | RFC-LOOP-025, RFC-LOOP-027, RFC-SLP-002 |
| Intake-only subagent | Specialist registered for wired invoke but omitted from the open CoreAgent `task` catalog and plan `delegate` surface (`planner`, `browser_use`, `deep_research`, `academic_research`). | RFC-LOOP-025 |
| Orphan SubAgent card | SubAgent card with empty parent step / `task` row, mounted for intake-only wired invoke. Registry key `wire:{subagent}:{invocation_id}`. | RFC-CLNT-009 |
| `wired_subagent_started` / `completed` / `failed` / `cancelled` | StrangeLoop lifecycle envelopes for the intake-only direct invoke; TUI mounts and completes the orphan SubAgent card. Wire progress is forwarded `soothe.subagent.*` customs stamped with `invocation_id`; planner recon also forwards `soothe.stream.tool_call.update`. | RFC-LOOP-025, RFC-LOOP-027 |
| Plan artifact | Markdown plan file at `{workspace}/.soothe/plans/{timestamp}-{slug}.md` written by the host after intake planner completes. | RFC-LOOP-027 |
| StrangeLoop `generate_plan` | StrangeLoop **planning-stage** station that drafts/refines the multi-step `AgentDecision` for the current goal wave (legacy id: `plan_generate`). Not the intake `planner` subagent. | RFC-LOOP-010, RFC-LOOP-016, IG-663 |
| StrangeLoop `assess` | StrangeLoop **planning-stage** station that assesses progress / continuation before (or instead of) regenerating a plan (legacy id: `plan_assess`). Not the intake `planner` subagent. | RFC-LOOP-010, RFC-LOOP-015, IG-663 |
| StrangeLoop stem stations | Flat LangGraph node IDs: `intake` → `enter_loop` → (`gather_evidence` →) `evaluate` → `generate_plan` → `commit_plan` → `execute` → `record_progress` → `check_limits`… / `finalize`. Sidecars: `await_user`, `delegate`. RFC-SLP-001 folded `validate_plan`→`commit_plan` and `begin_iteration`→`check_limits`. See `orchestrator/stations.py`. | IG-663, RFC-SLP-001 |
| `route_after_preprocess` | Conditional edge after `enter_loop` that dispatches by intake label (legacy name: `route_by_intent`). | RFC-LOOP-025, IG-663 |
| Planner subagent review | Human Approve / Reject / More comments gate **after** the intake-only `planner` subagent produces a markdown plan artifact. Clarification origin: `planner_subagent_review`. Distinct from StrangeLoop `plan_generate` / `plan_assess`. | RFC-LOOP-027, RFC-LOOP-023 |
| `planner_subagent_review` | `ClarificationOrigin` for the planner-subagent review gate only. Default entry in `agent.clarification.force_manual_origins`. | RFC-LOOP-027 |
| IntakeLabel | 4-class enum: `quiz` (greeting/thanks/trivia, no tools), `trivial` (single obvious action, no planning LLM needed), `simple` (single focused step, lightweight plan), `complex` (multi-step/multi-phase, full plan). Continuation is NOT a label — it is a structural overlay from the checkpoint. | RFC-LOOP-025 |
| `route_by_intent` | Legacy alias of `route_after_preprocess` (kept for imports). | RFC-LOOP-025, IG-663 |
| Branch routing | Start-phase dispatch: chitchat→END, fresh `trivial`→synthetic 1-step plan, fresh `simple`→lightweight `generate_plan`, `complex`→full spine; continuation `trivial/simple` → `assess` first. | RFC-LOOP-025, IG-663 |
| `generate_lightweight` | Cheaper plan call for the `simple` branch: reuses `generate_from_assessment`'s structured-output path with a reduced context window (last N step results, no full evidence ledger). Same `PlanGeneration` schema. | RFC-LOOP-025 |
| Trivial-branch plan | Minimal 1-step `PlanResult` injected by `enter_loop` for the `trivial` label: step action = the intake LLM's `goal_description` (no prefix), plan reasoning = `None` (no synthetic prose), soft direct-answer `expected_output`, and `requires_tool_use` from Pass 2 for the execute deliverable gate (IG-569). | RFC-LOOP-025, IG-663 |
| Two-stage pre-graph gather | Parallelized pre-graph sequence: stage 1 = intake LLM ∥ `checkpoint.load` ∥ `git_status`; stage 2 = CE construct+load+`create_goal`/`activate_goal` (depend on checkpoint) ∥ instruction/memory file reads via `to_thread`. Stage split is a correctness constraint (CE needs the checkpoint), not an optimization choice. | RFC-LOOP-025 |
| Direct replacement | The 4-class intake is the sole intent path — the legacy binary `IntentClassificationLLMResult` schema, `classify_intent`, the binary prompt fragments, and `_is_likely_agentic` are removed outright. No feature flag, no backward-compat shim. The `IntentClassifiedEvent` wire contract (`intent_type: quiz\|agentic`) is preserved; `intent_type` is derived from the 4-class label. | RFC-LOOP-025 |

### Node Lifecycle Terms (RFC-SLP-001)

| Term | Definition | Introduced In |
|------|------------|---------------|
| `LoopNode` | Base class for every StrangeLoop graph node with a five-method lifecycle: `pre` (guards/setup) → `project` (DAG projection) → `prompt` (message assembly) → `process` (core work) → `post` (writes/emit/route). Replaces the implicit `async def(ctx, state) -> dict` shape. Non-LLM nodes no-op `project`/`prompt`. | RFC-SLP-001 |
| `RouteDecision` | Typed sum-type returned by `LoopNode.post()`: `kind ∈ {proceed, await_user, deferred, fatal, terminal}` + `next_phase` / `clarification_origin` / `state_patch`. Replaces the free-form route-key dict (`plan_route`, `assess_route`, `evidence_gather_route`, `after_record_route`, `resume_synth`, `planner_implement_handoff`). | RFC-SLP-001 |
| `GuardOutcome` | Short-circuit result from `LoopNode.pre()`: `kind ∈ {fatal, deferred, skip}`. Folds the per-node `emit fatal_error + return {"last_outcome":"fatal"}` boilerplate into one `pre()` default. | RFC-SLP-001 |
| `wrap_node` | Builder adapter that detects `LoopNode` instances vs legacy `async def(ctx, state) -> dict` functions, so the graph adopts the new base incrementally. | RFC-SLP-001 |

### Clarification Relay Terms (RFC-LOOP-023, RFC-LOOP-024)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Clarification Relay | CoreAgent → user → CoreAgent loop to resolve ambiguity without stopping the agent loop. When CoreAgent cannot confidently answer, it emits a `ClarificationRequest` event, suspends itself, waits for user clarification via `await_clarification` node, then resumes with the clarified answer. | RFC-LOOP-023 |
| Veritas | Agent node that performs structured yes/no confidence checking for core agent answers. Checks whether CoreAgent has enough information to confidently respond to user. Emits `ClarificationDeferredError` when confidence is insufficient. | RFC-LOOP-023 |
| Interactive Fallback | Mechanism allowing StrangeLoop to auto-retry Veritas failures up to N times before raising `ClarificationDeferredError` to the orchestrator. Prevents immediate loop exit on transient issues. | RFC-LOOP-024 |
| `ClarificationPolicy` | Config knob controlling Veritas behavior: `max_defer_attempts` (N), `confidence_threshold`, `auto_retry_on_defer_kind`. | RFC-LOOP-023 |
| `ClarificationRequest` | Event payload: `{question, context, urgency, timeout_hint}`. Sent from daemon to client when CoreAgent needs clarification. | RFC-LOOP-023 |
| `ClarificationAnswer` | Event payload: `{original_question, answer, source}`. User response to a clarification request. | RFC-LOOP-023 |
| `ClarificationDeferredError` | Exception raised after N Veritas failures. Signals StrangeLoop to either retry with different parameters or exit goal with clarification status. | RFC-LOOP-023, RFC-LOOP-024 |
| `DeferKind` | Enum in Veritas response: `ambiguous`, `insufficient_context`, `contradiction`, `other`. Used by Interactive Fallback to decide retry strategy. | RFC-LOOP-024 |
| `VeritasAnswerSchema` | Pydantic model for Veritas structured output: `{can_answer: bool, defer_kind: DeferKind | null, reasoning: str}`. | RFC-LOOP-023, RFC-LOOP-024 |
| `await_user` station | StrangeLoop sidecar that suspends for clarification (legacy id: `await_clarification`). Resumes when `ClarificationAnswer` arrives. | RFC-LOOP-023, IG-663 |
| `awaiting_clarification` | LoopState status flag indicating CoreAgent is suspended waiting for user clarification. | RFC-LOOP-023 |
| `defer_kind` (event field) | Field in `ClarificationDeferredError` event indicating why Veritas deferred. Used by downstream handlers for categorization. | RFC-LOOP-024 |
| `invoke_structured_chat` | Veritas helper that calls the model with `VeritasAnswerSchema` to check confidence. Returns structured `can_answer` decision. | RFC-LOOP-024 |
| `build_veritas_response_schema(n)` | Constructor for Veritas schema with configurable `max_defer_attempts` N. | RFC-LOOP-024 |

### Client Library Terms (RFC-CLNT-010)

| Term | Definition | Introduced In |
|------|------------|---------------|
| `Client` / `WebSocketClient` | The core WebSocket transport for the protocol-1 daemon (Python: `WebSocketClient`; Go/TS: `Client`). Owns handshake, envelope codec, RPC, streaming, control frames, heartbeat, reconnect/reattach, multiplexing, and `delivery_ack`. | RFC-CLNT-010 |
| `appkit` | Sibling package over the core client: `DaemonSession`, connection pooling, single-flight query gating, turn execution, event classification, and SSE fan-out. Product decisions via configuration and interfaces. | RFC-CLNT-010 |
| `DaemonSession` | Dual-socket `appkit` session for one conversation: subscribed stream socket + RPC sidecar; `SendTurn` / `IterTurnChunks` / `EnsureConnected`. Primary happy-path entry for streamed turns. | RFC-CLNT-010 |
| `CommandClient` | Ephemeral one-shot RPC client for jobs/cron: connect → handshake → single request → close. Distinct from the long-lived streaming client. | RFC-CLNT-010 |
| `delivery_ack` | Client notification carrying monotonic per-loop `seq` after terminal stream frames so the daemon can gate drain correctly under load. | RFC-CLNT-010 |
| `ConnectionPool` | `appkit` component that acquires/releases/health-checks/reuses a core client per logical session, delegating bootstrap (`loop_new` + `subscribe`) or reattach (`loop_reattach` + `ReattachAndProbe`). | RFC-CLNT-010 |
| `QueryGate` | `appkit` component enforcing single-flight query execution per session (`ErrQueryBusy`) and the cancel-before-context ordering (daemon `cancel` before local context cancel, on a detached timeout). | RFC-CLNT-010 |
| `TurnRunner` | `appkit` component executing a timeout-bounded turn: send `loop_input`, consume the multiplexed event stream, classify events, resolve the deliverable, then persist/broadcast. | RFC-CLNT-010 |
| `EventClassifier` | `appkit` component mapping streamed frames to deliverable/streaming/terminal outcomes, keyed on `(namespace, mode, phase)` with a configurable `DeliverablePhases` set. | RFC-CLNT-010 |
| `DeliverablePhases` | Application-supplied configuration set naming which `phase` values on `mode:"messages"` chunks count as user-facing deliverables (e.g. `{quiz, goal_completion, direct_model}`). A product decision, not library policy. | RFC-CLNT-010 |
| `SSEBroadcaster` | `appkit` string-keyed pub/sub fan-out for SSE-style event delivery to subscribers; rekeyed from any application domain key type to `string`. | RFC-CLNT-010 |
| `LoopSessionStore` | `appkit` interface abstracting per-application persistence: session↔loop-id mapping, message append, last-used/reset tracking. Triarch's Postgres `ChatRegistryStore` is one implementation. | RFC-CLNT-010 |
| `StaleLoopError` | Typed error returned by `ReattachAndProbe` when a loop accepts the reattach handshake but fails the `loop_get` liveness probe; signals the caller to fall back to a fresh `loop_new` bootstrap. | RFC-CLNT-010 |
| `DisconnectCause` | Enum distinguishing clean vs unclean connection loss (Go: `DisconnectClean`/`DisconnectUnclean`; TypeScript: `DisconnectCause.Clean`/`DisconnectCause.Unclean`). Clean follows a `disconnect` notification; unclean is a read/write error or missed pong. | RFC-CLNT-010 |
| `Multiplexer` | Core client component that routes inbound protocol-1 frames to the correct waiter by `(type, id)` instead of discarding non-matching events, enabling concurrent RPCs and subscription streams. | RFC-CLNT-010 |

### Persistence / SQLite Runtime (RFC-PERS-001, RFC-PERS-002)

| Term | Definition | Introduced In |
|------|------------|---------------|
| `SqliteStoreRuntime` | Process-scoped owner of connections for one SQLite DB file: single writer, leased readers, WAL + busy_timeout, `BEGIN IMMEDIATE` writes. | RFC-PERS-001 |
| `SqliteRuntimeRegistry` | Process map of absolute path → `SqliteStoreRuntime` with refcount; closes and WAL-checkpoints on daemon shutdown. | RFC-PERS-001 |
| `databases/` layout | All purpose SQLite files under `$SOOTHE_DATA_DIR/databases/{purpose}.db` (e.g. `checkpoints.db`, `persist.db`, `vectors.db`). Hard cut; no legacy path shims. | RFC-PERS-001, RFC-PERS-002 |
| Purpose DB file | Unified `{purpose}.db` name for a logical store (checkpoints, context, display, cron, identity, metadata, persist, vectors, memory). | RFC-PERS-001, RFC-PERS-002 |

### Recursive Step Decomposition Terms (RFC-SLP-002)

| Term | Definition | Introduced In |
|------|------------|---------------|
| `StepDAG` | Goal-scoped directed acyclic graph of steps owned by the Context Engine (CE). Each step is a thread; CE reconciles proposals and commits children. Replaces upfront plan waves. | RFC-SDK-002, RFC-SLP-002 |
| `decompose_task` | Executor-bound tool that emits a `DecompositionProposal` for child steps. Distinct from `write_todos` (intra-step UX) and from goal-level `apply_llm_subgoals`. | RFC-SLP-002 |
| `DecompositionProposal` | Structured output of `decompose_task`: proposed child steps with descriptions, dependencies, and execution hints. CE reconciles (deterministic by default; LLM only on conflict). | RFC-SLP-002 |
| Reconcile | CE process of merging `DecompositionProposal`s into the StepDAG: deterministic by default, LLM only on dependency conflict. Proposals wait on the reconcile barrier; completions/failures land immediately. | RFC-SLP-002 |
| Root step | The top-level step of a goal's StepDAG, created after intake pass1 classifies a task (vs chitchat). | RFC-SLP-002 |
| `GapResult` | Historical RFC-SLP-002 P4 shape for ROOT_EVAL recoverable gaps + new-root projection. **Withdrawn** as the continuation mechanism; see RFC-SLP-003 Eval thread. | RFC-SLP-002, RFC-SLP-003 |
| `ROOT_EVAL` | Graph station: action-tree-green **gate** that inserts `kind=eval` or routes FINALIZE (RFC-SLP-003). No longer assess-only / MUST NOT `decompose_task`. | RFC-SLP-002, RFC-SLP-003 |
| B-lazy replacement | Interior node failure strategy: happy-path interior nodes are not re-invoked; failed interior nodes get replacement nodes lazily. Coverage eval is RFC-SLP-003 when the action tree is green. | RFC-SLP-002, RFC-SLP-003 |
| Tree-green | DAG helper: no pending/active/failed; non-superseded leaves completed; decomposed parents do not block. Action-tree green (RFC-SLP-003) is the Eval insert predicate. | RFC-SLP-002, RFC-SLP-003 |
| Do-or-decompose | Guiding principle: scope is discovered in execution, not by pass2 pre-classification. A step either completes or calls `decompose_task`. | RFC-SLP-002 |
| Pass1 | Intake classification (chitchat vs task) retained from RFC-LOOP-025. Pass2 (trivial/simple/complex scope pre-classification) is removed by RFC-SLP-002. | RFC-LOOP-025, RFC-SLP-002 |

### StrangeLoop Eval Thread (RFC-SLP-003)

| Term | Definition | Introduced In |
|------|------------|---------------|
| Eval thread | Fresh CoreAgent thread for a `kind=eval` StepNode: readonly inspect tools plus `decompose_task`; coverage audit of the user goal, not worker execution or FINALIZE synthesis. | RFC-SLP-003 |
| `kind=eval` | Engine-injected StepNode kind (not LLM-scheduled). Continuation children hang off this node; eval becomes `decomposed` when in-scope proposals commit. | RFC-SLP-003 |
| Action-tree green | No pending/active action or ask_user leaves; unresolved failed blocks Eval. Distinct from `tree_green()` once eval nodes exist. | RFC-SLP-003 |
| `StepCloseReport` | Fast-model structured close of an action step: `goal_portion_complete`, `early_exit`, `deferred_items`, `recommendations`. Triggers Eval when `early_exit` or nonempty `deferred_items`. Not keyword matching on worker prose. | RFC-SLP-003 |
| Early-exit | Structured claim that the worker stopped with leftover or recommended work while marking the step complete. Untrusted until Eval. | RFC-SLP-003 |

### Code Naming

| Convention | Pattern | Example |
|------------|---------|---------|
| Protocol classes | `{Name}Protocol` | `ContextProtocol`, `PolicyProtocol` |
| Middleware classes | `{Name}Middleware` | `ContextMiddleware`, `PolicyMiddleware` |
| Module directories | snake_case | `src/soothe/protocols/`, `src/soothe/middleware/` |
| Config fields | snake_case | `planner_routing`, `policy_profiles` |
| Data models | CamelCase | `ContextEntry`, `Plan`, `Permission` |

---

## Related Documents

- [RFC Standard](rfc-standard.md) - RFC process and specification kinds
- [RFC Index](rfc-index.md) - Complete RFC catalog
- [RFC History](rfc-history.md) - Chronological change history

This terminology index is manually curated with automated extraction support. To update:

```bash
# Manual additions are preserved
# Automated extraction available via:
python scripts/generate_rfc_namings.py
```