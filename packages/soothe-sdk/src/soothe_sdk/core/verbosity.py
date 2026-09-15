"""Binary verbosity tier for event classification.

Events classify to one of two tiers:
- `NORMAL`: client-visible — flows over the wire to subscribed clients.
- `INTERNAL`: hidden — daemon-side only, never leaves the daemon.

Earlier revisions had a five-level ladder (QUIET / NORMAL / DETAILED / DEBUG /
INTERNAL). That granularity was never exercised — the wire ceiling and the TUI
gate were both hardcoded to "normal", so the only effective distinction was
visible-vs-hidden. The tiers collapsed accordingly.
"""

from __future__ import annotations

from enum import IntEnum


class VerbosityTier(IntEnum):
    """Visibility class for catalog events.

    `NORMAL` events are sent to clients; `INTERNAL` events are not.

    Examples:
        >>> should_show(VerbosityTier.NORMAL)
        True
        >>> should_show(VerbosityTier.INTERNAL)
        False
    """

    NORMAL = 1
    INTERNAL = 99


def should_show(tier: VerbosityTier) -> bool:
    """Return True if tier is client-visible.

    Args:
        tier: Visibility class of the content.

    Returns:
        True iff `tier` is `NORMAL`.
    """
    return tier == VerbosityTier.NORMAL


__all__ = [
    "VerbosityTier",
    "should_show",
]
