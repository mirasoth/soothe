"""TUI composer modes — working-mode hierarchy.

Agent sub-modes (auto, bypass, manual) grouped first, then plan and ask.
Shift+Tab cycle: Auto → Bypass → Manual → Plan → Ask → Auto.

Manual is the human-confirmed agent mode: gated tool actions that would be
resolved by the veritas leg (rule-unresolved calls carrying risk hints) and
inline `ask_user` veritas fast paths are skipped — those clarifications go to
the human relay instead (dock cards). Deny rules, safety checks, allowlist
grants, and plan review are mode-independent and unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

COMPOSER_MODE_AUTO = "auto"
COMPOSER_MODE_BYPASS = "bypass"
COMPOSER_MODE_MANUAL = "manual"
COMPOSER_MODE_PLAN = "plan"
COMPOSER_MODE_ASK = "ask"

# Agent sub-modes grouped first, then standalone working modes.
COMPOSER_MODE_ORDER: tuple[str, ...] = (
    COMPOSER_MODE_AUTO,
    COMPOSER_MODE_BYPASS,
    COMPOSER_MODE_MANUAL,
    COMPOSER_MODE_PLAN,
    COMPOSER_MODE_ASK,
)
VALID_COMPOSER_MODES: frozenset[str] = frozenset(COMPOSER_MODE_ORDER)

# Agent working mode sub-modes (full mutating tool surface).
AGENT_SUB_MODES: frozenset[str] = frozenset(
    {COMPOSER_MODE_AUTO, COMPOSER_MODE_BYPASS, COMPOSER_MODE_MANUAL}
)
# Standalone working modes (not agent sub-modes).
STANDALONE_WORKING_MODES: frozenset[str] = frozenset({COMPOSER_MODE_PLAN, COMPOSER_MODE_ASK})


@dataclass(frozen=True)
class ComposerWireFields:
    """Wire fields derived from a composer mode."""

    clarification_mode: str
    preferred_subagent: str | None
    interaction_mode: str | None


def normalize_composer_mode(mode: str | None) -> str:
    """Clamp an arbitrary value to a valid composer mode (default `auto`)."""
    if mode in VALID_COMPOSER_MODES:
        return mode
    return COMPOSER_MODE_AUTO


def next_composer_mode(current: str) -> str:
    """Advance to the next mode in the cycle. Unknown values reset to `auto`."""
    if current not in VALID_COMPOSER_MODES:
        return COMPOSER_MODE_AUTO
    idx = COMPOSER_MODE_ORDER.index(current)
    return COMPOSER_MODE_ORDER[(idx + 1) % len(COMPOSER_MODE_ORDER)]


def parse_mode_prefix_from_input(user_input: str) -> tuple[str | None, str]:
    """Detect a leading `/plan` or `/ask` slash token in user input.

    One-shot mode override: a leading mode token wins over the sticky
    composer mode for the current turn only (the badge is unchanged).
    Returns `(interaction_mode, cleaned_text)` where `interaction_mode` is
    `"plan"`/`"ask"` and `cleaned_text` has the token stripped; `(None,
    user_input)` unchanged when no mode token is present. The token must
    be at the start of the input and followed by whitespace or end of
    string, so `/plans` or `see /plan` do not match. Leading whitespace is
    tolerated before the token.
    """
    candidate = user_input.lstrip()
    lowered = candidate.lower()
    for token, mode in (
        ("/plan", COMPOSER_MODE_PLAN),
        ("/ask", COMPOSER_MODE_ASK),
    ):
        if lowered.startswith(token) and (
            len(candidate) == len(token) or candidate[len(token)].isspace()
        ):
            return (mode, candidate[len(token) :].strip())
    return (None, user_input)


def resolve_composer_wire_fields(mode: str) -> ComposerWireFields:
    """Map composer mode to wire fields.

    auto → clarification=auto, interaction=None; bypass → clarification=auto,
    interaction=bypass; manual → clarification=manual (the human-confirmed
    agent mode: veritas fast paths yield to the human relay); plan →
    interaction=plan; ask → interaction=ask. Slash routing in the message
    wins over the sticky hint.
    """
    normalized = normalize_composer_mode(mode)
    if normalized == COMPOSER_MODE_PLAN:
        return ComposerWireFields(COMPOSER_MODE_AUTO, None, "plan")
    if normalized == COMPOSER_MODE_ASK:
        return ComposerWireFields(COMPOSER_MODE_AUTO, None, COMPOSER_MODE_ASK)
    if normalized == COMPOSER_MODE_BYPASS:
        return ComposerWireFields(COMPOSER_MODE_AUTO, None, COMPOSER_MODE_BYPASS)
    if normalized == COMPOSER_MODE_MANUAL:
        return ComposerWireFields(COMPOSER_MODE_MANUAL, None, None)
    return ComposerWireFields(COMPOSER_MODE_AUTO, None, None)
