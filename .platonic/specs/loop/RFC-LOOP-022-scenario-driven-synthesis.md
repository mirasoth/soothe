# RFC-LOOP-022: Scenario-Driven Goal Completion Synthesis

**RFC**: 616
**Title**: Scenario-Driven Goal Completion Synthesis
**Status**: Implemented
**Kind**: Architecture Design
**Created**: 2026-04-28
**Authors**: Soothe Team
**Updated**: 2026-04-28
**Related**: RFC-LOOP-020, RFC-LOOP-009, IG-298, IG-299

## Abstract

Refactor goal completion synthesis from keyword-driven, length-categorized system to scenario-driven, adaptive synthesis generation. The new system uses intent classification and execution patterns to determine appropriate deliverable structure, with LLM self-judging depth and detail level based on goal context and evidence.

## Motivation

### Current Limitations

1. **Artificial goal_type concept**: Uses keyword-based classification (`architecture_analysis`, `research_synthesis`, etc.) derived from evidence regex patterns - narrow, brittle, and domain-specific
2. **Response length constraints**: Enforces word count categories (BRIEF/CONCISE/STANDARD/COMPREHENSIVE) that artificially limit synthesis depth without understanding goal context
3. **Generic synthesis prompts**: "Summarize what was accomplished" approach produces uniform outputs regardless of goal type
4. **Keyword-based rules**: Heavily relies on regex pattern matching for goal classification, incompatible with general agent philosophy

### Proposed Solution

Replace keyword-based classification and length constraints with:

- **Scenario-driven synthesis**: Different deliverable structures for different goal types (architecture design vs travel plan vs research synthesis)
- **Intent + execution pattern matching**: Classification uses `IntentClassification` and execution metadata, not regex
- **Adaptive depth control**: LLM self-judges appropriate detail level from goal + evidence + scenario structure
- **Extensible scenario library**: Built-in templates for common scenarios + LLM-designed templates for novel cases
- **General agent compatible**: Works across diverse domains without domain-specific keyword logic

## Specification

### Core Components

#### 1. Scenario Classification (pre-LLM resolution, zero LLM calls)

**Purpose**: Determine appropriate synthesis scenario from goal + intent + execution pattern
**before** the single synthesis LLM call — without making an LLM call of its own.

**Resolution order** (first non-`None` wins; all paths produce zero LLM calls):

1. **Heuristic fast-path** (`scenario_classifier._heuristic_classify`): pure
   pattern matching on execution metadata (step count, success count, tool
   types, evidence volume). Returns a `ScenarioClassification` for obvious
   cases (single-step, all-failed, high-step analysis, low-evidence agentic)
   or `None` when no rule matches.

2. **TypeSafe (JEV) `Choice`** (`scenario_typesafe.classify_scenario_typesafe`):
   when the heuristic is inconclusive, a TypeSafe `Choice` question picks one
   of the 10 built-in scenario styles. JEV is a decision model, not a
   text-generation LLM — this is **not** an LLM round trip. A trusted verdict
   (confidence + margin gates met, suppression bar satisfied) returns a
   `ScenarioClassification`; an untrusted or unavailable verdict returns
   `None`.

3. **Scratchpad fallback**: when both heuristic and JEV return `None`, the
   synthesis LLM self-classifies inside `<analysis>` tags in the **same
   streaming response** that produces the report body (see §2 below). This is
   not a separate call — classification and synthesis are collapsed into one
   LLM request.

**Output Schema**:
```python
class ScenarioClassification(BaseModel):
    scenario: str                 # Built-in scenario name or "custom"
    sections: list[str]           # Section names for synthesis structure
    contextual_focus: list[str]   # 2-3 specific focus areas for this goal
    evidence_emphasis: str        # How to use execution evidence
```

**Built-in Scenarios** (minimal templates):
1. `code_architecture_design` - System/module structure analysis
2. `code_implementation_design` - Concrete implementation patterns and examples
3. `research_synthesis` - Multi-source information gathering and findings
4. `travel_activity_plan` - Structured planning for trips, events, activities
5. `tutorial_guide` - Step-by-step instructional content
6. `analysis_report` - Data/metrics/trends analysis with recommendations
7. `investigation_summary` - Problem/troubleshooting investigation process
8. `decision_analysis` - Options comparison with trade-offs
9. `content_draft` - Blog, documentation, proposal, email drafts
10. `general_summary` - Simple summarization fallback

#### 2. SynthesisGenerator (single LLM call)

**Unified Flow** (one LLM call, not two):
1. Resolve classification via the pre-LLM cascade above (zero LLM calls)
2. Build synthesis prompt from classification (no length constraints)
3. Stream the report via a **single** `astream` call:
   - **Non-scratchpad mode** (heuristic or JEV resolved the scenario): system
     prompt has the scenario pre-selected; the LLM writes the report directly.
   - **Scratchpad mode** (fallback): the system prompt instructs the model to
     self-classify inside `<analysis>` tags **and then** write the report body
     in the **same streaming response**. The `<analysis>` block is internal
     reasoning within the same response — classification and synthesis are
     collapsed into a single LLM request. After streaming,
     `strip_analysis_scratchpad` removes the analysis block from the final
     output (a deterministic string operation, not an LLM call).

> **Design note**: The former two-phase classify→generate design (separate
> classifier LLM call, then separate synthesis LLM call) has been collapsed
> into a single LLM call. The scratchpad mode lets the model self-classify
> inline; the JEV path pre-selects the scenario so the model doesn't even
> need to self-classify. Neither path adds a second LLM call.

**Prompt Structure**:
```
Generate a {scenario} synthesis for the goal: {goal}

SCENARIO STRUCTURE:
Sections: {sections}

CONTEXTUAL FOCUS:
{contextual_focus (bullet list)}

EVIDENCE EMPHASIS:
{evidence_emphasis}

EXECUTION EVIDENCE:
{evidence}

INSTRUCTIONS:
1. Follow the scenario structure - address each section purposefully
2. Focus on the contextual areas identified above
3. Judge appropriate depth and detail level based on the goal and evidence
4. Extract and present actual content from tool results
5. Be concrete and actionable
6. Use the full execution history available in the conversation context
```

### Integration Architecture

```
Goal Completion Flow:
Execute Phase → generates evidence
    ↓
Plan Phase → decides synthesis needed
    ↓
Goal Completion Decision → skip/direct/synthesize
    ↓
SynthesisGenerator (single LLM call):
    Pre-LLM classification (zero LLM calls):
        1. Heuristic fast-path (pattern matching)
        2. TypeSafe (JEV) Choice question (decision model, not LLM)
        3. Fallback → scratchpad self-classification (inline in the LLM call)
    
    Single LLM streaming call:
        - Non-scratchpad: scenario pre-selected → direct report stream
        - Scratchpad: model self-classifies in <analysis> then writes
          report body in the same response stream
    ↓
Final Synthesis Response (analysis block stripped deterministically)
```

### Removal of Legacy Components

**Deleted**:
- `policies/response_length_policy.py` (entire file)
- `ResponseLengthCategory` enum
- `determine_response_length()` function
- `calculate_evidence_metrics()` function (moved to classifier input)
- `_infer_goal_type_from_intent()` method
- `categorize_response_length()` method
- `_get_length_guidance()` method
- Keyword constants (`_MIN_DIRECTORIES_FOR_ARCHITECTURE`, etc.)
- `response_length_category` field from `PlanResult`

**Modified**:
- `SynthesisGenerator.generate_synthesis()` - remove `length_category` parameter
- `SynthesisGenerator._build_synthesis_prompt()` - use classification instead of goal_type/length_category
- `strange_loop.py` - remove length categorization call

## Implementation Details

### File Structure

**New**:
- `sloop/engine/completion/scenario_classifier.py` - Heuristic fast-path, ScenarioClassification schema, _SCENARIO_DESCRIPTIONS, SCENARIO_FORMAT_HINTS
- `sloop/engine/completion/scenario_typesafe.py` - TypeSafe (JEV) Choice question for scenario classification (pre-LLM, zero LLM calls)
- `sloop/engine/completion/synthesis_projection.py` - Prompt rendering, scratchpad mode, analysis-block stripping

**Modified**:
- `sloop/engine/completion/synthesis.py` - Unified single-LLM-call synthesis with heuristic → JEV → scratchpad cascade
- `sloop/engine/strange_loop.py` - Remove length categorization step

**Deleted**:
- `policies/response_length_policy.py` - Entire file

### Error Handling

**Classification Failure (heuristic + JEV both return None)**:
```python
# No LLM call was made for classification — fall through to scratchpad mode.
# The synthesis LLM self-classifies inside <analysis> tags in the same
# streaming response that produces the report body (single LLM call).
classification = ScenarioClassification(
    scenario="custom",
    sections=[],
    contextual_focus=[f"Summarize key findings for: {goal[:120]}"],
    evidence_emphasis="Group evidence by concern or outcome in bullets/tables",
)
scratchpad_mode = True  # → single LLM call with inline self-classification
```

**Scratchpad Stream Timeout**:
```python
# The single synthesis LLM stream is wrapped in asyncio.timeout. On timeout,
# fall back to generate_user_fallback_summary (deterministic, no LLM call).
async for chunk in self._yield_fallback_chunk(state):
    yield chunk
```

**No Intent Available**:
```python
intent_type = "new_goal"
task_complexity = "medium"
if state.intent and hasattr(state.intent, "intent_type"):
    intent_type = state.intent.intent_type
    task_complexity = getattr(state.intent, "task_complexity", "medium")
```

**Empty Evidence**:
```python
evidence = self._extract_evidence(state)
if not evidence:
    evidence = "No execution evidence available (goal completed without tools)"
```

## Testing Requirements

### Unit Tests

**ScenarioClassifier**:
- Mock execution summaries for each built-in scenario type
- Built-in scenario matching (architecture goal + file_read → code_architecture_design)
- Custom scenario generation (novel goal → custom sections)
- Contextual_focus quality (specific to goal, not generic)
- Evidence_emphasis generation

**SynthesisGenerator**:
- Prompt construction with classification
- Evidence extraction (unchanged behavior)
- Fallback handling (classifier failure → general_summary)

### Integration Tests

**End-to-end Synthesis**:
- Architecture goals → code_architecture_design scenario + proper sections
- Research goals → research_synthesis scenario + source analysis
- Travel planning → travel_activity_plan scenario + itinerary
- Novel goals → custom scenario generation with appropriate sections
- Empty evidence handling
- Depth self-judgment quality (varying evidence volume)

### Removed Tests

Delete all tests for:
- `ResponseLengthCategory` enum
- `determine_response_length()` function
- `calculate_evidence_metrics()` function
- `categorize_response_length()` method

## Migration Path

Cut-over is sequenced: (1) Preparation — create `analysis/scenario_classifier.py` with classifier logic, `BUILTIN_SCENARIOS` constant, `ScenarioClassification` schema, unit tests; (2) Integration — modify `SynthesisGenerator` to use classifier, remove length categorization logic, update synthesis prompt template, update `strange_loop.py` to drop `length_category`; (3) Cleanup — delete `policies/response_length_policy.py`, remove `response_length_category` from `PlanResult`, remove related tests, update imports, run verification suite; (4) Validation — integration tests with diverse goals, scenario matching accuracy, synthesis quality, comparison vs current system.

## Success Criteria

1. **No keyword rules**: Classification uses intent + execution pattern, not regex matching
2. **Scenario-appropriate outputs**: Architecture goals → architecture design structure, research → research synthesis structure
3. **Adaptive depth**: LLM self-judges detail level based on goal + evidence
4. **Novel scenario handling**: Goals outside built-in library get custom-designed templates
5. **Quality improvement**: Synthesis outputs more structured and actionable than current generic prompts
6. **General agent compatibility**: Works across domains without domain-specific logic

## Future Extensions

1. **Plugin-defined scenarios**: Allow plugins to register custom scenario templates
2. **User preferences**: Learn preferred synthesis styles per user/goal type
3. **Multi-scenario synthesis**: Complex goals with multiple deliverable components
4. **Section-level templates**: Richer guidance per section (beyond names)
5. **Quality metrics**: Measure synthesis quality (structure adherence, evidence usage, actionability)

---

## Amendments

### Amendment A: Prompt Structure Improvements (IG-524)

**Date**: 2026-06-28
**Issue**: Misleading execution summary, static content in dynamic message, non-standard markers

**Changes**:

1. **Removed EXECUTION SUMMARY from user message**:
   - Previously showed `step_results` data which could be empty while `loop_messages` contained evidence
   - Misleading "Total steps: 0" signals degraded LLM reasoning
   - LLM derives needed summary from EVIDENCE section content

2. **Moved AVAILABLE BUILT-IN SCENARIOS to system prompt**:
   - Scenario list (~300 tokens) was identical for every request
   - Placed in user message → no caching, paid per synthesis call
   - Now in `synthesis_report_system.xml` → cached once per session

3. **Standardized transcript markers**:
   - Changed `[Task]` → `USER:` and `[Finding]` → `AI:`
   - Standard conversation markers universally recognized by LLMs
   - Improves comprehension without changing flattening approach

**Updated Prompt Structure** (after IG-524):

System Prompt (`synthesis_report_system.xml`):
```xml
<SYNTHESIS_REPORT>
Available report formats:
1. code_architecture_design - System/module structure analysis
...
10. general_summary - Simple summarization fallback

Report style: {{ scenario }}
Required sections: {{ sections }}
Focus areas: {{ contextual_focus }}
How to use evidence: {{ evidence_emphasis }}
Rules: ...
</SYNTHESIS_REPORT>
```

User Message (`user_message.py:build_synthesis_message()`):
```
GOAL:
{user_query}

INTENT:
{intent_type} (complexity: {task_complexity})

CONTEXTUAL FOCUS:
- {item 1}
- {item 2}

EVIDENCE EMPHASIS:
{evidence_emphasis}

EVIDENCE:
STEP SUMMARIES:
  [Step S1] {summary}
  ...

WORK TRANSCRIPT:
  USER: {human content}
  AI: {ai content}
  USER: {human content}
  AI: {ai content}

TASK:
1. Write a final report for the person who submitted the request
2. Use only the execution evidence provided — do not invent results
3. Organize by theme, not chronologically
4. Include the required sections for the matched scenario
```

**Files Modified**:
- `user_message.py` — Removed EXECUTION SUMMARY + AVAILABLE BUILT-IN SCENARIOS sections
- `synthesis_report_system.xml` — Added scenario list to system prompt
- `synthesis_projection.py` — Changed `[Task]`→`USER:` and `[Finding]`→`AI:`

### Amendment B: Unified Single-LLM-Call Synthesis + JEV Pre-Classification (IG-SLP-002)

**Date**: 2026-10-12
**Issue**: The original spec described a two-phase classify→generate flow (separate
classifier LLM call, then separate synthesis LLM call). The implementation
collapsed this into a single LLM call, and added a TypeSafe (JEV) pre-classification
path that replaces what would otherwise be the classifier LLM call with a cheaper
decision-model question.

**Changes**:

1. **Collapsed two-phase classify→generate into a single LLM call**:
   - The scratchpad mode lets the model self-classify inside `<analysis>` tags
     and then write the report body in the **same streaming response**.
   - `strip_analysis_scratchpad` removes the analysis block deterministically
     (string operation, not an LLM call).
   - Docstring in `synthesis.py:generate_synthesis` confirms: "collapsing the
     former two-phase classify→generate into a single LLM call."

2. **Added TypeSafe (JEV) pre-classification path** (`scenario_typesafe.py`):
   - When the heuristic fast-path is inconclusive, a JEV `Choice` question picks
     one of the 10 built-in scenario styles before falling back to scratchpad.
   - JEV is a decision model (TypeSafe `Choice`), not a text-generation LLM —
     this is **not** an LLM round trip.
   - A trusted verdict (confidence + margin gates met, suppression bar satisfied)
     returns a `ScenarioClassification` with `scratchpad_mode=False` so the
     synthesis LLM doesn't even need to self-classify.
   - An untrusted or unavailable verdict returns `None` → scratchpad fallback.

3. **Pre-LLM classification cascade** (zero LLM calls):
   - Heuristic fast-path → JEV `Choice` → scratchpad fallback (inline in the
     single LLM call). Only the synthesis `astream` call is an LLM call.

**Files Added/Modified**:
- `sloop/engine/completion/scenario_typesafe.py` — JEV `Choice` for scenario classification
- `sloop/engine/completion/synthesis.py` — `_resolve_classification` now async, tries heuristic → JEV → scratchpad
- `sloop/engine/completion/synthesis_projection.py` — `render_synthesis_system_prompt` with `scratchpad_mode=True`