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


# Argument keys worth sending to a classifier (bounded, low-risk of secrets).
_CLASSIFY_ARG_KEYS: tuple[str, ...] = ("command", "file_path", "path", "directory")
_CLASSIFY_PREVIEW_CHARS = 200
_SECRET_ARG_MARKERS = ("token", "password", "secret", "api_key", "authorization")

# Configurable key the executor writes the per-step LoopStateView under.
_LOOP_VIEW_KEY = "soothe_veritas_loop_view"


def _loop_view(ctx: AutoModeContext) -> Any | None:
    """Read the per-step LoopStateView from configurable (classifier context)."""
    from soothe.sloop.clarification.protocol import LoopStateView

    view = ctx.configurable.get(_LOOP_VIEW_KEY)
    return view if isinstance(view, LoopStateView) else None


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
        classifier: Any | None = None,
        classifier_config: Any | None = None,
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
            classifier: Optional ``RiskClassifier`` consulted for rule-unresolved
                (ambiguous) calls. ``None`` keeps the deterministic behaviour.
            classifier_config: nano ``ClassifierConfig`` supplying strict /
                per-turn limits for the classifier path.
        """
        super().__init__()
        self._pipeline = pipeline
        self._classifier = classifier
        self._classifier_cfg = classifier_config
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
        """Async gate: deterministic verdicts, then the optional classifier."""
        result = self.after_model(state, runtime)
        if self._classifier is None or self._classifier_cfg is None:
            return result
        if not self._classifier_cfg.enabled:
            return result
        return await self._apply_classification(state, runtime)

    # ------------------------------------------------------------------
    # Optional classifier pass (rule-unresolved calls only)
    # ------------------------------------------------------------------

    async def _apply_classification(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Classify ambiguous gated calls and act on trusted verdicts.

        Only calls the deterministic stages left unresolved (`default_approve`)
        reach the classifier, so most tool calls never pay for it. Verdicts
        that are untrusted (distribution shift / near-tie) or unavailable
        leave the deterministic outcome untouched unless `strict` is set.
        """
        from soothe.sloop.clarification.risk_classifier import RiskQuery

        ctx = AutoModeContext.from_runtime(runtime, default_mode=self._default_mode)
        view = _loop_view(ctx)
        if view is None or ctx.clarification_mode != "auto":
            return None

        try:
            messages = state["messages"]
        except (KeyError, TypeError):
            return None
        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        cfg = self._classifier_cfg
        candidates: list[tuple[int, Any]] = []
        for idx, tc in enumerate(last_ai_msg.tool_calls):
            if tc.get("name") not in self._tools:
                continue
            if not self._is_ambiguous(tc, ctx):
                continue
            candidates.append((idx, tc))
        if not candidates:
            return None

        bounded = candidates[: int(cfg.max_calls_per_turn)]
        queries = [
            RiskQuery(
                tool=str(tc.get("name") or ""),
                args_preview=self._args_preview(tc),
                goal_summary=view.goal_description or None,
            )
            for _, tc in bounded
        ]
        try:
            verdicts = await self._classifier.classify(queries)
        except Exception:  # noqa: BLE001 — never block on the classifier
            logger.warning("[auto_mode] classifier failed; keeping deterministic outcome")
            return None

        revised: list[Any] = []
        artificial: list[ToolMessage] = []
        verdict_by_idx = {idx: verdict for (idx, _), verdict in zip(bounded, verdicts)}
        changed = False
        for idx, tc in enumerate(last_ai_msg.tool_calls):
            verdict = verdict_by_idx.get(idx)
            if verdict is None or verdict.band in ("allow", "untrusted", "unavailable"):
                if verdict is not None and cfg.strict:
                    # strict: no trusted answer → do not allow silently.
                    artificial.append(self._classifier_reject_message(tc, verdict))
                    changed = True
                    continue
                revised.append(tc)
                continue
            if verdict.band == "reject":
                artificial.append(self._classifier_reject_message(tc, verdict))
                changed = True
                continue
            # escalate → human decision via the standard interrupt.
            payload = {
                "action_requests": [self._action_request(tc)],
                "review_configs": [self._review_config(tc)],
                "gate_deferred": True,
                "gate_deferred_kind": "classifier_escalate",
            }
            resumed = interrupt(payload)
            decisions = self._extract_decisions(resumed, expected=1)
            edited = self._apply_decision(tc, decisions[0], artificial)
            if edited is None:
                changed = True
                continue
            tc = edited
            revised.append(tc)

        if not changed and not artificial:
            return None
        last_ai_msg.tool_calls = revised
        return {"messages": [last_ai_msg, *artificial]}

    def _is_ambiguous(self, tool_call: Any, ctx: AutoModeContext) -> bool:
        """True when the deterministic stages left this call unresolved."""
        if ctx.bypass and not self._active_in_bypass:
            return False
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            result = self._pipeline.evaluate_action(
                str(tool_call.get("name") or ""),
                args,
                workspace_root=ctx.workspace,
                allowlist=list(ctx.allowlist),
                bypass=ctx.bypass,
            )
        except Exception:  # noqa: BLE001
            return False
        return result.decision == "approve" and result.stage == "default_approve"

    @staticmethod
    def _args_preview(tool_call: Any) -> dict[str, Any]:
        """Bounded, redacted argument preview for the classifier."""
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            return {}
        preview: dict[str, Any] = {}
        for key in _CLASSIFY_ARG_KEYS:
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if any(marker in key.lower() for marker in _SECRET_ARG_MARKERS):
                continue
            preview[key] = truncate_text(value.strip(), limit=_CLASSIFY_PREVIEW_CHARS)
            break
        return preview

    @staticmethod
    def _classifier_reject_message(tool_call: Any, verdict: Any) -> ToolMessage:
        """Instructive reject citing the classifier verdict."""
        from soothe.sloop.clarification.risk_classifier import RiskVerdict

        detail = verdict.reason if isinstance(verdict, RiskVerdict) else str(verdict)
        content = (
            f"Tool call `{tool_call['name']}` was blocked by the tool-approval "
            f"classifier ({detail}). The tool was not executed. Choose a "
            "different action or adjust the arguments; do not retry this exact "
            "call unless the goal changes."
        )
        return ToolMessage(
            content=content,
            name=tool_call["name"],
            tool_call_id=tool_call["id"],
            status="error",
        )

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
