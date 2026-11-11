"""Middleware: inject decompose prompts / tool on step THREADS."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage, ToolMessage

from soothe.prompts import (
    ASK_MODE_ADDENDUM,
    CONTEXT_PRESSURE_SPLIT_HINT,
    PARALLEL_NUDGE_ADDENDUM,
    PLAN_MODE_ADDENDUM,
    SPLIT_CANDIDATE_ADDENDUM,
    THREAD_POLICY_SYSTEM_ADDENDUM,
    WRITE_TODOS_TOOL_DESCRIPTION,
)
from soothe.sloop.decompose import runtime as _decompose_runtime
from soothe.sloop.decompose.tool import build_decompose_task_tool
from soothe.sloop.utils.config_keys import (
    SOOTHE_DECOMPOSE_STEP_ID_KEY,
    SOOTHE_EVAL_STEP_ID_KEY,
    SOOTHE_INTERACTION_MODE_KEY,
    SOOTHE_IS_DAG_ROOT_KEY,
    SOOTHE_MAX_BRANCH_ROOT_KEY,
    SOOTHE_STEP_DESCRIPTION_KEY,
    SOOTHE_TASK_COMPLEXITY_KEY,
    SOOTHE_USER_GOAL_KEY,
)

logger = logging.getLogger(__name__)

_DECOMPOSE_TOOL = build_decompose_task_tool()

# Per-step dedup for the injection DEBUG log. `modify_request` fires on
# every model call within a step, so without dedup a single step floods the
# log with identical "injecting decompose_task" lines (1549× in one loop was
# observed). This is ephemeral diagnostic state — not loop state — and never
# needs to survive a worker exit; it only suppresses redundant DEBUG lines.
_INJECTION_LOGGED_STEPS: set[str] = set()
_INJECTION_LOG_CAP = 512

# Per-step cache for the TypeSafe finish-vs-split decision. Set from
# `awrap_model_call` (async) before `modify_request` (sync) runs, so the
# sync hook can read the cached verdict without making its own async call.
# `None` = not yet evaluated; "finish" = strip decompose; "split" = root
# nudge / re-check addendum; anything else = keep decompose (fail-safe).
_jev_finish_split: ContextVar[str | None] = ContextVar("jev_finish_split", default=None)
_jev_finish_split_step: ContextVar[str | None] = ContextVar("jev_finish_split_step", default=None)

# RFC-SLP-006 §2: mid-thread re-check state. Tool completions accumulate in
# `_jev_tool_calls`; crossing a recheck_tool_calls milestone (bounded by
# `_jev_rechecks_done`) invalidates the verdict cache so the next model
# call re-evaluates with the trajectory. Never re-evaluates without new tool
# results — the 7d10 classifier-churn lesson.
_jev_tool_calls: ContextVar[int] = ContextVar("jev_tool_calls", default=0)
_jev_recent_tools: ContextVar[list[str] | None] = ContextVar("jev_recent_tools", default=None)
_jev_rechecks_done: ContextVar[int] = ContextVar("jev_rechecks_done", default=0)

_RECENT_TOOL_DIGESTS = 8
_TOOL_DIGEST_CHARS = 120
_DEFAULT_CONTEXT_LIMIT = 200_000


def reset_decompose_injection_log_state() -> None:
    """Clear per-step injection-log dedup state (test isolation)."""
    _INJECTION_LOGGED_STEPS.clear()


def _decompose_cfg(conf: dict[str, Any]) -> Any | None:
    """Best-effort `agent.loop.decompose` config; None when absent."""
    sc = conf.get("soothe_config")
    try:
        return sc.agent.loop.decompose
    except AttributeError:
        return None


def _recheck_interval(conf: dict[str, Any]) -> int:
    """Tool-completion milestone interval for re-checks; 0 disables."""
    value = getattr(_decompose_cfg(conf), "recheck_tool_calls", 10)
    return value if isinstance(value, int) and value > 0 else 0


def _max_rechecks(conf: dict[str, Any]) -> int:
    value = getattr(_decompose_cfg(conf), "max_rechecks_per_step", 2)
    return value if isinstance(value, int) and value > 0 else 0


def _split_hint_pct(conf: dict[str, Any]) -> float:
    value = getattr(_decompose_cfg(conf), "split_hint_threshold_pct", 0.60)
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def _root_split_nudge_enabled(conf: dict[str, Any]) -> bool:
    return getattr(_decompose_cfg(conf), "root_split_nudge", True) is not False


def _context_limit(conf: dict[str, Any]) -> int:
    sc = conf.get("soothe_config")
    try:
        return int(sc.agent.loop.context_window_limit)
    except (AttributeError, TypeError, ValueError):
        return _DEFAULT_CONTEXT_LIMIT


def _system_text(request: Any) -> str:
    """Existing system message text (str content or joined text blocks)."""
    system = getattr(request, "system_message", None)
    content = getattr(system, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _estimate_request_tokens(request: Any) -> int:
    """Coarse token estimate: message text bytes ÷ 4 (hint-only precision)."""
    total = 0
    for message in getattr(request, "messages", None) or []:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(b.get("text", ""))) for b in content if isinstance(b, dict))
    return total // 4


def _context_pressure_hint(request: Any, conf: dict[str, Any]) -> str | None:
    """RFC-SLP-006 §5: split hint below the compaction threshold."""
    pct = _split_hint_pct(conf)
    if pct <= 0:
        return None
    threshold = int(_context_limit(conf) * pct)
    if _estimate_request_tokens(request) < threshold:
        return None
    return CONTEXT_PRESSURE_SPLIT_HINT


def _track_tool_completion(tool_name: str, result: Any) -> None:
    """Count tool completions on agent-mode step threads; arm re-checks (RFC-SLP-006 §2)."""
    conf = _decompose_runtime.langgraph_configurable()
    if conf.get(SOOTHE_EVAL_STEP_ID_KEY):
        return
    step_id = _decompose_runtime.current_step_id() or conf.get(SOOTHE_DECOMPOSE_STEP_ID_KEY)
    if not step_id:
        return
    if conf.get(SOOTHE_INTERACTION_MODE_KEY) in ("plan", "ask"):
        return
    interval = _recheck_interval(conf)
    if interval <= 0:
        return
    count = _jev_tool_calls.get() + 1
    _jev_tool_calls.set(count)
    content = getattr(result, "content", result)
    digests = list(_jev_recent_tools.get() or [])
    digests.append(f"{tool_name}: {str(content)[:_TOOL_DIGEST_CHARS]}")
    _jev_recent_tools.set(digests[-_RECENT_TOOL_DIGESTS:])
    if count % interval == 0 and _jev_rechecks_done.get() < _max_rechecks(conf):
        _jev_rechecks_done.set(_jev_rechecks_done.get() + 1)
        _jev_finish_split_step.set(None)
        logger.info("[decompose] re-check armed at tool call %d (step=%s)", count, step_id)


async def _try_jev_finish_vs_split(conf: dict[str, Any]) -> str | None:
    """Run a TypeSafe finish-vs-split decision for the current step.

    Returns "finish" / "split" on trusted verdicts, None otherwise. The
    verdict is cached in `_jev_finish_split` keyed by step id so the sync
    `modify_request` hook can read it without an async call. On a re-check
    (cache invalidated mid-step), an untrusted or unavailable verdict keeps
    the prior surface — a t=0 strip stays sticky.
    """
    step_id = _decompose_runtime.current_step_id() or conf.get(SOOTHE_DECOMPOSE_STEP_ID_KEY)
    if not step_id:
        return None

    # Already evaluated for this step — return cached result.
    cached_step = _jev_finish_split_step.get()
    if cached_step == step_id:
        return _jev_finish_split.get()

    # Not yet evaluated (or invalidated for a re-check) — run the decision.
    previous_verdict = _jev_finish_split.get()
    _jev_finish_split_step.set(step_id)
    _jev_finish_split.set(None)

    soothe_config = conf.get("soothe_config")
    step_description = conf.get(SOOTHE_STEP_DESCRIPTION_KEY, "")
    user_goal = conf.get(SOOTHE_USER_GOAL_KEY, "")
    task_complexity = conf.get(SOOTHE_TASK_COMPLEXITY_KEY)

    if not step_description:
        return None

    try:
        from soothe.sloop.decompose.decompose_typesafe import (
            decide_finish_vs_split_typesafe,
        )

        verdict = await decide_finish_vs_split_typesafe(
            step_description=step_description,
            user_goal=user_goal,
            task_complexity=task_complexity,
            tool_call_count=_jev_tool_calls.get(),
            recent_tools=_jev_recent_tools.get(),
            soothe_config=soothe_config,
        )
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.info("[decompose] typesafe finish/split unavailable (%s)", exc)
        _jev_finish_split.set(previous_verdict)
        return previous_verdict

    if verdict == "finish":
        _jev_finish_split.set("finish")
        logger.info("[decompose] typesafe: finish in-thread (step=%s)", step_id)
        return "finish"

    if verdict == "split":
        _jev_finish_split.set("split")
        logger.info(
            "[decompose] typesafe: split (step=%s recheck=%s)",
            step_id,
            _jev_rechecks_done.get() > 0,
        )
        return "split"

    # Untrusted verdict — keep the prior surface (strip stays sticky).
    _jev_finish_split.set(previous_verdict)
    return previous_verdict


def _reset_jev_finish_split_cache() -> None:
    """Clear the JEV finish-vs-split cache and re-check state (test isolation)."""
    _jev_finish_split.set(None)
    _jev_finish_split_step.set(None)
    _jev_tool_calls.set(0)
    _jev_recent_tools.set(None)
    _jev_rechecks_done.set(0)


def _override_write_todos_description(tools: list[Any]) -> list[Any]:
    out: list[Any] = []
    for tool in tools:
        name = getattr(tool, "name", None) or getattr(tool, "get", lambda *_: None)("name")
        if name == "write_todos" and hasattr(tool, "description"):
            try:
                cloned = tool.model_copy(update={"description": WRITE_TODOS_TOOL_DESCRIPTION})
                out.append(cloned)
                continue
            except Exception:
                try:
                    tool.description = WRITE_TODOS_TOOL_DESCRIPTION
                except Exception:
                    pass
        out.append(tool)
    return out


def _ensure_decompose_tool(tools: list[Any]) -> list[Any]:
    names = {getattr(t, "name", None) for t in tools}
    if "decompose_task" in names:
        return tools
    return [*tools, _DECOMPOSE_TOOL]


def _strip_decompose_tool(tools: list[Any]) -> list[Any]:
    return [t for t in tools if getattr(t, "name", None) != "decompose_task"]


class DecomposeTaskMiddleware(AgentMiddleware):
    """Inject `decompose_task` + THREAD policy on step threads.

    Active when a StrangeLoop step id is bound (contextvar or LangGraph
    configurable `soothe_decompose_step_id`). Hidden on non-step threads
    (synthesis, intake specialists, etc.).

    In plan/ask modes the tool is stripped from the schema (coded policy)
    and an `awrap_tool_call` guard intercepts stray calls with a guidance
    message. System gets finish-vs-split / write_todos / hygiene policy +
    mode-specific addendum; user envelope stays instance-focused.
    """

    tools = [_DECOMPOSE_TOOL]

    # ------------------------------------------------------------------
    # Mode resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _active_mode(conf: dict[str, Any]) -> str | None:
        """Return `"plan"`, `"ask"`, or `None` (agent) from config."""
        mode = conf.get(SOOTHE_INTERACTION_MODE_KEY)
        return mode if mode in ("plan", "ask") else None

    # ------------------------------------------------------------------
    # Agent-mode optional addenda (RFC-SLP-006)
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_addenda(conf: dict[str, Any]) -> list[str]:
        """Agent-mode optional prompt fragments for the current step.

        P1 nudge: a DAG root with client-forced complexity OR a trusted t=0
        split verdict. P2 addendum: a mid-step re-check flipped to split.
        """
        fragments: list[str] = []
        is_root = conf.get(SOOTHE_IS_DAG_ROOT_KEY) is True
        verdict = _jev_finish_split.get()
        task_complexity = conf.get(SOOTHE_TASK_COMPLEXITY_KEY)
        forced_complex = isinstance(task_complexity, str) and task_complexity.lower() == "complex"
        rechecked = _jev_rechecks_done.get() > 0

        if is_root and (forced_complex or (verdict == "split" and not rechecked)):
            if forced_complex or _root_split_nudge_enabled(conf):
                fragments.append(PARALLEL_NUDGE_ADDENDUM)
        elif verdict == "split" and rechecked:
            fragments.append(SPLIT_CANDIDATE_ADDENDUM)
        return fragments

    # ------------------------------------------------------------------
    # Tool-call guard (coded policy) + re-check tracking
    # ------------------------------------------------------------------

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Intercept `decompose_task` in plan/ask modes; track completions."""
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name", ""))
        if tool_name != "decompose_task":
            result = await handler(request)
            _track_tool_completion(tool_name, result)
            return result
        conf = _decompose_runtime.langgraph_configurable()
        mode = self._active_mode(conf)
        if mode is None:
            result = await handler(request)
            _track_tool_completion(tool_name, result)
            return result
        step_id = _decompose_runtime.current_step_id() or conf.get(
            SOOTHE_DECOMPOSE_STEP_ID_KEY, "?"
        )
        logger.info(
            "[decompose] blocked decompose_task call in %s mode (step=%s)",
            mode,
            step_id,
        )
        if mode == "plan":
            guidance = (
                "decompose_task is not available in Plan mode. "
                "Finish your research and output the plan document in this thread. "
                "Do not split into subtasks — produce the full plan now."
            )
        else:
            guidance = (
                "decompose_task is not available in Ask mode. "
                "Answer the user's question directly in this thread. "
                "Do not split into subtasks."
            )
        return ToolMessage(
            content=guidance,
            tool_call_id=tool_call.get("id", ""),
            name="decompose_task",
        )

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """Sync variant of the tool-call guard."""
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name", ""))
        if tool_name != "decompose_task":
            result = handler(request)
            _track_tool_completion(tool_name, result)
            return result
        conf = _decompose_runtime.langgraph_configurable()
        mode = self._active_mode(conf)
        if mode is None:
            result = handler(request)
            _track_tool_completion(tool_name, result)
            return result
        step_id = _decompose_runtime.current_step_id() or conf.get(
            SOOTHE_DECOMPOSE_STEP_ID_KEY, "?"
        )
        logger.info(
            "[decompose] blocked decompose_task call in %s mode (step=%s)",
            mode,
            step_id,
        )
        if mode == "plan":
            guidance = (
                "decompose_task is not available in Plan mode. "
                "Finish your research and output the plan document in this thread. "
                "Do not split into subtasks — produce the full plan now."
            )
        else:
            guidance = (
                "decompose_task is not available in Ask mode. "
                "Answer the user's question directly in this thread. "
                "Do not split into subtasks."
            )
        return ToolMessage(
            content=guidance,
            tool_call_id=tool_call.get("id", ""),
            name="decompose_task",
        )

    # ------------------------------------------------------------------
    # Tool-set injection / system-prompt addendum
    # ------------------------------------------------------------------

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject decompose-task tool and prompt addendum for step-mode threads."""
        conf = _decompose_runtime.langgraph_configurable()
        if conf.get(SOOTHE_EVAL_STEP_ID_KEY):
            # EvalStepMiddleware owns the Eval tool/prompt policy
            # (full tool surface + coverage-audit addendum + decompose_task).
            return request
        step_id = _decompose_runtime.current_step_id() or conf.get(SOOTHE_DECOMPOSE_STEP_ID_KEY)
        if not step_id:
            tools = list(request.tools or [])
            stripped = _strip_decompose_tool(tools)
            return request.override(tools=stripped) if len(stripped) != len(tools) else request

        mode = self._active_mode(conf)
        tools = list(request.tools or [])
        tools = _override_write_todos_description(tools)
        agent_optional: list[str] = []

        if mode in ("plan", "ask"):
            # Coded policy: strip decompose_task so the LLM can't call it.
            before = len(tools)
            tools = _strip_decompose_tool(tools)
            logger.debug(
                "[decompose] stripped decompose_task for %s mode (step=%s tools=%d→%d)",
                mode,
                step_id,
                before,
                len(tools),
            )
        else:
            # Agent mode: check JEV finish-vs-split cache first.
            # "finish" strips decompose_task; anything else keeps it.
            jev_verdict = _jev_finish_split.get()
            agent_optional = self._agent_addenda(conf)
            if jev_verdict == "finish":
                before = len(tools)
                tools = _strip_decompose_tool(tools)
                if step_id not in _INJECTION_LOGGED_STEPS:
                    if len(_INJECTION_LOGGED_STEPS) >= _INJECTION_LOG_CAP:
                        _INJECTION_LOGGED_STEPS.clear()
                    _INJECTION_LOGGED_STEPS.add(step_id)
                    logger.debug(
                        "[decompose] stripped decompose_task per JEV finish "
                        "verdict (step=%s tools=%d→%d)",
                        step_id,
                        before,
                        len(tools),
                    )
            else:
                # Agent mode: inject decompose_task as usual.
                before = len(tools)
                already_present = "decompose_task" in {getattr(t, "name", None) for t in tools}
                tools = _ensure_decompose_tool(tools)
                if step_id not in _INJECTION_LOGGED_STEPS:
                    if len(_INJECTION_LOGGED_STEPS) >= _INJECTION_LOG_CAP:
                        _INJECTION_LOGGED_STEPS.clear()
                    _INJECTION_LOGGED_STEPS.add(step_id)
                    logger.debug(
                        "[decompose] injecting decompose_task on step %s thread "
                        "(tools=%d→%d already_present=%s)",
                        step_id,
                        before,
                        len(tools),
                        already_present,
                    )
                # P5: context-pressure split hint — only when the tool is
                # actually available to act on it.
                hint = _context_pressure_hint(request, conf)
                if hint is not None and hint not in agent_optional:
                    agent_optional.append(hint)

        # Build the system-prompt addendum. Each fragment is checked against
        # the existing system text individually so a fragment appearing
        # mid-step (re-check verdict, pressure hint) never duplicates the
        # base policy.
        existing_text = _system_text(request)
        base = THREAD_POLICY_SYSTEM_ADDENDUM
        # Inject the branch cap so the LLM stays under the limit.
        max_branch_root = conf.get(SOOTHE_MAX_BRANCH_ROOT_KEY)
        if isinstance(max_branch_root, int) and max_branch_root > 0:
            base = (
                f"{base}\n\n"
                f"## decompose_task subtask limit\n\n"
                f"Propose at most {max_branch_root} child subtasks per "
                f"decompose_task call. Excess subtasks are truncated."
            )
        if mode == "ask":
            base = f"{base}\n\n{ASK_MODE_ADDENDUM}"
        elif mode == "plan":
            base = f"{base}\n\n{PLAN_MODE_ADDENDUM}"

        pending: list[str] = []
        if base and base not in existing_text:
            pending.append(base)
        for fragment in agent_optional:
            if fragment and fragment not in base and fragment not in existing_text:
                pending.append(fragment)

        if not pending:
            return request.override(tools=tools)
        addendum = "\n\n".join(pending)

        system = request.system_message
        if system is not None and hasattr(system, "content"):
            content = system.content
            if isinstance(content, str):
                new_system = SystemMessage(content=f"{content}\n\n{addendum}")
                return request.override(tools=tools, system_message=new_system)
            if isinstance(content, list):
                new_blocks = [
                    *content,
                    {"type": "text", "text": f"\n\n{addendum}"},
                ]
                new_system = SystemMessage(content=new_blocks)
                return request.override(tools=tools, system_message=new_system)

        return request.override(tools=tools)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Apply decompose injection before the sync model call."""
        return handler(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Apply decompose injection before the async model call.

        Before the sync `modify_request` runs, evaluate the TypeSafe
        finish-vs-split decision for the current step (once per step at t=0,
        re-run after a re-check milestone invalidates the cache). The cached
        verdict is read by `modify_request` to shape the tool surface.
        """
        conf = _decompose_runtime.langgraph_configurable()
        step_id = _decompose_runtime.current_step_id() or conf.get(SOOTHE_DECOMPOSE_STEP_ID_KEY)
        if step_id and not conf.get(SOOTHE_EVAL_STEP_ID_KEY):
            mode = self._active_mode(conf)
            if mode is None:
                # Agent mode — run the JEV finish-vs-split decision.
                await _try_jev_finish_vs_split(conf)
        return await handler(self.modify_request(request))
