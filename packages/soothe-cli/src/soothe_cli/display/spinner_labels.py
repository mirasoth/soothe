"""Single-word thinking-row spinner labels and plan-phase mapping."""

from __future__ import annotations

# Turn-level thinking row (step cards carry finer detail).
SPINNER_LABEL_THINKING = "Thinking"
SPINNER_LABEL_INTERPRETING = "Interpreting"
SPINNER_LABEL_ASSESSING = "Assessing"
SPINNER_LABEL_PLANNING = "Planning"
SPINNER_LABEL_FINALIZING = "Finalizing"
SPINNER_LABEL_EXECUTING = "Executing"
SPINNER_LABEL_TOOLS = "Tools"
SPINNER_LABEL_OFFLOADING = "Offloading"
SPINNER_LABEL_SYNTHESIZING = "Synthesizing"
SPINNER_LABEL_WRITING = "Writing"
SPINNER_LABEL_RETRYING = "Retrying"
SPINNER_LABEL_INPUT = "Input"
SPINNER_LABEL_WAITING = "Waiting"
SPINNER_LABEL_CONNECTING = "Connecting"
SPINNER_LABEL_DELEGATING = "Delegating"
SPINNER_LABEL_SUBMITTING = "Submitting"

# Backend plan_phase_status labels (orchestrator nodes) → display word.
_PLAN_PHASE_TO_SPINNER: dict[str, str] = {
    "Interpreting goal": SPINNER_LABEL_INTERPRETING,
    "Analyzing coverage": SPINNER_LABEL_ASSESSING,
    "Assessing progress": SPINNER_LABEL_ASSESSING,
    "Assessing continuation": SPINNER_LABEL_ASSESSING,
    "Generating plan": SPINNER_LABEL_PLANNING,
    "Finalizing goal": SPINNER_LABEL_FINALIZING,
    "Synthesizing plan": SPINNER_LABEL_SYNTHESIZING,
    "Plan ready": SPINNER_LABEL_PLANNING,
    "Refining plan": SPINNER_LABEL_SYNTHESIZING,
    "Refined plan ready": SPINNER_LABEL_PLANNING,
    "Refinement failed": SPINNER_LABEL_RETRYING,
    # Canonical words (if backend adopts them later).
    SPINNER_LABEL_INTERPRETING: SPINNER_LABEL_INTERPRETING,
    SPINNER_LABEL_ASSESSING: SPINNER_LABEL_ASSESSING,
    SPINNER_LABEL_PLANNING: SPINNER_LABEL_PLANNING,
    SPINNER_LABEL_FINALIZING: SPINNER_LABEL_FINALIZING,
    SPINNER_LABEL_SYNTHESIZING: SPINNER_LABEL_SYNTHESIZING,
}


def map_plan_phase_spinner_label(label: str) -> str:
    """Map a backend plan-phase label to a single-word thinking-row label."""
    key = label.strip()
    if not key:
        return SPINNER_LABEL_THINKING
    mapped = _PLAN_PHASE_TO_SPINNER.get(key)
    if mapped:
        return mapped
    # Intake-only wire: «Delegating to {subagent}» (RFC-630).
    if key.lower().startswith("delegating"):
        return SPINNER_LABEL_DELEGATING
    return SPINNER_LABEL_THINKING


# ---------------------------------------------------------------------------
# Interaction-mode-aware labels (plan / ask turns)
#
# A read-only plan/ask turn must not look like a mutating agent turn:
# agent-flavored execution words (Executing / Tools / Writing) are replaced
# with research-flavored words carrying a "(read-only)" suffix. Turn-lifecycle
# words (Submitting / Finalizing / Retrying / Waiting / Connecting /
# Offloading / Synthesizing / Delegating) pass through unchanged — they
# describe the turn machinery, not the tool surface.
# ---------------------------------------------------------------------------

_MODE_SPINNER_WORD: dict[str, str] = {
    "plan": "Planning",
    "ask": "Researching",
}

_MODE_SPINNER_OVERRIDDEN: frozenset[str] = frozenset(
    {
        SPINNER_LABEL_THINKING,
        SPINNER_LABEL_INTERPRETING,
        SPINNER_LABEL_ASSESSING,
        SPINNER_LABEL_PLANNING,
        SPINNER_LABEL_EXECUTING,
        SPINNER_LABEL_TOOLS,
        SPINNER_LABEL_WRITING,
        SPINNER_LABEL_INPUT,
    }
)


def mode_aware_spinner_label(label: str | None, interaction_mode: str | None) -> str | None:
    """Adapt a spinner label to the turn's interaction mode.

    On plan/ask turns, execution-flavored labels become "Planning (read-only)"
    / "Researching (read-only)"; anything else (including `None` and unknown
    modes) passes through unchanged.
    """
    word = _MODE_SPINNER_WORD.get(interaction_mode or "")
    if word is None or label not in _MODE_SPINNER_OVERRIDDEN:
        return label
    return f"{word} (read-only)"


def daemon_connect_hint_extra(*, attempt: int, max_attempts: int) -> str | None:
    """Hint suffix for daemon connect retries, e.g. `attempt 2/3`."""
    if max_attempts <= 1 or attempt <= 1:
        return None
    return f"attempt {attempt}/{max_attempts}"
