"""Tool-approval evaluator shared by the `AutoModeMiddleware` inline gate.

RFC-634: deterministic evaluation for a single tool call — deny rules →
loop allowlist (exact signature) → nano operation security (with rule-family
allowlist override). The middleware maps the result onto its decision table
(auto/manual mode, bypass, human attachment) and owns all interrupt /
inline-reject / allow behavior.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from soothe.sloop.clarification.tool_rule_matcher import (
    match_command_rule,
    match_path_rule,
)

if TYPE_CHECKING:
    from soothe.config.models import ToolApprovalConfig

logger = logging.getLogger(__name__)

ApprovalDecision = Literal["approve", "reject", "escalate"]
PipelineStage = Literal["deny_rule", "allowlist", "safety_check", "default_approve"]

# Tool args fields that identify the action operand for allowlist signatures.
_COMMAND_TOOLS = frozenset({"run_command"})
_PATH_TOOLS = frozenset({"edit_file", "write_file", "delete"})


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


def rule_approved(
    rule_id: str | None,
    allowlist: list[Mapping[str, Any]],
) -> bool:
    """True when the human already approved this rule's family this loop."""
    if not rule_id:
        return False
    from soothe_nano.security.operation_guard import rule_family

    approved = rule_family(rule_id)
    for rec in allowlist:
        if isinstance(rec, Mapping) and str(rec.get("rule") or "") in approved:
            return True
    return False


@dataclass(frozen=True)
class ApprovalResult:
    """Evaluation outcome for one tool action.

    `escalate` (safety-check hit without a prior human override) is the only
    verdict that may need a human decision — the middleware decides whether
    one is available and routes to an interrupt or an inline reject.
    """

    decision: ApprovalDecision
    stage: PipelineStage
    reason: str = ""
    rule_id: str | None = None


class ToolApprovalPipeline:
    """Deny-list-first single-action evaluator.

    Stage order (first deciding stage wins): deny rules → loop allowlist
    (exact signature) → safety checks (rule-family allowlist override
    honored). Deny rules are absolute; no allowlist entry overrides them.
    """

    def __init__(
        self,
        config: ToolApprovalConfig,
        *,
        security_config: Any = None,
    ) -> None:
        """Initialize deny rules and security config."""
        self._deny_rules = config.deny_rules
        self._security_config = security_config
        self._security_evaluator: Any = None  # lazy-init in _check_safety

    def evaluate_action(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        workspace_root: str | None = None,
        allowlist: list[Mapping[str, Any]] | None = None,
        bypass: bool = False,
    ) -> ApprovalResult:
        """Evaluate one tool call. Raises on evaluator failure (caller fail-safes).

        Args:
            tool_name: Tool being called.
            args: Tool-call arguments.
            workspace_root: Per-request workspace root (`<workspace>` token).
            allowlist: Loop-scoped `{"tool", "signature"}` / `{"rule"}` records
                from prior human approvals.
            bypass: Skip the safety stage (deny rules still run).
        """
        allowlist = allowlist or []

        # Stage 1: deny rules — absolute, never overridden.
        if self._matches_any_rule(tool_name, args, self._deny_rules, workspace_root):
            return ApprovalResult(
                "reject",
                "deny_rule",
                f"matched deny rule for {tool_name}",
            )

        # Stage 2: loop allowlist — prior human approval of this exact action.
        if allowlist and self._matches_allowlist(tool_name, args, allowlist):
            return ApprovalResult(
                "approve",
                "allowlist",
                "matched loop-scoped approval",
            )

        # Stage 3: safety checks (delegated to nano), with rule-family override.
        if not bypass:
            safety_result = self._check_safety(tool_name, args, workspace_root)
            if safety_result is not None:
                reason, rule_id = safety_result
                if rule_approved(rule_id, allowlist):
                    logger.info(
                        "[tool_approval] safety rule=%s overridden by prior human approval",
                        rule_id,
                    )
                    return ApprovalResult(
                        "approve",
                        "allowlist",
                        f"rule {rule_id} overridden by prior human approval",
                        rule_id=rule_id,
                    )
                return ApprovalResult(
                    "escalate",
                    "safety_check",
                    reason,
                    rule_id=rule_id,
                )

        return ApprovalResult(
            "approve",
            "default_approve",
            "no deny rule or safety check matched",
        )

    def _matches_allowlist(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        allowlist: list[Mapping[str, Any]],
    ) -> bool:
        """True when `(tool_name, signature)` is in the loop allowlist."""
        sig = signature_for(tool_name, args)
        if sig is None:
            return False
        for rec in allowlist:
            if not isinstance(rec, Mapping):
                continue
            if str(rec.get("tool") or "") == tool_name and str(rec.get("signature") or "") == sig:
                return True
        return False

    def _check_safety(
        self,
        name: str,
        args: Mapping[str, Any],
        workspace_root: str | None,
    ) -> tuple[str, str | None] | None:
        """Run safety checks via nano's OperationSecurity.  Returns `(reason, rule_id)` if denied."""
        from soothe_nano.security.operation_guard import (
            WorkspaceToolOperationSecurity,
            build_operation_security_request,
        )
        from soothe_sdk.protocols.operation_security import (
            OperationSecurityContext,
        )

        if self._security_evaluator is None:
            self._security_evaluator = WorkspaceToolOperationSecurity()
        request = build_operation_security_request(name, dict(args))
        ctx = OperationSecurityContext(
            workspace=workspace_root,
            security_config=self._security_config,
        )
        decision = self._security_evaluator.evaluate(request, ctx)
        if decision.verdict == "deny":
            return decision.reason, decision.rule_id
        return None

    def _matches_any_rule(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        rules: list,
        workspace_root: str | None,
    ) -> bool:
        """Check if a tool action matches any rule in the list."""
        if tool_name == "run_command":
            val = str(args.get("command") or "")
            for rule in rules:
                if rule.tool != tool_name:
                    continue
                if match_command_rule(val, rule.pattern):
                    return True
            return False

        if tool_name in ("edit_file", "write_file", "delete"):
            val = str(args.get("file_path") or args.get("path") or "")
            for rule in rules:
                if rule.tool != tool_name:
                    continue
                if match_path_rule(val, rule.pattern, workspace_root):
                    return True
            return False

        return False  # unknown tool type — no rule match


__all__ = [
    "ApprovalResult",
    "ToolApprovalPipeline",
    "approval_record",
    "rule_approved",
    "signature_for",
]
