"""RFC-634: inline tool-approval gate for the four mutating tools.

`AutoModeMiddleware` is the sole tool-approval human-in-the-loop: it
evaluates every gated tool call in its ``after_model`` hook (host middleware
runs before the deepagents HITL hook would, and the builder no longer wires
``interrupt_on`` for these tools). Deterministic verdicts resolve inline —
deny-rule and autopilot-safety rejects become instructive error
``ToolMessage``s with no interrupt and no graph round trip; auto-mode
approvals execute silently. Only genuinely human decisions emit the standard
``{"action_requests": [...]}`` interrupt the executor, relay, and TUI
already understand.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.types import interrupt

from soothe.events.catalog import TOOL_AUTO_GATE_REJECTED
from soothe.sloop.clarification.tool_approval_pipeline import (
    ApprovalResult,
    ToolApprovalPipeline,
    signature_for,
)
from soothe.sloop.middleware.auto_mode_context import AutoModeContext
from soothe.utils.text import truncate_text

logger = logging.getLogger(__name__)

_DESCRIPTION_PREFIX = "Tool execution requires approval"
_SIGNATURE_PREVIEW_CHARS = 120

# Verdicts for a single gated tool call.
VerdictAction = Literal["allow", "reject", "escalate"]


class _Verdict:
    """One tool call's gate verdict."""

    __slots__ = ("action", "result")

    def __init__(self, action: VerdictAction, result: ApprovalResult | None = None) -> None:
        self.action = action
        self.result = result


class AutoModeMiddleware(AgentMiddleware):
    """Inline tool-approval gate (RFC-634). See module docstring."""

    def __init__(
        self,
        pipeline: ToolApprovalPipeline,
        *,
        tools: tuple[str, ...] | list[str] = ("edit_file", "write_file", "delete", "run_command"),
        active_in_bypass: bool = True,
        default_clarification_mode: str = "auto",
        manual_scope: str = "all",
        force_manual_tool_approval: bool = False,
    ) -> None:
        """Wire the evaluator and gate posture.

        Args:
            pipeline: Shared ``ToolApprovalPipeline`` (deny rules + safety).
            tools: Gated tool names; all other tools pass through untouched.
            active_in_bypass: Keep deny rules absolute in bypass mode.
            default_clarification_mode: Fallback when the per-run
                clarification mode is absent from ``configurable``.
            manual_scope: ``manual_scope`` from ``ToolApprovalConfig`` —
                ``all`` routes every unresolved gated call to the human,
                ``ambiguous_only`` auto-approves rule-unresolved calls.
            force_manual_tool_approval: ``tool_approval`` listed in
                ``force_manual_origins`` — every gated call goes to the human.
        """
        super().__init__()
        self._pipeline = pipeline
        self._tools = frozenset(tools)
        self._active_in_bypass = active_in_bypass
        self._default_mode = default_clarification_mode
        self._manual_scope = manual_scope
        self._force_manual_tool_approval = force_manual_tool_approval

    # ------------------------------------------------------------------
    # after_model hook
    # ------------------------------------------------------------------

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Gate the last AIMessage's tool calls before they execute."""
        try:
            messages = state["messages"] if state is not None else None
        except (KeyError, TypeError):
            return None
        if not messages:
            return None
        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        gated: list[tuple[int, ToolCall]] = [
            (idx, tc)
            for idx, tc in enumerate(last_ai_msg.tool_calls)
            if tc.get("name") in self._tools
        ]
        if not gated:
            return None

        ctx = AutoModeContext.from_runtime(runtime, default_mode=self._default_mode)
        verdicts: list[tuple[int, ToolCall, _Verdict]] = []
        for idx, tc in gated:
            verdicts.append((idx, tc, self._evaluate(tc, ctx)))

        # Human decisions: interrupt once with the standard HITL payload.
        pending = [(tc, v) for _, tc, v in verdicts if v.action == "escalate"]
        decisions: list[dict[str, Any]] = []
        if pending:
            payload = {
                "action_requests": [self._action_request(tc) for tc, _ in pending],
                "review_configs": [self._review_config(tc) for tc, _ in pending],
                "escalated_rule_id": next(
                    (v.result.rule_id for _, v in pending if v.result and v.result.rule_id),
                    None,
                ),
            }
            resumed = interrupt(payload)
            decisions = self._extract_decisions(resumed, expected=len(pending))

        # Rewrite the tool-call list: strip rejects, apply human decisions.
        revised: list[ToolCall] = []
        artificial: list[ToolMessage] = []
        changed = False
        decision_idx = 0
        verdict_by_idx = {idx: (tc, v) for idx, tc, v in verdicts}
        for idx, tc in enumerate(last_ai_msg.tool_calls):
            entry = verdict_by_idx.get(idx)
            if entry is None:
                revised.append(tc)
                continue
            _, verdict = entry
            if verdict.action == "reject":
                artificial.append(self._reject_message(tc, verdict.result))
                changed = True
                continue
            if verdict.action == "escalate":
                decision = (
                    decisions[decision_idx]
                    if decision_idx < len(decisions)
                    else {"type": "approve"}
                )
                decision_idx += 1
                edited = self._apply_decision(tc, decision, artificial)
                if edited is None:
                    changed = True
                    continue
                if edited is not tc:
                    changed = True
                tc = edited
            revised.append(tc)

        if not changed and not artificial:
            return None
        last_ai_msg.tool_calls = revised
        return {"messages": [last_ai_msg, *artificial]}

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Async delegation to the sync gate."""
        return self.after_model(state, runtime)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, tool_call: ToolCall, ctx: AutoModeContext) -> _Verdict:
        """Resolve one gated tool call per the RFC-634 decision table."""
        name = str(tool_call.get("name") or "")
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        # Bypass with the gate deactivated → no evaluation at all.
        if ctx.bypass and not self._active_in_bypass:
            return _Verdict("allow")

        try:
            result = self._pipeline.evaluate_action(
                name,
                args,
                workspace_root=ctx.workspace,
                allowlist=list(ctx.allowlist),
                bypass=ctx.bypass,
            )
        except Exception:
            # Fail-safe: pass the call through; the FS permission layer and
            # nano's operation guard still run downstream as backstops.
            logger.warning(
                "[auto_mode] evaluator error on tool=%s; passing through (downstream guards apply)",
                name,
                exc_info=True,
            )
            return _Verdict("allow")

        if result.decision == "reject":
            self._notify_rejected(name, result, args)
            return _Verdict("reject", result)

        if result.decision == "escalate":
            if not ctx.human_attached:
                # Autopilot: no human at the other end — instructive reject
                # (parity with the old station degrade-to-reject path).
                self._notify_rejected(name, result, args)
                return _Verdict("reject", result)
            return _Verdict("escalate", result)

        # approve: allowlist override (exact signature or rule family) never
        # re-asks. Only rule-unresolved actions route to the human when the
        # mode demands a decision.
        if (
            result.decision == "approve"
            and result.stage == "default_approve"
            and self._needs_human_for_ambiguous(ctx)
        ):
            return _Verdict("escalate", result)
        return _Verdict("allow", result)

    def _needs_human_for_ambiguous(self, ctx: AutoModeContext) -> bool:
        """Whether a rule-unresolved gated call still needs a human decision."""
        if self._force_manual_tool_approval:
            return ctx.human_attached
        if ctx.bypass:
            return False
        if ctx.clarification_mode == "manual":
            return ctx.human_attached and self._manual_scope == "all"
        return False

    # ------------------------------------------------------------------
    # Interrupt payload + decision processing (HITL protocol parity)
    # ------------------------------------------------------------------

    @staticmethod
    def _action_request(tool_call: ToolCall) -> dict[str, Any]:
        name = tool_call["name"]
        args = tool_call.get("args") or {}
        description = f"{_DESCRIPTION_PREFIX}\n\nTool: {name}\nArgs: {args}"
        return {"name": name, "args": args, "description": description}

    @staticmethod
    def _review_config(tool_call: ToolCall) -> dict[str, Any]:
        return {
            "action_name": tool_call["name"],
            "allowed_decisions": ["approve", "reject"],
        }

    @staticmethod
    def _extract_decisions(resumed: Any, *, expected: int) -> list[dict[str, Any]]:
        """Normalize the resume payload into a decisions list.

        Tolerates a mid-flight allowlist change shrinking the re-evaluated
        action list: missing trailing decisions default to ``approve``
        (mirrors the relay's padding rule in ``build_clarification_resume_payload``).
        """
        if isinstance(resumed, dict):
            raw = resumed.get("decisions")
            if isinstance(raw, list):
                decisions = [d for d in raw if isinstance(d, dict)]
                while len(decisions) < expected:
                    decisions.append({"type": "approve"})
                return decisions
        return [{"type": "approve"}] * expected

    @staticmethod
    def _apply_decision(
        tool_call: ToolCall,
        decision: dict[str, Any],
        artificial: list[ToolMessage],
    ) -> ToolCall | None:
        """Apply one human decision. Returns the (possibly edited) tool call,
        or `None` when the call is stripped (reject / respond)."""
        dtype = decision.get("type")
        if dtype == "reject":
            reason = decision.get("message")
            content = (
                f"User rejected the tool call for `{tool_call['name']}` with reason: {reason}"
                if reason
                else (
                    f"User rejected the tool call for `{tool_call['name']}` with id "
                    f"{tool_call['id']}. The tool was not executed. Do not retry this tool "
                    "call unless the user explicitly requests it."
                )
            )
            artificial.append(
                ToolMessage(
                    content=content,
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"],
                    status="error",
                )
            )
            return None
        if dtype == "respond":
            artificial.append(
                ToolMessage(
                    content=str(decision.get("message") or ""),
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"],
                    status="success",
                )
            )
            return None
        if dtype == "edit":
            edited = decision.get("edited_action")
            if isinstance(edited, dict) and edited.get("name"):
                return ToolCall(
                    type="tool_call",
                    name=edited["name"],
                    args=edited.get("args") or {},
                    id=tool_call["id"],
                )
        return tool_call  # approve (and malformed decisions) → keep

    # ------------------------------------------------------------------
    # Inline reject
    # ------------------------------------------------------------------

    @staticmethod
    def _reject_message(tool_call: ToolCall, result: ApprovalResult | None) -> ToolMessage:
        """Build the instructive error ToolMessage for an inline reject."""
        name = tool_call["name"]
        reason = result.reason if result else "tool approval denied"
        rule = f" (rule={result.rule_id})" if result and result.rule_id else ""
        content = (
            f"Tool call `{name}` was blocked by the tool-approval gate "
            f"(stage={result.stage if result else 'deny_rule'}{rule}): {reason}. "
            "The tool was not executed. Choose a different action or adjust the "
            "arguments; do not retry this exact call unless the goal changes."
        )
        return ToolMessage(
            content=content,
            name=name,
            tool_call_id=tool_call["id"],
            status="error",
        )

    def _notify_rejected(
        self,
        tool_name: str,
        result: ApprovalResult,
        args: dict[str, Any],
    ) -> None:
        """Log and stream-emit an inline reject for observability."""
        sig = signature_for(tool_name, args)
        logger.info(
            "[auto_mode] reject tool=%s stage=%s rule_id=%s signature=%s reason=%s",
            tool_name,
            result.stage,
            result.rule_id,
            truncate_text(sig or "", limit=_SIGNATURE_PREVIEW_CHARS),
            result.reason,
        )
        try:
            from langgraph.config import get_stream_writer  # type: ignore[import]

            writer = get_stream_writer()
            if callable(writer):
                writer(
                    {
                        "type": TOOL_AUTO_GATE_REJECTED,
                        "tool": tool_name,
                        "stage": result.stage,
                        "reason": result.reason,
                        "rule_id": result.rule_id,
                        "signature": truncate_text(sig or "", limit=_SIGNATURE_PREVIEW_CHARS),
                    }
                )
        except Exception:  # noqa: BLE001 — observability only, never fatal
            pass


__all__ = ["AutoModeMiddleware"]
