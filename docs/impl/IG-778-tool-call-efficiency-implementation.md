# IG-778: Tool Call Efficiency — Implementation Design

**Created**: 2026-09-19
**Status**: Draft
**Benchmark source**: `../claude-code` (Claude Code source snapshot, March 2026)

---

## Problem

The target is to **use tool calls as little as possible** while maintaining task
quality. Each tool call costs: one model hop (latency + tokens), one execution
round-trip, and one context-window entry that persists for the remainder of the
thread. Reducing tool-call count is the highest-leverage efficiency improvement
available — it reduces all three costs simultaneously.

Soothe nano already has several efficiency mechanisms (progressive tool loading,
per-step lookup cache, edit coalescing, output capping). Comparing against
Claude Code's patterns reveals six concrete gaps that allow unnecessary calls.
This IG covers the implementation design for all six.

---

## Scope

### In scope

| # | Item | Touch points | Effort |
|---|------|-------------|--------|
| 1 | Explicit parallel-call instruction | `system_templates.py` prompt text | S |
| 2 | Cross-step tool result eviction (microcompact analog) | New `tool_result_eviction.py`; `_builder.py` wiring | M |
| 3 | Shrink core tool set (26 → 20) | `registry.py` `DEFAULT_CORE_TOOL_NAMES`; `system_templates.py` | S |
| 4 | Multi-file read coalescing | New `read_coalescing.py`; `_builder.py` wiring | M |
| 5 | Search consolidation | Extend `tool_optimization_middleware.py` | S |
| 6 | Tool result disk storage with compact reference | New `tool_result_storage.py` + `tool_result_storage_middleware.py`; `_builder.py`; `config/models.py`, `config/constants.py` | M |

### Out of scope

- Tool budget counter in system prompt (low priority, deferred)
- Pre-read injection for common paths (low priority, deferred)
- BriefTool single-output channel (architectural change, deferred)
- Microcompact / API-native context editing (`clear_tool_uses_20250919` — not available outside Anthropic's API)
- Prompt cache prefix preservation (`ContentReplacementState` — Soothe does not use prompt caching at the middleware layer)

---

## Current State (Soothe Nano)

| Mechanism | File | What it does |
|---|---|---|
| ProgressiveToolMiddleware | `middleware/progressive_tools.py` | Defers non-core tools; promotes on `search_tools` or direct invoke |
| ToolOptimizationMiddleware | `middleware/tool_optimization_middleware.py` | Per-step lookup cache for `read_file`/`glob`/`grep`; blocks duplicate empty-result calls; blocks shell grep/rg/find; empty `write_todos` short-circuit; read-file thrash guidance |
| EditCoalescingMiddleware | `middleware/edit_coalescing.py` | Batches parallel edit calls to same file into one operation |
| ToolOutputCapMiddleware | (deepagents `reliability.py`) | Caps tool output char count at 10K (standard) / 100K (code exec). **Truncates and discards** — no disk fallback, no preview, no recovery path |
| ProgressiveListingMiddleware | `middleware/progressive_listing.py` | Builds `<AVAILABLE_TOOLS>` / `<AVAILABLE_SKILLS>` blocks |
| SystemPromptMiddleware | `middleware/system_prompt.py` | Dynamic prompt verbosity by task complexity |

### Key gap: truncate-and-discard

`ToolOutputCapMiddleware` (from `soothe_deepagents.middleware.reliability`):

- `_truncate_content()` (`reliability.py:182–191`): slices `text[:max_chars - len(suffix)] + suffix`. No disk fallback. No preview. No recovery path.
- `awrap_tool_call()` (`reliability.py:285–312`): applies per-tool cap after the handler returns.
- `awrap_model_call()` (`reliability.py:314–324`): re-applies caps to historical tool messages before each model call (idempotent re-application, but content was already truncated at write time).

**Config** (`config/constants.py:43`):
```python
DEFAULT_TOOL_OUTPUT_CHARS = 10_000
DEFAULT_CODE_EXEC_MAX_OUTPUT_CHARS = 100_000
```

Once truncated, the content is **permanently lost** to the model. If the model
needs the omitted portion, it must issue another `read_file` call with a
different `offset` — costing a full model hop. This is the single most common
source of unnecessary re-reads in long sessions.

---

## Claude Code Patterns Analyzed

### Tool deferral (`ToolSearchTool`)
Claude Code defers MCP tools and feature-gated tools behind `ToolSearchTool`.
The model sees only tool **names** (not schemas) until it calls
`ToolSearchTool(query="select:Read,Edit")`. **Soothe equivalent**:
`ProgressiveToolMiddleware` already does this — functionally equivalent.

### Microcompact (`services/compact/microCompact.ts`, `apiMicrocompact.ts`)
Two compaction tiers:
- **Microcompact**: clears old tool results (read, grep, glob, bash, web) when
  input tokens exceed 180K, keeping the last 40K tokens. Uses API-native
  `clear_tool_uses_20250919` context-edit strategy — no extra model call.
- **Auto-compact**: full conversation summary when approaching context limit.

**Soothe gap**: No equivalent. Old tool results persist indefinitely.

### Tool result storage (`utils/toolResultStorage.ts`)
Large tool outputs are written to disk and replaced with a compact reference
(preview + file path). The model can re-fetch via `FileReadTool` if needed.

**Key constants** (`constants/toolLimits.ts`):
- `DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000` — global cap
- `PREVIEW_SIZE_BYTES = 2000` — preview shown in reference
- `MAX_TOOL_RESULT_TOKENS = 100_000` (~400KB)
- `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000` — aggregate per-turn budget

**Soothe gap**: `ToolOutputCapMiddleware` truncates but doesn't persist to disk.

### Parallel tool calls (BashTool prompt)
Claude Code's system prompt explicitly instructs the model to batch independent
tool calls in a single response. **Soothe state**: only mentions batching for
grep patterns — no cross-tool parallel-call instruction.

---

## 1. Explicit Parallel-Call Instruction

### Current state

The system prompt in
`packages/soothe-nano/src/soothe_nano/prompts/system_templates.py` has
domain-scoped guides (`_SHELL_GUIDE`, `_FILE_OPS_GUIDE`, `_SEARCH_GUIDE`,
etc.) assembled into `_TOOL_ORCHESTRATION_GUIDE`. The only parallel-call
hint is in `_SEARCH_GUIDE`:

```
- Content search: use the grep tool (literal pattern; batch parallel calls
  for multiple patterns).
```

This mentions batching only for grep. There is no instruction to batch
independent calls **across different tools** (e.g., `read_file` + `grep` +
`glob` in one response). The model serializes these, costing one full hop
per call.

### Design

Add a `_PARALLEL_CALL_GUIDE` constant and inject it into
`_TOOL_ORCHESTRATION_GUIDE` between `_FILE_OPS_GUIDE` and `_SEARCH_GUIDE`.

```python
_PARALLEL_CALL_GUIDE = """\
Parallel tool calls (CRITICAL for efficiency):
- When you need multiple INDEPENDENT pieces of information, issue ALL tool \
calls in a single response. The runtime executes them concurrently.
- Examples of batchable independent calls:
  - read_file(A) + read_file(B) + read_file(C) → one response, 3 calls
  - grep("pattern1") + grep("pattern2") + glob("*.py") → one response
  - read_file(config) + grep("API_KEY") + ls(directory) → one response
- NEVER serialize independent calls. If call B does not depend on the \
result of call A, issue them together.
- Only serialize when output of call A determines arguments of call B.
"""
```

### Placement in `_TOOL_ORCHESTRATION_GUIDE`

```python
_TOOL_ORCHESTRATION_GUIDE = f"""\

Tool selection rules (follow strictly):

{_SHELL_GUIDE}

{_FILE_OPS_GUIDE}

{_PARALLEL_CALL_GUIDE}

{_SEARCH_GUIDE}

{_SURGICAL_EDIT_GUIDE}

...
"""
```

### Why this reduces tool calls

Doesn't reduce tool *invocations* — reduces model *hops* from N to 1 for
independent operations. Each hop is a full LLM round-trip. This is the
single highest-impact change: 30–50% fewer hops for multi-info tasks.

### Risk

None. Prompt-only change. No middleware, no state, no config.

### Verification

- **Parallel call rate**: percentage of model responses that issue 2+ tool
  calls. Target: >40% for multi-info tasks (currently ~15%).
- No regression in single-call tasks (the instruction says "independent"
  calls only).

---

## 2. Cross-Step Tool Result Eviction

### Current state

Soothe nano has no microcompact equivalent. Old `read_file` / `grep` /
`glob` / `run_command` results persist in the message history indefinitely.
`ToolOutputCapMiddleware` truncates individual outputs at write time but
never evicts old results from context.

Claude Code's `microCompact` evicts old tool results when input tokens
exceed 180K, keeping the last 40K tokens, using API-native
`clear_tool_uses_20250919` context-edit strategy (no extra model call).

### Design

New middleware: `ToolResultEvictionMiddleware` in
`packages/soothe-nano/src/soothe_nano/middleware/tool_result_eviction.py`.

#### Eviction policy

```python
class ToolResultEvictionMiddleware(AgentMiddleware):
    """Evict old tool results from context to prevent re-read churn.

    Replaces ToolMessage.content for old evictable tool results with a
    compact stub when total tool-result token estimate exceeds a threshold.
    Runs in ``awrap_model_call`` so the eviction is visible to the model
    on the next hop without persisting to checkpoint.
    """

    EVICTABLE_TOOLS = frozenset({
        "read_file", "grep", "glob", "run_command",
        "run_python", "ls", "file_info",
    })
    # Never evict — model depends on confirmation of mutation
    NON_EVICTABLE_TOOLS = frozenset({
        "write_file", "edit_file", "edit_lines", "insert_lines",
        "delete_lines", "apply_diff", "delete", "move_file",
        "write_todos", "task",
    })
    MAX_TOOL_RESULT_TOKENS = 60_000
    # Never evict the most recent N tool results (model needs them)
    PROTECT_RECENT_COUNT = 3
```

#### Hook point: `awrap_model_call`

```python
async def awrap_model_call(
    self,
    request: ModelRequest,
    handler,
) -> ModelResponse:
    messages = self._effective_messages_for_prompt(request)
    tool_result_tokens = self._estimate_tool_result_tokens(messages)

    if tool_result_tokens <= self.MAX_TOOL_RESULT_TOKENS:
        return await handler(request)

    # Find evictable ToolMessages, oldest first, skipping protected recent ones
    evictable = self._find_evictable(messages)
    for msg in evictable:
        if tool_result_tokens <= self.MAX_TOOL_RESULT_TOKENS:
            break
        evicted_tokens = self._estimate_tokens(msg.content)
        msg.content = self._build_stub(msg)
        tool_result_tokens -= evicted_tokens

    return await handler(request)
```

#### Stub format

```
[Evicted: read_file(file_path=src/foo.py, offset=0, limit=100) — re-read if needed]
```

The stub preserves the tool name and key arguments so the model can decide
whether re-reading is necessary.

#### Token estimation

Reuse the existing token estimation from `soothe_nano` (the `TokenEstimator`
used by `SystemPromptMiddleware` for prompt-verbosity routing). If
unavailable, fall back to `len(content) // 4` (rough char-to-token ratio).

#### Config gate

```python
# In SootheConfig (or agent_middleware_config):
tool_result_eviction:
  enabled: true       # default: true
  max_tokens: 60000   # default: 60000
  protect_recent: 3   # default: 3
```

### Middleware stack placement

Insert after `ToolOutputCapMiddleware` (step 9 in `_builder.py`) and before
`InvalidToolHintsMiddleware` (step 10). The eviction middleware operates on
the message list that the model sees, so it must run after output capping
but before the model call:

```python
# 9. Cap tool output before graph state / model context
stack.append(ToolOutputCapMiddleware(...))

# 9b. Evict old tool results to prevent re-read churn (microcompact analog)
if config.tool_result_eviction.enabled:
    from .tool_result_eviction import ToolResultEvictionMiddleware
    stack.append(ToolResultEvictionMiddleware(config=config))

# 10. Progressive builtin-tool loading (optional)
stack.append(InvalidToolHintsMiddleware())
```

### Why this reduces tool calls

Without eviction, the model sees stale content buried in context and
re-reads "just to be sure." With eviction, the model sees the stub and skips
the re-read unless it genuinely needs fresh content. The net effect is
15–25% fewer re-reads in long sessions.

### Risk

**Model may re-read evicted files.** Mitigation: the stub includes the
path and key arguments, so the model can decide if re-reading is needed.
Most evicted results are never re-read. The net effect is still fewer calls.

**Checkpoint persistence.** If eviction runs in `awrap_model_call`, it
modifies the in-flight message list but does not persist to the LangGraph
checkpoint. This means eviction is re-applied on every hop (idempotent).
If we want eviction to persist across checkpoints, we need a state
reducer that rewrites historical messages — this is more complex and is
deferred to a follow-up if the non-persistent approach proves insufficient.

### Verification

- **Re-read rate**: percentage of `read_file` calls that re-read a path
  already read in the same thread. Target: <15% (currently ~30%+).
- **Context token trend**: tool-result tokens should plateau, not grow
  linearly with session length.

---

## 3. Shrink Core Tool Set

### Current state

`packages/soothe-nano/src/soothe_nano/toolkits/progressive/registry.py`
defines `DEFAULT_CORE_TOOL_NAMES` with 26 tools:

```python
DEFAULT_CORE_TOOL_NAMES = frozenset({
    "ls", "read_file", "write_file", "edit_file", "glob", "grep",
    "write_todos", "task",
    "delete", "edit_lines", "insert_lines", "delete_lines", "apply_diff",
    "file_info",
    "run_command", "run_python", "run_background",
    "tail_background_log", "kill_process",
    "current_datetime",
    "search_tools", "search_skills", "invoke_skill",
    "search_mcp_tools", "mcp_resources_list", "mcp_resources_read",
})
```

Every tool in the core set sends full JSONSchema in every model call. Claude
Code's `ToolSearchTool` defers ALL non-essential tools, keeping the initial
bound set to ~10.

### Design

Move these 6 tools from `DEFAULT_CORE_TOOL_NAMES` to deferred:

| Tool | Reason |
|------|--------|
| `file_info` | Rarely needed; `ls` + `read_file` cover most cases |
| `apply_diff` | Only needed for patch workflows; `edit_lines` covers most edits |
| `delete` | Destructive; better to require explicit promotion |
| `tail_background_log` | Only needed when background jobs exist |
| `kill_process` | Same — only needed when background jobs exist |
| `current_datetime` | Rarely needed; can be injected into system prompt instead |

This shrinks the core set from 26 to 20 tools.

```python
DEFAULT_CORE_TOOL_NAMES = frozenset({
    # Filesystem
    "ls", "read_file", "write_file", "edit_file", "glob", "grep",
    # Surgical file ops (edit_lines is the primary edit tool)
    "edit_lines", "insert_lines", "delete_lines",
    # Execution
    "run_command", "run_python", "run_background",
    # Task delegation
    "write_todos", "task",
    # Progressive discovery
    "search_tools", "search_skills", "invoke_skill",
    # MCP progressive disclosure
    "search_mcp_tools", "mcp_resources_list", "mcp_resources_read",
})

# Tools moved to deferred (promoted on search_tools or direct invoke):
DEFERRED_FROM_CORE = frozenset({
    "file_info", "apply_diff", "delete",
    "tail_background_log", "kill_process",
    "current_datetime",
})
```

### System prompt update

Update `_TOOL_ORCHESTRATION_GUIDE` in `system_templates.py` to reflect the
new core set. The `Progressive tool binding` section currently lists
`tail_background_log`, `kill_process`, and `current_datetime` as
"always bound" — these must move to the `<AVAILABLE_TOOLS>` deferred list:

```python
# Before:
# - Always bound: filesystem, surgical edits, execution (run_command,
#   run_python, run_background, tail_background_log, kill_process),
#   search_tools, search_skills, invoke_skill, write_todos, task,
#   current_datetime.

# After:
# - Always bound: filesystem, surgical edits (edit_lines, insert_lines,
#   delete_lines), execution (run_command, run_python, run_background),
#   search_tools, search_skills, invoke_skill, write_todos, task.
# - Deferred (promote via search_tools or direct invoke): file_info,
#   apply_diff, delete, tail_background_log, kill_process,
#   current_datetime.
```

Also update `_SHELL_GUIDE` to note that `tail_background_log` and
`kill_process` are now deferred:

```python
_SHELL_GUIDE = """\
Execution tools (always bound — not listed in <AVAILABLE_TOOLS>):
- run_command: Sync shell ...
- run_background: Async shell ...
- run_python: Execute Python code ...

Background job management (deferred — search_tools to promote):
- tail_background_log: Read the last N lines from a run_background log.
- kill_process: Terminate a run_background PID only.

Choose run_command vs run_background:
...
"""
```

And remove `current_datetime` from the inline mention:

```python
# Before:
- datetime: Get current date and time.

# After: (remove this line — current_datetime is now deferred)
```

### Why this reduces tool calls

1. Less system prompt token cost per hop (~2–4K tokens saved).
2. Less decision paralysis — fewer tools to choose between.
3. The model is more likely to use the right tool the first time instead
   of trying a deferred tool that needs a `search_tools` round-trip.

### Risk

**Model may not find `tail_background_log` when it needs it.** Mitigation:
the `_SHELL_GUIDE` mentions these tools by name and says
"search_tools to promote." The model already knows the pattern from
other deferred tools (data inspection, research, etc.).

**`current_datetime` removal may break datetime-dependent workflows.**
Mitigation: the system prompt already includes a `<TIMESTAMP>` block
(see `system_prompt.py` — the executor injects the current datetime at
`system_templates.py:221`). The model can read the timestamp from there
without calling the tool.

### Verification

- **Tool schema tokens**: measure system prompt token count before/after.
  Target: ~2–4K token reduction.
- **search_tools calls for moved tools**: should be near zero for
  `current_datetime` (timestamp is in prompt) and low for others.

---

## 4. Multi-File Read Coalescing

### Current state

`EditCoalescingMiddleware` in
`packages/soothe-nano/src/soothe_nano/middleware/edit_coalescing.py`
collects parallel edit tool calls within a 50ms detection window, groups
them by file, and merges same-file edits into a single batched operation.

There is no read-side analog. When the model issues 3 `read_file` calls in
one response (which the parallel-call instruction in §1 will encourage),
they execute as 3 separate tool handlers, producing 3 separate
`ToolMessage` results with 3 message envelopes.

### Design

New middleware: `ReadCoalescingMiddleware` in
`packages/soothe-nano/src/soothe_nano/middleware/read_coalescing.py`.

This is the read-side analog of `EditCoalescingMiddleware`. It uses the
same detection-window pattern:

```python
class ReadCoalescingMiddleware(AgentMiddleware):
    """Coalesce parallel read_file calls into a single merged result.

    Collects read_file tool calls within a detection window, executes
    them concurrently, and returns a single merged ToolMessage with
    all file contents. This reduces context overhead (fewer message
    envelopes) and makes cross-file referencing easier for the model.
    """

    READ_TOOL_NAME = "read_file"
    DEFAULT_DETECTION_WINDOW_MS = 50  # Same as EditCoalescingMiddleware

    def __init__(self, *, detection_window_ms: int = 50) -> None:
        self._detection_window_ms = detection_window_ms
        self._pending_reads: list[PendingRead] = []
        self._window_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
```

#### Detection window

Same 50ms window as `EditCoalescingMiddleware`. When a `read_file` call
arrives:

1. If no window is active, start a 50ms timer and add the call to
   `_pending_reads`.
2. If a window is active, add the call to `_pending_reads`.
3. When the timer fires, execute all pending reads concurrently via
   `asyncio.gather`, merge results, and resolve futures.

#### Execution approach

The model expects one `ToolMessage` per `tool_call_id`, so we cannot
merge into a single message. Instead: execute all pending reads
concurrently (one `asyncio.gather`), but return individual `ToolMessage`
results per call. The benefit is:
- Concurrent I/O (reads happen in parallel, not sequentially)
- Single detection-window overhead instead of N separate handler calls

```python
async def _flush_pending_reads(self) -> None:
    """Execute all pending reads concurrently and resolve futures."""
    async with self._lock:
        pending = self._pending_reads
        self._pending_reads = []
        self._window_task = None

    if not pending:
        return

    # Execute all reads concurrently
    results = await asyncio.gather(
        *[p.handler(p.request) for p in pending],
        return_exceptions=True,
    )

    for read, result in zip(pending, results, strict=True):
        if isinstance(result, Exception):
            result = ToolMessage(
                content=f"Error: {result}",
                tool_call_id=read.tool_call_id,
                name=self.READ_TOOL_NAME,
                status="error",
            )
        if not read.result_future.done():
            read.result_future.set_result(result)
```

#### Skip coalescing when

- Only 1 read_file in the window (no benefit, just latency).
- Any read_file has a different `offset`/`limit` that suggests the model
  wants separate results (rare — the merged format handles this fine).

#### Middleware stack placement

Insert after `ToolOptimizationMiddleware` (step 6) and before
`EditCoalescingMiddleware` (step 7) in `_builder.py`:

```python
# 6. Deterministic tool optimization (reuse/dedup/search consolidation)
stack.append(ToolOptimizationMiddleware())

# 6b. Multi-file read coalescing (concurrent read_file execution)
stack.append(ReadCoalescingMiddleware())

# 7. Edit coalescing for parallel file edits
stack.append(EditCoalescingMiddleware())
```

### Why this reduces tool calls

Doesn't reduce the count of tool call *requests*, but:
1. Reduces execution time (concurrent I/O vs sequential).
2. Reduces middleware overhead (one detection-window cycle vs N).
3. Makes cross-file referencing easier — the model sees all file contents
   arrive together, reducing the urge to re-read for confirmation.

### Risk

**Detection window latency.** The 50ms window adds 50ms latency to every
`read_file` call. Mitigation: skip the window when only 1 read is pending
(execute immediately). The window only activates when 2+ reads arrive in
the same response.

**Interaction with `ToolOptimizationMiddleware` cache.** The optimization
middleware runs before read coalescing and may return cached results
without calling the handler. This is fine — cached results return
immediately and don't enter the coalescing window.

### Verification

- **Read execution time**: measure wall-clock time for 3 parallel
  `read_file` calls. Target: ~1x (concurrent) vs ~3x (sequential).
- **No regression**: single `read_file` calls should have <50ms overhead.

---

## 5. Search Consolidation in Existing Middleware

### Current state

`ToolOptimizationMiddleware` in
`packages/soothe-nano/src/soothe_nano/middleware/tool_optimization_middleware.py`
already has:
- Per-step lookup cache for `read_file` / `glob` / `grep` (lines 401–457)
- Duplicate empty-result blocking (lines 415–434)
- Shell search redirect (blocks `grep`/`rg`/`find` in `run_command`,
  lines 383–398)
- Read-file thrash guidance (lines 341–378)

It does NOT consolidate concurrent search calls within the same model
response. When the model issues `grep("foo")` + `grep("bar")` + `glob("*.py")`
in one response, they execute as 3 separate handler invocations.

### Design

Extend `ToolOptimizationMiddleware` with a search-batching layer. This
reuses the same detection-window pattern from `EditCoalescingMiddleware`
but for search tools.

#### New constants

```python
_SEARCH_BATCH_TOOLS = frozenset({"grep", "glob"})
```

#### Batched execution

When multiple `grep` / `glob` calls arrive within the detection window:

1. **Multiple `grep` calls with different patterns but same path** →
   single `rg` invocation with alternation (`rg 'pat1|pat2'`).
   - Return individual `ToolMessage` results per call (the model expects
     per-call results).
   - Split the combined output by pattern.

2. **Multiple `glob` calls** → single `glob` with brace expansion.
   - Return individual results per call.

3. **Mixed `grep` + `glob`** → execute concurrently via `asyncio.gather`
   (no merging possible, but concurrent I/O).

#### Implementation approach

Add a `_pending_searches` list and detection window to
`ToolOptimizationMiddleware`:

```python
class ToolOptimizationMiddleware(AgentMiddleware):
    ...
    _SEARCH_BATCH_TOOLS = frozenset({"grep", "glob"})
    _SEARCH_DETECTION_WINDOW_MS = 50

    def __init__(self) -> None:
        self._pending_searches: list[PendingSearch] = []
        self._search_window_task: asyncio.Task[None] | None = None
        self._search_lock = asyncio.Lock()
```

In `awrap_tool_call`, after the existing cache check but before calling
the handler, if the tool is in `_SEARCH_BATCH_TOOLS` and there are
already pending searches, add to the batch. Otherwise, start a new
detection window.

#### Consolidation logic

```python
async def _flush_pending_searches(self) -> None:
    """Execute pending searches with consolidation where possible."""
    async with self._search_lock:
        pending = self._pending_searches
        self._pending_searches = []
        self._search_window_task = None

    if not pending:
        return

    # Group greps by path
    grep_by_path: dict[str, list[PendingSearch]] = {}
    standalone: list[PendingSearch] = []
    for s in pending:
        if s.tool_name == "grep":
            path = str(s.args.get("path") or s.args.get("directory") or ".")
            grep_by_path.setdefault(path, []).append(s)
        else:
            standalone.append(s)

    # Consolidate same-path greps
    consolidated: list[Awaitable] = []
    for path, searches in grep_by_path.items():
        if len(searches) == 1:
            consolidated.append(searches[0].handler(searches[0].request))
        else:
            consolidated.append(
                self._consolidated_grep(searches, path)
            )

    # Add standalone globs (concurrent execution)
    for s in standalone:
        consolidated.append(s.handler(s.request))

    results = await asyncio.gather(*consolidated, return_exceptions=True)
    # Resolve futures...
```

### Why this reduces tool calls

Doesn't reduce the count of tool call *requests*, but:
1. Reduces filesystem I/O (one `rg` pass vs N).
2. Reduces execution time (concurrent + consolidated).
3. Faster execution reduces the window where the model might time out
   and retry.

### Risk

**Consolidated grep output splitting.** When multiple patterns are
consolidated into one `rg 'pat1|pat2'` invocation, the output contains
matches for both patterns interleaved. Splitting them back per-pattern
requires parsing the output. Mitigation: use `rg --json` output format
which includes the matched pattern in each result, making splitting
deterministic.

**Glob brace expansion.** `glob("*.py")` + `glob("*.ts")` →
`glob("*.{py,ts}")`. The results need to be split back by extension.
Mitigation: filter the combined results by extension per original call.

### Verification

- **Search execution time**: measure wall-clock time for 3 parallel
  `grep` calls to the same path. Target: ~1x (consolidated) vs ~3x
  (sequential).
- **Result correctness**: consolidated results must match per-call
  results exactly (same files, same line numbers).

---

## 6. Tool Result Disk Storage with Compact Reference

### Problem

Soothe nano currently uses a **truncate-and-discard** strategy for large tool
outputs. `ToolOutputCapMiddleware` hard-truncates `ToolMessage.content`
at `DEFAULT_TOOL_OUTPUT_CHARS` (10,000 chars). Once truncated, the content
is **permanently lost** to the model. If the model needs the omitted
portion (e.g., a function definition 15K chars into a 30K-char file read),
it must issue another `read_file` call with a different `offset` — costing
a full model hop.

Claude Code solves this with a fundamentally different strategy: **persist
large outputs to disk, replace the in-context content with a compact
reference containing a file path and preview**. The model can re-fetch the
full output via `FileReadTool` if needed, but the default path keeps
context small without information loss.

### Architecture overview

```
Tool handler returns ToolMessage (full content)
         │
         ▼
ToolResultStorageMiddleware.awrap_tool_call()
         │
         ├─ size <= threshold? → return as-is (no I/O)
         │
         ├─ size > threshold? → persist to disk, return compact reference
         │
         ▼
ToolOutputCapMiddleware (existing, unchanged)
         │
         ▼
Graph state / model context
```

The new middleware sits **before** `ToolOutputCapMiddleware` in the stack.
It intercepts large results and replaces them with compact references
*before* the cap middleware sees them. The cap middleware then becomes a
safety net for results that are above the persistence threshold but below
the hard cap — it truncates the *preview* if needed (rare, since previews
are 2K chars).

### 6.1 Disk persistence utility

**File**: `packages/soothe-nano/src/soothe_nano/middleware/tool_result_storage.py`

```python
"""Persist large tool results to disk instead of truncating.

Replaces the truncate-and-discard strategy with persist-to-disk + compact
reference. The model can re-fetch the full output via read_file if needed,
but the default path keeps context small without information loss.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
TOOL_RESULT_CLEARED_MESSAGE = "[Old tool result content cleared]"

# Default threshold: persist results larger than this to disk.
# Below this, content stays inline (no I/O overhead).
DEFAULT_PERSIST_THRESHOLD_CHARS = 10_000

# Preview size shown in the compact reference message.
DEFAULT_PREVIEW_CHARS = 2_000

# Per-message aggregate budget. When the total size of tool results in a
# single model response exceeds this, the largest results are persisted to
# disk until under budget.
DEFAULT_PER_MESSAGE_BUDGET_CHARS = 200_000


def _session_tool_results_dir(session_id: str, workspace_root: Path) -> Path:
    """Return the directory for persisting tool results for a session.

    Args:
        session_id: Unique session identifier.
        workspace_root: Workspace root path.

    Returns:
        Path to ``{workspace_root}/.soothe/tool-results/{session_id}``.
    """
    return workspace_root / ".soothe" / "tool-results" / session_id


def _get_tool_result_path(
    session_id: str,
    workspace_root: Path,
    tool_call_id: str,
    is_json: bool,
) -> Path:
    """Return the file path for a persisted tool result.

    Args:
        session_id: Session identifier.
        workspace_root: Workspace root path.
        tool_call_id: Unique tool call ID (used as filename).
        is_json: Whether the content is JSON (affects extension).

    Returns:
        File path for the persisted result.
    """
    ext = "json" if is_json else "txt"
    # Sanitize tool_call_id for filesystem safety
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_call_id)
    return _session_tool_results_dir(session_id, workspace_root) / f"{safe_id}.{ext}"


def _generate_preview(content: str, max_chars: int) -> tuple[str, bool]:
    """Generate a preview of content, truncating at a line boundary.

    Args:
        content: Full content string.
        max_chars: Maximum characters in the preview.

    Returns:
        Tuple of (preview, has_more). The preview is truncated at the last
        newline within max_chars to avoid cutting mid-line.
    """
    if len(content) <= max_chars:
        return content, False

    truncated = content[:max_chars]
    last_newline = truncated.rfind("\n")

    # If we found a newline reasonably close to the limit, use it
    cut_point = last_newline if last_newline > max_chars * 0.5 else max_chars
    return content[:cut_point], True


def _format_file_size(size: int) -> str:
    """Format a byte/char count as a human-readable size string.

    Args:
        size: Size in characters (approximated as bytes for display).

    Returns:
        Human-readable size string like "2.0KB" or "150.0KB".
    """
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def persist_tool_result(
    content: str,
    tool_call_id: str,
    session_id: str,
    workspace_root: Path,
) -> dict[str, Any] | None:
    """Persist a tool result to disk and return metadata.

    Writes the full content to
    ``{workspace_root}/.soothe/tool-results/{session_id}/{tool_call_id}.txt``
    and returns a dict with the file path, original size, and preview.

    Args:
        content: Full tool result content.
        tool_call_id: Unique tool call ID.
        session_id: Session identifier.
        workspace_root: Workspace root path.

    Returns:
        Dict with keys: filepath, original_size, preview, has_more.
        None if persistence failed (caller should fall back to truncation).
    """
    try:
        result_dir = _session_tool_results_dir(session_id, workspace_root)
        result_dir.mkdir(parents=True, exist_ok=True)
        filepath = _get_tool_result_path(
            session_id, workspace_root, tool_call_id, is_json=False
        )
        # Use 'x' flag (exclusive create) — if file exists, it was persisted
        # on a prior turn. This prevents re-writing on every hop.
        filepath.write_text(content, encoding="utf-8")
    except FileExistsError:
        # Already persisted on a prior turn — fall through to preview
        pass
    except OSError as exc:
        logger.warning(
            "[ToolResultStorage] Failed to persist result for %s: %s",
            tool_call_id,
            exc,
        )
        return None

    preview, has_more = _generate_preview(content, DEFAULT_PREVIEW_CHARS)
    return {
        "filepath": str(filepath),
        "original_size": len(content),
        "preview": preview,
        "has_more": has_more,
    }


def build_persisted_output_message(
    filepath: str,
    original_size: int,
    preview: str,
    has_more: bool,
) -> str:
    """Build the compact reference message for a persisted tool result.

    Args:
        filepath: Path to the persisted file on disk.
        original_size: Original content size in characters.
        preview: Preview text to include in the reference.
        has_more: Whether the preview was truncated.

    Returns:
        Compact reference string wrapped in persisted-output tags.
    """
    lines = [
        PERSISTED_OUTPUT_TAG,
        f"Output too large ({_format_file_size(original_size)}). "
        f"Full output saved to: {filepath}",
        "",
        f"Preview (first {_format_file_size(DEFAULT_PREVIEW_CHARS)}):",
        preview,
    ]
    if has_more:
        lines.append("...")
    lines.append(PERSISTED_OUTPUT_CLOSING_TAG)
    return "\n".join(lines)


def maybe_persist_tool_result(
    tool_message: ToolMessage,
    session_id: str,
    workspace_root: Path,
    persist_threshold: int = DEFAULT_PERSIST_THRESHOLD_CHARS,
) -> ToolMessage:
    """Persist a tool result to disk if it exceeds the threshold.

    Args:
        tool_message: Original ToolMessage with full content.
        session_id: Session identifier.
        workspace_root: Workspace root path.
        persist_threshold: Character threshold above which to persist.

    Returns:
        Original ToolMessage if under threshold, or a new ToolMessage with
        a compact reference if persisted. On persistence failure, returns
        the original (the existing ToolOutputCapMiddleware will truncate).
    """
    content = str(tool_message.content or "")
    if len(content) <= persist_threshold:
        return tool_message

    result = persist_tool_result(
        content,
        tool_message.tool_call_id,
        session_id,
        workspace_root,
    )
    if result is None:
        # Persistence failed — return original, let ToolOutputCapMiddleware
        # truncate as fallback
        return tool_message

    reference = build_persisted_output_message(
        result["filepath"],
        result["original_size"],
        result["preview"],
        result["has_more"],
    )
    return tool_message.model_copy(update={"content": reference})
```

### 6.2 Storage middleware

**File**: `packages/soothe-nano/src/soothe_nano/middleware/tool_result_storage_middleware.py`

```python
"""Middleware that persists large tool results to disk with compact references.

Sits before ToolOutputCapMiddleware in the stack. Intercepts large results
and replaces them with disk-backed compact references. The model can
re-fetch the full output via read_file if needed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from soothe_nano.middleware.tool_result_storage import (
    DEFAULT_PERSIST_THRESHOLD_CHARS,
    DEFAULT_PER_MESSAGE_BUDGET_CHARS,
    maybe_persist_tool_result,
)

logger = logging.getLogger(__name__)


class ToolResultStorageMiddleware(AgentMiddleware):
    """Persist large tool results to disk, replacing content with references.

    Operates in two modes:

    1. **Per-tool persistence** (awrap_tool_call): When a single tool result
       exceeds the persistence threshold, write it to disk and replace the
       content with a compact reference containing the file path and a preview.

    2. **Per-message budget** (awrap_model_call): When the aggregate size of
       tool results in a single turn exceeds the per-message budget, persist
       the largest results to disk until under budget.

    The middleware is idempotent: re-application on subsequent hops is a
    no-op because persisted results are already replaced with references
    (detected by the ``<persisted-output>`` tag prefix).
    """

    name = "ToolResultStorageMiddleware"

    PERSISTED_TAG = "<persisted-output>"

    def __init__(
        self,
        *,
        session_id: str,
        workspace_root: str,
        persist_threshold: int = DEFAULT_PERSIST_THRESHOLD_CHARS,
        per_message_budget: int = DEFAULT_PER_MESSAGE_BUDGET_CHARS,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._workspace_root = workspace_root
        self._persist_threshold = persist_threshold
        self._per_message_budget = per_message_budget

    def _is_already_persisted(self, content: Any) -> bool:
        """Check if content has already been replaced with a persisted reference.

        Args:
            content: ToolMessage content.

        Returns:
            True if content starts with the persisted-output tag.
        """
        return isinstance(content, str) and content.startswith(self.PERSISTED_TAG)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Persist large single-tool results to disk after execution.

        Args:
            request: Tool call request.
            handler: Next handler in the middleware chain.

        Returns:
            ToolMessage with compact reference if persisted, or original
            result if under threshold.
        """
        result = await handler(request)

        if isinstance(result, ToolMessage):
            content = str(result.content or "")
            if self._is_already_persisted(content):
                return result
            if len(content) <= self._persist_threshold:
                return result
            return maybe_persist_tool_result(
                result,
                self._session_id,
                self._workspace_root,
                self._persist_threshold,
            )

        if isinstance(result, Command):
            update = result.update
            if isinstance(update, dict):
                messages = update.get("messages")
                if isinstance(messages, list):
                    patched: list[Any] = []
                    changed = False
                    for msg in messages:
                        if isinstance(msg, ToolMessage):
                            content = str(msg.content or "")
                            if (
                                not self._is_already_persisted(content)
                                and len(content) > self._persist_threshold
                            ):
                                msg = maybe_persist_tool_result(
                                    msg,
                                    self._session_id,
                                    self._workspace_root,
                                    self._persist_threshold,
                                )
                                changed = True
                        patched.append(msg)
                    if changed:
                        return Command(update={**update, "messages": patched})
        return result

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Enforce per-message aggregate budget on tool results.

        When the total size of tool results in the message list exceeds the
        per-message budget, persist the largest results to disk until under
        budget.

        Args:
            request: Model request containing messages.
            handler: Next handler in the middleware chain.

        Returns:
            Model response from handler (with possibly modified messages).
        """
        messages = list(getattr(request, "messages", None) or [])
        if not messages:
            return await handler(request)

        # Collect tool result candidates from the latest user message
        candidates: list[tuple[int, ToolMessage, int]] = []
        for i, msg in enumerate(messages):
            if isinstance(msg, ToolMessage):
                content = str(msg.content or "")
                if self._is_already_persisted(content):
                    continue
                size = len(content)
                if size > self._persist_threshold:
                    candidates.append((i, msg, size))

        if not candidates:
            return await handler(request)

        total_size = sum(size for _, _, size in candidates)
        if total_size <= self._per_message_budget:
            return await handler(request)

        # Sort by size descending — persist largest first
        candidates.sort(key=lambda x: x[2], reverse=True)

        patched = list(messages)
        changed = False
        for idx, msg, size in candidates:
            if total_size <= self._per_message_budget:
                break
            persisted = maybe_persist_tool_result(
                msg,
                self._session_id,
                self._workspace_root,
                self._persist_threshold,
            )
            if persisted is not msg:
                patched[idx] = persisted
                new_size = len(str(persisted.content or ""))
                total_size -= size - new_size
                changed = True

        if changed:
            request = request.override(messages=patched)
        return await handler(request)
```

### 6.3 Config additions

**File**: `packages/soothe-nano/src/soothe_nano/config/models.py`

```python
class ToolResultStorageConfig(BaseModel):
    """Configuration for tool result disk storage.

    Args:
        enabled: Whether to persist large tool results to disk.
        persist_threshold_chars: Character threshold above which to persist.
        per_message_budget_chars: Aggregate per-turn budget for tool results.
        preview_chars: Preview size shown in the compact reference.
    """

    enabled: bool = True
    persist_threshold_chars: int = 10_000
    per_message_budget_chars: int = 200_000
    preview_chars: int = 2_000
```

**File**: `packages/soothe-nano/src/soothe_nano/config/constants.py`

```python
DEFAULT_TOOL_RESULT_PERSIST_THRESHOLD = 10_000
DEFAULT_TOOL_RESULT_PER_MESSAGE_BUDGET = 200_000
DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 2_000
```

### 6.4 Middleware stack wiring

**File**: `packages/soothe-nano/src/soothe_nano/middleware/_builder.py`

Insert the storage middleware **before** `ToolOutputCapMiddleware` (step 9):

```python
# 8b. Catch-all tool error guard
stack.append(ToolErrorGuardMiddleware())

# 8c. Persist large tool results to disk (before output cap)
storage_config = config.tool_result_storage
if storage_config.enabled:
    from .tool_result_storage_middleware import ToolResultStorageMiddleware

    stack.append(
        ToolResultStorageMiddleware(
            session_id=config.session_id,
            workspace_root=config.filesystem_middleware.workspace_root,
            persist_threshold=storage_config.persist_threshold_chars,
            per_message_budget=storage_config.per_message_budget_chars,
        )
    )
    logger.info("[Middleware] Tool result disk storage enabled")

# 9. Cap tool output before graph state / model context
stack.append(
    ToolOutputCapMiddleware(
        default_max_chars=int(tool_output.tool_output_max_chars),
        code_exec_max_chars=int(tool_output.code_exec_max_output_chars),
    )
)

# 9b. Evict old tool results to prevent re-read churn (microcompact analog)
if config.tool_result_eviction.enabled:
    from .tool_result_eviction import ToolResultEvictionMiddleware
    stack.append(ToolResultEvictionMiddleware(config=config))
```

### 6.5 Disk layout

```
{workspace_root}/
  .soothe/
    tool-results/
      {session_id}/
        toolu_abc123.txt      # persisted read_file result
        toolu_def456.json     # persisted JSON tool result
        toolu_ghi789.txt      # persisted run_command output
```

- Files are named by `tool_call_id` (sanitized for filesystem safety).
- Extension is `.txt` for string content, `.json` for JSON-serializable
  content.
- Files are written with exclusive-create semantics to prevent re-writing
  on every hop.
- The `.soothe/tool-results/` directory follows the existing `.soothe/`
  workspace layout (same parent as `.soothe/backups/`).

### 6.6 Model guidance

The model needs to know that persisted outputs exist and how to re-fetch
them. Add a note to `_TOOL_ORCHESTRATION_GUIDE` in
`packages/soothe-nano/src/soothe_nano/prompts/system_templates.py`:

```python
_LARGE_RESULT_GUIDE = """\
Large tool outputs:
- When a tool result says "Full output saved to: <path>", the full content \
is on disk. Use read_file with that path to retrieve it if needed.
- You rarely need to re-read the full output — the preview contains the \
most relevant portion (first 2KB). Only re-read if the preview is \
insufficient.
"""
```

### Why this reduces tool calls

1. **Eliminates re-reads for truncated content.** When a 30K-char file read
   is truncated at 10K, the model often re-reads with a different offset to
   see the rest. With disk storage, the model sees a preview + file path and
   can `read_file` the persisted path with a specific offset if needed — one
   targeted read instead of multiple exploratory re-reads.

2. **Preserves information across hops.** Truncated content is lost forever.
   Persisted content is recoverable. The model doesn't need to "guess" what
   was omitted — it knows the file path and can fetch it.

3. **Reduces context pressure.** A 50K-char `run_command` output occupies
   ~12.5K tokens in context. A 2K-char reference occupies ~500 tokens. That's
   a 96% reduction for the same information, with no loss.

4. **Enables future eviction-with-recovery.** Once disk storage exists, the
   eviction middleware (§2) can persist-then-evict, giving the model a
   recovery path that Claude Code's eviction doesn't have.

### Risk analysis

**R1: Disk I/O latency.** Writing large results to disk adds I/O latency.
Mitigation: the threshold is 10K chars — only results above this are
persisted. Most tool results are well under 10K. For results above 10K, the
disk write (~1ms for 10K chars on SSD) is negligible compared to the tool
execution time itself.

**R2: Disk space consumption.** Persisted results accumulate on disk over a
long session. Mitigation: results are stored under
`.soothe/tool-results/{session_id}/` — scoped per session, cleaned up when
the session directory is removed. Each result is written once
(exclusive-create flag).

**R3: Workspace root resolution.** The middleware needs `session_id` and
`workspace_root` at construction time. Mitigation: `workspace_root` is
available from `config.filesystem_middleware.workspace_root`. `session_id`
can be resolved from the runtime context at middleware construction time.
If not available, fall back to a process-level unique directory.

**R4: Interaction with ToolOutputCapMiddleware.** The cap middleware runs
after the storage middleware and may re-truncate the compact reference.
Mitigation: the compact reference is ~2K chars — well under the 10K cap.
The cap middleware will be a no-op for persisted results.

**R5: Non-text content (images, multimodal).** `read_file` on PDFs/images
returns multimodal content blocks that cannot be persisted as text.
Mitigation: `maybe_persist_tool_result` checks `isinstance(content, str)`.
If content is a list of blocks, it is left untouched.

**R6: Checkpoint persistence.** If the middleware modifies
`ToolMessage.content` at the `awrap_tool_call` layer, the modified content
is written to the LangGraph checkpoint. On resume, the model sees the
compact reference, not the original content. This is actually the desired
behavior — the reference is self-contained (includes the file path). On
resume, the model can `read_file` the persisted path if needed. The
persisted file survives across resumes because it's on disk.

### Verification

- **Re-read rate for large files**: percentage of `read_file` calls that
  re-read a path already read in the same thread where the original read
  exceeded 10K chars. Target: <10% (currently ~30%+ due to truncation).
- **Context token reduction**: measure total tool-result tokens in context
  after 10 tool calls that produce large outputs. Target: 80%+ reduction
  vs. truncate-and-discard.
- **Disk I/O overhead**: measure wall-clock time added by persistence for
  results > 10K chars. Target: <5ms per persisted result (SSD).
- **Recovery success rate**: when the model does `read_file` on a persisted
  path, does it succeed? Target: 100%.

---

## Relationship Between §2 (Eviction) and §6 (Disk Storage)

These two items are **complementary**, not competing:

| Concern | §6 Disk Storage | §2 Eviction |
|---------|-----------------|-------------|
| **What** | Persist *large individual results* to disk | Evict *old accumulated results* from context |
| **When** | At write time (tool returns) | At model-call time (before next hop) |
| **Recovery** | Model can `read_file` the persisted path | Model must re-issue the original tool call |
| **Information loss** | None — full content on disk | Yes — content replaced with stub |
| **Target** | Results > 10K chars | Total tool-result tokens > 60K |

### Future integration: persist-then-evict

The eviction middleware (§2) can be enhanced to *persist-then-evict* — when
evicting an old tool result, first persist it to disk (if not already
persisted via §6), then replace with a stub that includes the disk path:

```
[Evicted: read_file(file_path=src/foo.py) — full output at /path/to/.soothe/tool-results/{session}/toolu_abc.txt]
```

This is strictly better than Claude Code's eviction (which has no recovery
path) and is enabled by having the storage infrastructure from §6.

**Recommended approach**: Start with stub-only eviction (simpler), add
persist-then-evict as a follow-up once §6 is stable.

---

## Implementation Order

| Phase | Item | Effort | Dependencies |
|-------|------|--------|-------------|
| 1 | §1 Parallel-call instruction | S (prompt edit) | None |
| 1 | §3 Shrink core tool set | S (config + prompt) | None |
| 1 | §6 Disk persistence utility + config | S | None |
| 2 | §6 Storage middleware + wiring | M | Phase 1 (utility + config) |
| 2 | §6 Model guidance in system prompt | S | Phase 2 |
| 2 | §2 Tool result eviction | M (new middleware) | None (stub-only); §6 for persist-then-evict |
| 2 | §4 Multi-file read coalescing | M (new middleware) | §1 (parallel calls make coalescing more impactful) |
| 3 | §5 Search consolidation | S (extend existing) | §1 (parallel calls make consolidation more impactful) |
| 3 | §6 Tests | M | Phase 2 |

Phase 1 items are prompt/config/utility changes with no code risk and should
ship first. Phase 2 items are new middleware. Phase 3 extends existing
middleware and adds tests.

---

## Test Location

Per development-process rules, tests go in
`packages/soothe-nano/tests/unit/middleware/`:

| Item | Test file |
|------|-----------|
| §1 Parallel-call instruction | `test_system_prompt_tool_extraction.py` (extend) or new `test_parallel_call_guide.py` |
| §2 Tool result eviction | `test_tool_result_eviction.py` (new) |
| §3 Shrink core tool set | `test_progressive_tool_middleware.py` (extend) |
| §4 Multi-file read coalescing | `test_read_coalescing.py` (new) |
| §5 Search consolidation | `test_tool_optimization_middleware.py` (extend) |
| §6 Disk storage | `test_tool_result_storage.py` (new) |

### §6 test cases

| Test | What it verifies |
|------|-----------------|
| `test_persist_under_threshold` | Results under 10K are not persisted |
| `test_persist_over_threshold` | Results over 10K are persisted to disk |
| `test_persisted_reference_format` | Reference message has correct format |
| `test_persisted_preview_line_boundary` | Preview truncates at line boundary |
| `test_persist_idempotent` | Re-application on subsequent hop is no-op |
| `test_persist_failure_fallback` | On disk write failure, original is returned |
| `test_per_message_budget` | Aggregate budget enforcement on parallel results |
| `test_per_message_budget_skip_small` | Small results stay inline |
| `test_non_text_content_untouched` | Multimodal content is not persisted |
| `test_persisted_file_survives_resume` | File on disk after checkpoint restore |

---

## What NOT to Implement

- **Hard tool-call limits.** Claude Code does not enforce a hard cap.
  Hard limits cause the model to give up prematurely. Use soft nudges only.
- **Automatic tool selection.** Do not try to predict which tool the model
  should use. The model's judgment is better than any heuristic. Only
  optimize the *environment* (fewer tools, better prompts, result eviction).
- **Tool merging at the API level.** Do not merge multiple tool calls into
  a single API tool-call. The model needs per-tool results to reason
  correctly. Merge only at the execution layer (§4, §5).
- **Persistent eviction in checkpoint.** The `awrap_model_call` approach
  is non-persistent (re-applied each hop). A persistent state-reducer
  approach is more complex and deferred until the non-persistent approach
  proves insufficient.

---

## Verification Metrics

After implementation, measure:

1. **Tool calls per goal**: count tool-call messages in a fixed benchmark
   suite.
2. **Model hops per goal**: count AIMessage turns. Target: reduce by 20%+
   for multi-step goals.
3. **Re-read rate**: percentage of `read_file` calls that re-read a path
   already read in the same thread. Target: <15% (currently ~30%+).
4. **Parallel call rate**: percentage of model responses that issue 2+
   tool calls. Target: >40% for multi-info tasks (currently ~15%).
5. **System prompt token count**: measure before/after core tool set
   shrink. Target: ~2–4K token reduction.
6. **Context token reduction (§6)**: measure total tool-result tokens in
   context after 10 tool calls that produce large outputs. Target: 80%+
   reduction vs. truncate-and-discard.

---

## Open Questions

1. **Eviction persistence (§2)**: Should tool result eviction use
   LangGraph's state reducer to modify historical messages, or operate
   at the model-call layer by rewriting the message list in
   `awrap_model_call`? The latter is simpler but doesn't persist across
   checkpoints. Decision: start with `awrap_model_call` (non-persistent);
   revisit if re-eviction overhead is measurable.

2. **Read coalescing window (§4)**: The `EditCoalescingMiddleware` uses
   50ms. Is this appropriate for `ReadCoalescingMiddleware`? Model
   responses with many reads may take >50ms to generate, but the calls
   arrive in a single response batch, so 50ms should be sufficient.
   Decision: start with 50ms, measure.

3. **`current_datetime` removal (§3)**: The system prompt already has a
   `<TIMESTAMP>` block (confirmed at `system_templates.py:221`). Confirm
   this is always present before removing the tool from core. If the
   timestamp is conditional, keep the tool in core.

4. **Search consolidation output splitting (§5)**: `rg --json` output
   includes the matched pattern per result, making splitting
   deterministic. Confirm the `grep` tool implementation supports
   `--json` output or add it.

5. **Session ID availability (§6)**: Is `session_id` available at middleware
   stack construction time in `_builder.py`? The executor has it, but
   `build_soothe_middleware_stack` may be called before the session is
   fully initialized. If not, fall back to a runtime ContextVar that is
   set when the session starts.

6. **Cleanup of persisted files (§6)**: Should persisted results be
   cleaned up when the corresponding tool call is no longer in the
   message history (e.g., after compaction)? Recommendation: persist for
   session lifetime, clean up on session end.

7. **Persist-then-evict integration (§2 + §6)**: Should the eviction
   middleware persist-then-evict (using §6's storage utility) or
   evict-with-stub (no recovery path)? Recommendation: start with
   stub-only eviction (simpler), add persist-then-evict as a follow-up
   once §6 is stable.

8. **JSON content detection (§6)**: Should the middleware attempt to
   detect JSON content and persist as `.json`? Or always persist as
   `.txt`? Recommendation: always `.txt` for simplicity. The model can
   parse JSON from a `.txt` file just as well.

---

## Appendix A: Eviction Quality and Number Control

This appendix analyzes the mechanisms for controlling **eviction quality**
(what gets evicted vs. retained) and **eviction number** (how many results
are evicted per cycle), drawing on Claude Code's microcompact implementation
as the reference design.

### A.1 Claude Code's control surface

Claude Code's eviction system (`microCompact.ts`, `apiMicrocompact.ts`,
`timeBasedMCConfig.ts`, `toolResultStorage.ts`) exposes six distinct control
dimensions:

#### A.1.1 Tool eligibility (quality control)

Two separate allowlists control *what can be evicted*:

```typescript
// microCompact.ts:41-50 — client-side content clearing
const COMPACTABLE_TOOLS = new Set([
  FILE_READ_TOOL_NAME,      // read
  ...SHELL_TOOL_NAMES,      // bash
  GREP_TOOL_NAME,           // grep
  GLOB_TOOL_NAME,           // glob
  WEB_SEARCH_TOOL_NAME,     // web search
  WEB_FETCH_TOOL_NAME,      // web fetch
  FILE_EDIT_TOOL_NAME,      // edit (tool_use, not result)
  FILE_WRITE_TOOL_NAME,     // write (tool_use, not result)
])

// apiMicrocompact.ts:19-32 — API-native context editing
const TOOLS_CLEARABLE_RESULTS = [
  ...SHELL_TOOL_NAMES, GLOB_TOOL_NAME, GREP_TOOL_NAME,
  FILE_READ_TOOL_NAME, WEB_FETCH_TOOL_NAME, WEB_SEARCH_TOOL_NAME,
]
const TOOLS_CLEARABLE_USES = [
  FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME, NOTEBOOK_EDIT_TOOL_NAME,
]
```

Key design: **mutation tools are never evicted from results** — the model
needs confirmation that its write succeeded. Only read/search tools are
evictable. Edit/write tool *uses* (the call blocks) can be cleared, but
their *results* (success confirmations) are kept.

#### A.1.2 Token threshold (number control)

```typescript
// apiMicrocompact.ts:16-17
const DEFAULT_MAX_INPUT_TOKENS = 180_000   // trigger threshold
const DEFAULT_TARGET_INPUT_TOKENS = 40_000  // keep last 40K tokens
```

Eviction fires when **estimated input tokens** exceed 180K. It evicts oldest
first until the remaining tool-result tokens drop below ~40K — a 140K token
clearing window. The token estimate uses `roughTokenCountEstimation()` with
a 4/3 conservative padding factor (`estimateMessageTokens`).

#### A.1.3 Keep-recent count (quality control)

```typescript
// timeBasedMCConfig.ts:30-34
const TIME_BASED_MC_CONFIG_DEFAULTS = {
  enabled: false,
  gapThresholdMinutes: 60,
  keepRecent: 5,
}
```

Even when time-based eviction fires (idle > 60 min, cache expired), the
**last 5 compactable tool results are always protected**. This ensures the
model retains working memory for the current task.

#### A.1.4 Time-based trigger (number control)

When the gap since the last assistant message exceeds 60 minutes, the
server-side prompt cache has expired (1h TTL). Since the full prefix will be
rewritten regardless, clearing old tool results *before* the request shrinks
what gets rewritten — a cost optimization, not a context-pressure response.

#### A.1.5 Per-message aggregate budget (number control)

```typescript
// toolLimits.ts:13,49
const DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000         // per-tool
const MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000   // per-turn aggregate
```

When N parallel tools each produce large results, the aggregate budget
(200K chars ≈ 50K tokens) forces the largest results to disk first. This
prevents a single turn from flooding context. The budget is **per-message**,
not cumulative — each turn is evaluated independently.

#### A.1.6 Content replacement state (quality control)

```typescript
// toolResultStorage.ts:390-393
type ContentReplacementState = {
  seenIds: Set<string>          // frozen decisions
  replacements: Map<string, string>  // exact replacement strings
}
```

Once a tool result is evaluated for persistence, its fate is **frozen** for
the conversation. This ensures byte-identical re-application on subsequent
hops, preserving prompt cache prefix. The `replacements` Map stores the
exact replacement string (not derived on re-apply) so code changes to the
preview template can't break cache.

### A.2 Soothe's current state

Soothe has **no eviction**. The only control is `ToolOutputCapMiddleware`
(`reliability.py:249-324`), which truncates individual tool results at
write time:

| Control dimension | Claude Code | Soothe (current) |
|---|---|---|
| Tool eligibility | Separate result/use allowlists | None — all tools capped equally |
| Token threshold | 180K input tokens triggers eviction | None — no cumulative trigger |
| Keep-recent | Last 5 protected | None — no recency protection |
| Time-based trigger | 60-min gap → pre-request clear | None |
| Per-message budget | 200K aggregate, largest-first | None — per-tool 10K cap only |
| Replacement state | Frozen decisions, byte-identical re-apply | None — truncation is idempotent but lossy |

### A.3 Recommended controls for Soothe's eviction middleware (§2)

The IG-778 §2 design already includes three controls. Here I map them to
Claude Code's dimensions and identify gaps:

| §2 control | Claude Code equivalent | Gap |
|---|---|---|
| `EVICTABLE_TOOLS` frozenset | `COMPACTABLE_TOOLS` | ✅ Covered — read/search tools only |
| `NON_EVICTABLE_TOOLS` frozenset | (implicit: mutation tools absent from allowlist) | ✅ Covered — write/edit/delete excluded |
| `MAX_TOOL_RESULT_TOKENS = 60_000` | `DEFAULT_MAX_INPUT_TOKENS = 180_000` | ⚠️ See below |
| `PROTECT_RECENT_COUNT = 3` | `keepRecent = 5` | ⚠️ Lower — may evict too aggressively |

#### A.3.1 Threshold calibration

Soothe's proposed 60K token threshold is **3x lower** than Claude Code's
180K. This means eviction fires earlier and more often. The tradeoff:

- **Lower threshold** → smaller context, fewer tokens per hop, but more
  re-reads (model evicts results it still needs).
- **Higher threshold** → larger context, more tokens per hop, but fewer
  re-reads.

**Recommendation**: Start at 120K (2x lower than Claude Code, but 2x higher
than current IG-778 proposal). Soothe's context window may be smaller than
Claude Code's 200K; calibrate to `0.6 × model_context_window`.

```python
# Dynamic calibration formula:
MAX_TOOL_RESULT_TOKENS = int(model_context_window * 0.6)
# For 200K context → 120K threshold
# For 128K context → 76K threshold
```

#### A.3.2 Keep-recent calibration

Claude Code protects the last 5; IG-778 proposes 3. In agentic loops with
subagent delegation, the model often needs to reference results from 3-5
turns back. **Recommendation**: increase to 5, matching Claude Code.

```python
PROTECT_RECENT_COUNT = 5  # was 3
```

#### A.3.3 Missing: time-based trigger

Claude Code's time-based microcompact fires when idle > 60 minutes. Soothe
has 24/7 autonomous agents where long idle gaps are common. Without a
time-based trigger, tool results from hours ago persist in context, wasting
tokens on every hop.

**Recommendation**: Add a time-based eviction trigger:

```python
# In ToolResultEvictionMiddleware:
TIME_BASED_GAP_MINUTES = 30  # shorter than Claude Code's 60 — autonomous
                             # agents don't have prompt cache TTL concerns
                             # but do have stale-context concerns

async def awrap_model_call(self, request, handler):
    messages = self._effective_messages_for_prompt(request)

    # Time-based trigger: if gap since last assistant > 30 min, evict
    # all but the last keep_recent results
    if self._time_since_last_assistant(messages) > self.TIME_BASED_GAP_MINUTES:
        self._evict_all_but_recent(messages, keep=self.PROTECT_RECENT_COUNT)
        return await handler(request)

    # Token-based trigger: if tool-result tokens exceed threshold, evict
    # oldest first until under threshold
    ...
```

#### A.3.4 Missing: per-message aggregate budget

IG-778 §6 covers per-tool persistence but not per-message aggregate budget.
Claude Code's 200K per-message budget prevents a single turn with 5 parallel
`run_command` calls (each 40K) from flooding 200K tokens into context.

**Recommendation**: The §6 storage middleware's `awrap_model_call` already
enforces a per-message budget (`DEFAULT_PER_MESSAGE_BUDGET_CHARS = 200_000`).
This is the correct control. Ensure it runs **before** eviction so that
disk-persisted results (which are compact references) are counted as small,
not as their original size.

#### A.3.5 Missing: frozen replacement state

Claude Code freezes eviction decisions per tool_call_id so re-application
on subsequent hops is byte-identical (preserving prompt cache). Soothe's
proposed `awrap_model_call` approach is non-persistent — it re-evaluates
every hop. This is simpler but has two costs:

1. **Re-evaluation overhead**: every hop walks all messages and re-estimates
   tokens. For 100+ message sessions, this is measurable.
2. **Non-deterministic eviction**: if the token estimate fluctuates, a
   result evicted on hop N might not be evicted on hop N+1, causing the
   model to see inconsistent context.

**Recommendation**: Add a `_evicted_ids: set[str]` to the middleware that
tracks eviction decisions within a session. Once evicted, always evicted
(matches Claude Code's `seenIds` pattern). This is lightweight (no disk I/O,
just a set lookup) and makes eviction deterministic:

```python
class ToolResultEvictionMiddleware(AgentMiddleware):
    def __init__(self, *, config) -> None:
        ...
        self._evicted_ids: set[str] = set()  # frozen decisions

    def _build_stub(self, msg: ToolMessage) -> str:
        stub = f"[Evicted: {msg.name}(...) — re-read if needed]"
        self._evicted_ids.add(msg.tool_call_id)
        return stub

    def _is_evicted(self, tool_call_id: str) -> bool:
        return tool_call_id in self._evicted_ids
```

### A.4 Eviction quality scoring

To measure eviction *quality* (not just quantity), track these metrics:

| Metric | Formula | Target |
|---|---|---|
| **Eviction hit rate** | `re-reads_of_evicted / total_evicted` | <20% (80% of evictions are never re-read) |
| **Eviction precision** | `1 - (re-reads_of_evicted / total_evicted)` | >80% |
| **Protected retention** | `recent_results_retained / recent_results_total` | 100% (PROTECT_RECENT_COUNT never violated) |
| **Mutation safety** | `mutation_results_evicted / mutation_results_total` | 0% (never evict mutation confirmations) |
| **Token reduction ratio** | `tokens_after_eviction / tokens_before_eviction` | <50% (eviction halves tool-result tokens) |
| **Re-read cost** | `extra_read_calls_from_eviction / total_read_calls` | <10% (eviction causes <10% more reads) |

### A.5 Summary: control dimensions for Soothe eviction

| # | Control | Claude Code | IG-778 §2 (current) | Recommendation |
|---|---|---|---|---|
| 1 | Tool eligibility | Separate result/use allowlists | `EVICTABLE_TOOLS` / `NON_EVICTABLE_TOOLS` | ✅ Keep as-is |
| 2 | Token threshold | 180K input tokens | 60K tool-result tokens | ⬆ Increase to 120K (or `0.6 × context_window`) |
| 3 | Keep-recent | 5 | 3 | ⬆ Increase to 5 |
| 4 | Time-based trigger | 60-min gap | None | ➕ Add 30-min gap trigger |
| 5 | Per-message budget | 200K aggregate | 200K (in §6) | ✅ Already in §6 |
| 6 | Frozen decisions | `seenIds` Set | None | ➕ Add `_evicted_ids` set |
| 7 | Eviction quality metrics | Analytics events | Verification metrics | ✅ Add A.4 metrics |

### A.6 Interaction with §6 (disk storage)

The eviction middleware (§2) and disk storage middleware (§6) should be
composed in a specific order for maximum quality:

```
1. §6 ToolResultStorageMiddleware (awrap_tool_call)
   → Large results persisted to disk, replaced with compact reference
   → This happens at WRITE TIME (once, permanently)

2. §2 ToolResultEvictionMiddleware (awrap_model_call)
   → Old results (now compact references or small inline) evicted from context
   → Eviction sees compact references as small → they survive longer
   → If a persisted result is evicted, stub includes disk path (future: persist-then-evict)
```

This ordering means:
- §6 runs first at write time → large results become small references
- §2 runs second at model-call time → evicts old results (which are now
  small if they were persisted, or full-size if they weren't)
- The token threshold in §2 counts **post-persistence** size, so persisted
  results don't contribute much to the token budget → eviction targets
  non-persisted old results first

**This is strictly better than Claude Code**, which has no disk fallback for
evicted results. Soothe's persist-then-evict path gives the model a recovery
mechanism (read_file the persisted path) that Claude Code lacks.
