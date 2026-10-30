"""Tool-approval evaluator shared by the `AutoModeMiddleware` inline gate.

RFC-634: deterministic evaluation for a single tool call — deny rules → loop
allowlist (exact signature) → nano operation security (with rule-family
allowlist override). The middleware maps the result onto its decision table
(auto/manual mode, bypass, human attachment) and owns all interrupt /
inline-reject / allow behavior.

Sub-critical security matches no longer escalate — they come back as
`risk_hints` on the `default_approve` result so the middleware can route
them to the veritas decision fast path instead of a human. The built-in
deny rules live in nano's severity-tiered registry; only user-configured
`deny_rules` are evaluated here (always absolute).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from soothe.sloop.clarification.dynamic_rules import (
    approval_record,
    command_prefix,
    grant_hit,
    rule_approved,
    signature_for,
)
from soothe.sloop.clarification.tool_rule_matcher import (
    match_command_rule,
    match_path_rule,
)

if TYPE_CHECKING:
    from soothe.config.models import ToolApprovalConfig

logger = logging.getLogger(__name__)

ApprovalDecision = Literal["approve", "reject", "escalate"]
PipelineStage = Literal["deny_rule", "allowlist", "safety_check", "default_approve"]


@dataclass(frozen=True)
class ApprovalResult:
    """Evaluation outcome for one tool action.

    `escalate` (a hard-deny safety hit without a prior human override) is
    the only verdict that may need a human decision — the middleware
    decides whether one is available and routes to an interrupt or an
    inline reject. Sub-critical security matches surface as `risk_hints`
    on the `default_approve` result: judgment input for the veritas leg,
    never a human interrupt on their own.
    """

    decision: ApprovalDecision
    stage: PipelineStage
    reason: str = ""
    rule_id: str | None = None
    risk_hints: tuple[str, ...] = field(default_factory=tuple)


class ToolApprovalPipeline:
    """Deny-list-first single-action evaluator.

    Stage order (first deciding stage wins): user deny rules → loop
    allowlist (signature / prefix / rule family) → safety checks
    (rule-family allowlist override honored; sub-critical matches become
    risk hints). User deny rules are absolute; no allowlist entry overrides
    them.
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
            allowlist: Loop-scoped records — human approvals (`{"tool",
                "signature"}` / `{"rule"}`) and gate grants (`{"tool",
                "prefix"}`) from the dynamic-rule layer.
            bypass: Skip the safety stage (deny rules still run).
        """
        allowlist = allowlist or []

        # Stage 1: user deny rules — absolute, never overridden.
        if self._matches_any_rule(tool_name, args, self._deny_rules, workspace_root):
            return ApprovalResult(
                "reject",
                "deny_rule",
                f"matched deny rule for {tool_name}",
            )

        # Stage 2: loop allowlist — prior human approval or grant for this action.
        if allowlist and grant_hit(tool_name, args, allowlist):
            return ApprovalResult(
                "approve",
                "allowlist",
                "matched loop-scoped approval",
            )

        # Stage 3: safety checks (delegated to nano), with rule-family override.
        risk_hints: tuple[str, ...] = ()
        if not bypass:
            safety_result = self._check_safety(tool_name, args, workspace_root)
            if safety_result is not None:
                reason, rule_id, verdict = safety_result
                if verdict == "risk_hint":
                    if rule_approved(rule_id, allowlist):
                        # A prior human approval of this rule's family
                        # suppresses re-judgment (e.g. the family's critical
                        # rule was approved, covering its high siblings).
                        logger.info(
                            "[tool_approval] risk hint rule=%s overridden by prior human approval",
                            rule_id,
                        )
                        return ApprovalResult(
                            "approve",
                            "allowlist",
                            f"rule {rule_id} overridden by prior human approval",
                            rule_id=rule_id,
                        )
                    # RFC-LOOP-030: sub-critical match — judgment input, not a
                    # human interrupt.
                    risk_hints = (rule_id,) if rule_id else ()
                elif rule_approved(rule_id, allowlist):
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
                else:
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
            risk_hints=risk_hints,
        )

    def _check_safety(
        self,
        name: str,
        args: Mapping[str, Any],
        workspace_root: str | None,
    ) -> tuple[str, str | None, str] | None:
        """Run safety checks via nano's OperationSecurity.

        Returns `(reason, rule_id, verdict)` for `deny` and `risk_hint`
        verdicts, `None` otherwise.
        """
        from soothe_nano.protocols.operation_security import (
            OperationSecurityContext,
        )
        from soothe_nano.security.operation_guard import (
            WorkspaceToolOperationSecurity,
            build_operation_security_request,
        )

        if self._security_evaluator is None:
            self._security_evaluator = WorkspaceToolOperationSecurity()
        request = build_operation_security_request(name, dict(args))
        ctx = OperationSecurityContext(
            workspace=workspace_root,
            security_config=self._security_config,
        )
        decision = self._security_evaluator.evaluate(request, ctx)
        if decision.verdict in ("deny", "risk_hint"):
            return decision.reason, decision.rule_id, decision.verdict
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
    "command_prefix",
    "grant_hit",
    "rule_approved",
    "signature_for",
]
