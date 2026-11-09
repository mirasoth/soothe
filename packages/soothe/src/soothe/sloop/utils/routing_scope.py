"""Client-forced routing scope and `RoutingClassification` construction."""

from __future__ import annotations

from soothe_nano.protocols.routing import RoutingClassification, TaskComplexity


def parse_intake_scope(raw: object | None) -> TaskComplexity | None:
    """Parse a wire `intake_scope` value into a forced `TaskComplexity`.

    Args:
        raw: Client value (string or `None`). Empty / whitespace returns `None`.

    Returns:
        Normalized `TaskComplexity`, or `None` when unset.

    Raises:
        ValueError: When `raw` is a non-empty string outside minimal|simple|complex,
            or a non-string non-None value.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("intake_scope must be a string (minimal, simple, or complex)")
    text = raw.strip().lower()
    if not text:
        return None
    try:
        return TaskComplexity(text)
    except ValueError as exc:
        raise ValueError(f"intake_scope must be minimal, simple, or complex; got {raw!r}") from exc


def build_routing_classification(
    task_complexity: TaskComplexity | None,
    preferred_subagent: str | None,
) -> RoutingClassification | None:
    """Build routing classification consumed by StrangeLoop Plan/Execute.

    Wired specialists are requested explicitly via slash/daemon `preferred_subagent`;
    routing never infers one. `task_complexity` comes only from a client-forced
    `intake_scope`; unforced turns stay `None` so the decompose gate decides.
    """
    from soothe.sloop.state.schemas import resolve_wire_subagent

    resolved_wire = resolve_wire_subagent(wire_subagent=preferred_subagent)
    if resolved_wire:
        return RoutingClassification(
            task_complexity=task_complexity or TaskComplexity.COMPLEX,
            preferred_subagent=resolved_wire,
            routing_hint="subagent",
        )
    if task_complexity is None:
        return None
    return RoutingClassification(
        task_complexity=task_complexity,
        preferred_subagent=None,
        routing_hint="forced_scope",
    )


__all__ = ["build_routing_classification", "parse_intake_scope"]
