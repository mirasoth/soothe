"""Typed errors for ContextEngine persistence stores."""

from __future__ import annotations


class LedgerCorruptionError(RuntimeError):
    """A persisted CE ledger row exists but cannot be deserialized.

    Raised by `load_ledger` when the stored JSON is corrupt — a
    distinguishable, sticky condition (the row stays corrupt until
    overwritten) that must not be silently downgraded to "empty ledger":
    treating corruption as absence drops all partial-work evidence and
    suppresses downstream markers (`goal_interrupted`).
    """


__all__ = ["LedgerCorruptionError"]
