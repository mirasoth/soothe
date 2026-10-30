"""Loop-scoped dynamic rules: the grant store and record matching.

One record vocabulary covers every approval provenance in the loop:

- `{"tool", "signature"}` — exact-action record (human approvals today,
  veritas approvals for path tools).
- `{"tool", "prefix"}` — command-prefix grant (`run_command` only): one
  approve of `pytest -x …` clears every subsequent `pytest …` call at L1
  with zero model calls.
- `{"rule", ...}` — rule-family override (nano `rule_family()`), honored by
  the safety stage exactly as before.

Records live in two places with the same lifecycle (the loop): the
graph-state allowlist (human approvals, written by the executor relay) and
the middleware-instance grant store (decision-model / LLM approvals, written
by the auto-mode gate). `grant_hit()` treats both identically.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Tool args fields that identify the action operand for allowlist signatures.
_COMMAND_TOOLS = frozenset({"run_command"})
_PATH_TOOLS = frozenset({"edit_file", "write_file", "delete"})

DEFAULT_GRANT_CAP = 256
"""Upper bound on middleware-instance grant records per loop (oldest evicted)."""


def signature_for(tool_name: str, args: Mapping[str, Any]) -> str | None:
    """Stable per-action signature (`command` or `file_path`), or `None`."""
    if tool_name in _COMMAND_TOOLS:
        return str(args.get("command") or "").strip() or None
    if tool_name in _PATH_TOOLS:
        return str(args.get("file_path") or args.get("path") or "").strip() or None
    return None


def approval_record(tool_name: str, args: Mapping[str, Any]) -> dict[str, str] | None:
    """Allowlist record `{"tool", "signature"}`, or `None` if not signable."""
    sig = signature_for(tool_name, args)
    if sig is None:
        return None
    return {"tool": tool_name, "signature": sig}


def command_prefix(command: str) -> str | None:
    """First whitespace token of a command, or `None` when empty."""
    token = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    return token or None


def grant_hit(
    tool_name: str,
    args: Mapping[str, Any],
    records: list[Mapping[str, Any]] | None,
    *,
    rule_id: str | None = None,
) -> bool:
    """True when any record approves this action (signature / prefix / family)."""
    if not records:
        return False

    sig = signature_for(tool_name, args)
    prefix: str | None = None
    if tool_name in _COMMAND_TOOLS:
        prefix = command_prefix(str(args.get("command") or ""))

    approved_rules = _rule_family(rule_id) if rule_id else frozenset()

    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        if approved_rules and str(rec.get("rule") or "") in approved_rules:
            return True
        if sig is not None and str(rec.get("tool") or "") == tool_name:
            if str(rec.get("signature") or "") == sig:
                return True
            if prefix is not None and str(rec.get("prefix") or "") == prefix and prefix:
                return True
    return False


def _rule_family(rule_id: str | None) -> frozenset[str]:
    """All nano rule ids in the same family as ``rule_id`` (inclusive)."""
    if not rule_id:
        return frozenset()
    from soothe_nano.security.operation_guard import rule_family

    return rule_family(rule_id)


def rule_approved(
    rule_id: str | None,
    records: list[Mapping[str, Any]] | None,
) -> bool:
    """True when the human already approved this rule's family this loop."""
    if not rule_id or not records:
        return False
    approved = _rule_family(rule_id)
    for rec in records:
        if isinstance(rec, Mapping) and str(rec.get("rule") or "") in approved:
            return True
    return False


class LoopGrantStore:
    """Middleware-instance grant store — loop-scoped write-back for the
    veritas leg.

    Holds the records the auto-mode gate mints when the decision model or
    the LLM approves an L1-unresolved call. The graph-state allowlist (human
    approvals) is read separately and merged at match time; only this store
    is mutated here.
    """

    def __init__(self, *, cap: int = DEFAULT_GRANT_CAP) -> None:
        self._cap = max(1, int(cap))
        self._records: list[dict[str, Any]] = []

    @property
    def records(self) -> list[Mapping[str, Any]]:
        """Current grant records (do not mutate)."""
        return list(self._records)

    def matches(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        allowlist: list[Mapping[str, Any]] | None,
        *,
        rule_id: str | None = None,
    ) -> bool:
        """Grant or human-approval hit across the instance store and the loop allowlist."""
        if grant_hit(tool_name, args, self._records, rule_id=rule_id):
            return True
        return grant_hit(tool_name, args, allowlist, rule_id=rule_id)

    def record(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        source: str,
        rule_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Mint a grant for an approved action; evicts oldest beyond the cap.

        Only allow-verdicts are recorded — denies stay per-call (an
        instructive reject already teaches the model not to retry).
        """
        rec: dict[str, Any] | None = None
        if rule_id:
            rec = {"rule": rule_id, "source": source}
        elif tool_name in _COMMAND_TOOLS:
            prefix = command_prefix(str(args.get("command") or ""))
            if prefix:
                rec = {"tool": tool_name, "prefix": prefix, "source": source}
        else:
            sig = signature_for(tool_name, args)
            if sig is not None:
                rec = {"tool": tool_name, "signature": sig, "source": source}

        if rec is None:
            return None
        if rec in self._records:
            return rec
        self._records.append(rec)
        while len(self._records) > self._cap:
            self._records.pop(0)
        logger.info(
            "[dynamic_rules] grant tool=%s match=%s source=%s (records=%d)",
            rec.get("tool") or "<rule>",
            rec.get("prefix") or rec.get("signature") or rec.get("rule"),
            source,
            len(self._records),
        )
        return rec


__all__ = [
    "DEFAULT_GRANT_CAP",
    "LoopGrantStore",
    "approval_record",
    "command_prefix",
    "grant_hit",
    "rule_approved",
    "signature_for",
]
