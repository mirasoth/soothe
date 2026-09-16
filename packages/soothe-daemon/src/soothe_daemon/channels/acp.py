"""ACP (Agent Client Protocol) channel — multi-transport JSON-RPC server.

Implements the ACP server as a daemon `Channel` with two transports:
stdio (default, single-connection NDJSON JSON-RPC 2.0) and websocket
(route on the unified FastAPI app, multi-connection). Translates ACP
`session/*` methods into daemon-internal calls (`handle_inbound`,
cancel events, resume). Daemon EventBus output events are translated
to ACP `session/update` notifications.

Plan projection is lossy: Soothe DAG plans are flattened to ACP's
`{content, priority, status}` list. ACP accepts only three plan statuses and
requires `priority`, and the client SDK silently drops any entry outside those
enums — so per-step statistics (duration, tool count, tokens, summary, DAG
dependencies) and the true outcome of a failed step travel in
`_meta.soothe.*`, which ACP reserves for exactly this. See
`docs/soothe-acp-step-projection.md` in the Backchat repo for the contract.
The `agent-client-protocol` package is an optional `[acp]` extra; without it,
JSON-RPC framing is built manually.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import sys
import uuid
from logging import getLogger
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket
from soothe_sdk.core.events import (
    STRANGE_LOOP_COMPLETED,
    STRANGE_LOOP_PLAN_DECISION,
    STRANGE_LOOP_STEP_COMPLETED,
    STRANGE_LOOP_STEP_QUEUED,
    STRANGE_LOOP_STEP_STARTED,
)
from soothe_sdk.display.text_extract import extract_text_from_ai_message
from soothe_sdk.ux.loop_stream import assistant_output_phase
from starlette.websockets import WebSocketDisconnect

from soothe_daemon.channels.base import Channel
from soothe_daemon.channels.message import ChannelMessage
from soothe_daemon.config.models import ACPConfig
from soothe_daemon.event import loop_event_topic
from soothe_daemon.events.constants import (
    OUTPUT_PROGRESS,
    OUTPUT_REASONING,
    OUTPUT_TEXT_COMPLETE,
    OUTPUT_TEXT_DELTA,
    OUTPUT_TEXT_END,
)

if TYPE_CHECKING:
    from soothe_daemon.channel_manager import ChannelManager

logger = getLogger(__name__)

# Try to import ACP helpers for block construction (optional).
# Falls back to manual dict construction when the SDK is not installed.
try:
    from acp import helpers as _acp_helpers  # type: ignore[import-not-found]
    from acp.meta import PROTOCOL_VERSION as _ACP_PROTOCOL_VERSION  # type: ignore[import-not-found]
except ImportError:
    _acp_helpers = None  # type: ignore[assignment]
    _ACP_PROTOCOL_VERSION = 1

# Message kinds that carry user-visible assistant prose. Tool/system messages
# also ride ``mode="messages"`` frames and must not be rendered as answers.
_ASSISTANT_MESSAGE_TYPES = frozenset({"ai", "AIMessage", "AIMessageChunk"})

# ----------------------------------------------------------------------
# Plan projection (Soothe strange-loop steps -> ACP `plan` updates)
# ----------------------------------------------------------------------

# Soothe step phase -> ACP `PlanEntryStatus`.
#
# ACP's `plan` variant accepts *exactly* `pending | in_progress | completed`.
# This is not advisory: the pinned client SDK validates `plan` updates against
# that union and **silently drops any entry whose status is outside it** — the
# frame still succeeds, the step just disappears from the client's plan. So
# every phase must map onto a whitelisted value, and unknown phases fall back
# to `pending` (see ``_PLAN_PHASE_TO_ACP_STATUS.get(phase, "pending")``).
#
# A failed step therefore has to travel as `completed` with the truth carried
# in `_meta.soothe.outcome`; `cancelled`/`failed` are not representable here.
_PLAN_PHASE_TO_ACP_STATUS: dict[str, str] = {
    "pending": "pending",
    "queued": "pending",
    "running": "in_progress",
    "done": "completed",
    "success": "completed",
    "error": "completed",
    "interrupted": "completed",
}

# ACP requires `priority` on every entry and Soothe has no priority concept.
# Omitting it would drop the entry, so send a constant.
_PLAN_ENTRY_PRIORITY = "medium"

# `_meta.soothe.summary` is a display hint, not an assertion channel; keep it
# bounded so one long step summary cannot bloat every subsequent plan update.
_PLAN_SUMMARY_LIMIT = 512


class _PlanStep:
    """Accumulated Soothe step state for one ACP plan entry."""

    __slots__ = (
        "step_id",
        "description",
        "dependencies",
        "phase",
        "success",
        "duration_ms",
        "tool_call_count",
        "tokens_used",
        "summary",
    )

    def __init__(self, step_id: str, description: str = "") -> None:
        self.step_id = step_id
        self.description = description
        self.dependencies: list[str] = []
        self.phase = "pending"
        self.success: bool | None = None
        self.duration_ms = 0
        self.tool_call_count = 0
        self.tokens_used = 0
        self.summary = ""

    @property
    def settled(self) -> bool:
        """True once the step reached a terminal phase."""
        return self.phase in ("done", "error", "interrupted")


class _PlanState:
    """Accumulated ACP plan for one loop.

    ACP requires every `plan` update to carry the **complete** entry list and
    the client to replace its plan wholesale, so this accumulates rather than
    forwarding per-event deltas. Merging mirrors the TUI's `sync_plan_steps`:
    settled steps survive later `plan.decision` frames (Soothe re-plans per
    iteration), and only never-started `pending` steps can be dropped.
    """

    __slots__ = ("order", "steps", "iteration", "_last_emitted")

    def __init__(self) -> None:
        self.order: list[str] = []
        self.steps: dict[str, _PlanStep] = {}
        self.iteration = 0
        self._last_emitted: str | None = None

    def sync_plan(self, raw_steps: list[Any], iteration: int) -> None:
        """Merge a `plan.decision` step list into the accumulated plan."""
        self.iteration = iteration
        planned: set[str] = set()
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            step_id = str(raw.get("id") or "").strip()
            if not step_id:
                continue
            planned.add(step_id)
            description = str(raw.get("description") or "").strip()
            existing = self.steps.get(step_id)
            if existing is None:
                step = _PlanStep(step_id, description or "(step)")
                step.dependencies = _plan_dependencies(raw.get("dependencies"))
                self.order.append(step_id)
                self.steps[step_id] = step
                continue
            # Refresh descriptive fields only while the step is not settled;
            # a finished step's description is history, not a re-plan target.
            if not existing.settled and description:
                existing.description = description
            deps = _plan_dependencies(raw.get("dependencies"))
            if deps and not existing.settled:
                existing.dependencies = deps

        # Drop planned-but-never-started steps that this replan abandoned.
        for step_id in list(self.order):
            step = self.steps.get(step_id)
            if step is not None and step.phase == "pending" and step_id not in planned:
                self.order.remove(step_id)
                del self.steps[step_id]

    def mark(self, step_id: str, phase: str, description: str = "") -> None:
        """Move a step to ``phase``, creating it if it was never planned."""
        step = self.steps.get(step_id)
        if step is None:
            step = _PlanStep(step_id, description or "(step)")
            self.order.append(step_id)
            self.steps[step_id] = step
        elif description and not step.settled:
            step.description = description
        if step.settled:
            # A settled step never regresses; late/out-of-order frames for an
            # already-finished step must not reopen it.
            return
        step.phase = phase

    def settle(self, step_id: str, success: bool, **stats: Any) -> None:
        """Record a terminal phase with its statistics."""
        step = self.steps.get(step_id)
        if step is None:
            step = _PlanStep(step_id, "(step)")
            self.order.append(step_id)
            self.steps[step_id] = step
        step.phase = "done" if success else "error"
        step.success = success
        step.duration_ms = int(stats.get("duration_ms") or 0)
        step.tool_call_count = int(stats.get("tool_call_count") or 0)
        step.tokens_used = int(stats.get("total_tokens_used") or 0)
        summary = str(stats.get("summary") or "").strip()
        step.summary = summary[:_PLAN_SUMMARY_LIMIT]

    def close_open_steps(self) -> None:
        """Converge still-open steps when the loop ends without settling them.

        ACP has no cancelled plan status, so these land on `completed` with
        ``_meta.soothe.outcome == "error"`` — the same encoding as a failed
        step, which is what an abandoned step is from the plan's perspective.
        """
        for step in self.steps.values():
            if not step.settled:
                step.phase = "error"
                step.success = False

    def entries(self) -> list[dict[str, Any]]:
        """Serialize the accumulated plan to ACP `plan` entries."""
        entries: list[dict[str, Any]] = []
        for step_id in self.order:
            step = self.steps.get(step_id)
            if step is None:
                continue
            meta: dict[str, Any] = {"step_id": step.step_id, "phase": step.phase}
            if step.dependencies:
                meta["depends_on"] = list(step.dependencies)
            if step.settled:
                meta["outcome"] = "ok" if step.success else "error"
                meta["duration_ms"] = step.duration_ms
                meta["tool_call_count"] = step.tool_call_count
                meta["tokens_used"] = step.tokens_used
                if step.summary:
                    meta["summary"] = step.summary
            entries.append(
                {
                    "content": step.description or "(step)",
                    "priority": _PLAN_ENTRY_PRIORITY,
                    "status": _PLAN_PHASE_TO_ACP_STATUS.get(step.phase, "pending"),
                    "_meta": {"soothe": meta},
                }
            )
        return entries

    def update(self) -> dict[str, Any] | None:
        """Build the `session/update` body, or None when there is no plan.

        An empty `entries` list is a legal frame, but clients read it as
        "clear the plan" — never send one just because nothing is planned.

        ``total_steps``/``done_steps`` are **derived from the entries actually
        being sent**, not passed through from the source event: the source's
        `plan.decision` counts are cumulative-across-iterations while `entries`
        is the client-visible list, so passing them through would let the two
        disagree (and `done_steps` would go stale the moment a step settled).
        """
        entries = self.entries()
        if not entries:
            return None
        settled = sum(1 for step in self.steps.values() if step.settled)
        return {
            "sessionUpdate": "plan",
            "entries": entries,
            "_meta": {
                "soothe": {
                    "iteration": self.iteration,
                    "total_steps": len(entries),
                    "done_steps": settled,
                }
            },
        }

    def emit_update(self) -> dict[str, Any] | None:
        """`update()`, but suppressed when identical to the last frame emitted.

        Distinct source events legitimately produce the same plan — e.g.
        `step.completed` settles the last open step, then `strange_loop.completed`
        converges nothing. Re-sending an unchanged full plan is pure wire cost,
        so skip it rather than relying on a time-based debounce.
        """
        update = self.update()
        if update is None:
            return None
        fingerprint = json.dumps(update, sort_keys=True)
        if fingerprint == self._last_emitted:
            return None
        self._last_emitted = fingerprint
        return update


def _plan_dependencies(raw: Any) -> list[str]:
    """Normalize a plan-decision `dependencies` field to a string list."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


# Default timeout for permission responses from the ACP client (seconds).
_PERMISSION_TIMEOUT_S = 120.0

# JSON-RPC error codes.
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603

# Sentinel key used for the single stdio connection in the _connections dict.
# In stdio mode there is exactly one connection (the stdin/stdout pipe); in
# WebSocket mode each connected client gets its own _ConnectionState entry
# keyed by its WebSocket object.
_STDIO_SENTINEL: str = "__stdio__"

# Context var tracking the connection currently being serviced. Protocol
# handlers and the EventBus consumer use this to route output to the correct
# transport endpoint without an explicit connection parameter at every call
# site. Set by _read_stdin_loop / _handle_ws_connection before dispatching.
_current_connection: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "_acp_current_connection", default=_STDIO_SENTINEL
)


class _SessionState:
    """Per-session state tracking for production-ready ACP compliance.

    Tracks the cwd, active mode, config options, and open documents for a
    single ACP session. This replaces the previous stub handlers that
    accepted requests but discarded all state.

    Attributes:
        cwd: Working directory the session was created with.
        current_mode: Active mode ID (default "default").
        config_options: Config option values set via session/set_config_option.
        documents: Open documents keyed by URI (uri → text/version dict).
        focused_uri: Set of URIs currently focused in the editor.
    """

    __slots__ = (
        "cwd",
        "current_mode",
        "config_options",
        "documents",
        "focused_uri",
    )

    def __init__(self, cwd: str = "/tmp") -> None:
        self.cwd: str = cwd
        self.current_mode: str = "default"
        self.config_options: dict[str, Any] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.focused_uri: set[str] = set()


class _ConnectionState:
    """Per-connection state for a single ACP client.

    In stdio mode a single `_ConnectionState` is stored under the
    `_STDIO_SENTINEL` key. In WebSocket mode each connected WebSocket gets
    its own instance, enabling multiple concurrent sessions with isolated
    session maps, permission futures, and event queues.

    Attributes:
        session_map: ACP session_id → daemon loop_id.
        session_states: ACP session_id → _SessionState (per-session metadata).
        nes_sessions: NES session_id → metadata dict (workspaceUri, suggestions).
        pending_permissions: request_id → future awaiting client response.
        event_queues: loop_id → EventBus event queue.
        consumer_tasks: loop_id → event consumer asyncio task.
        plan_states: loop_id → _PlanState (accumulated ACP plan projection).
    """

    __slots__ = (
        "session_map",
        "session_states",
        "nes_sessions",
        "pending_permissions",
        "event_queues",
        "consumer_tasks",
        "pending_turns",
        "plan_states",
    )

    def __init__(self) -> None:
        self.session_map: dict[str, str] = {}
        self.session_states: dict[str, _SessionState] = {}
        self.nes_sessions: dict[str, dict[str, Any]] = {}
        self.pending_permissions: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # loop_id → FIFO of futures awaiting turn end. ACP answers
        # `session/prompt` when the loop reports idle; dispatcher-serialized
        # turns report idle in submission order.
        self.pending_turns: dict[str, list[asyncio.Future[str]]] = {}
        self.event_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.consumer_tasks: dict[str, asyncio.Task[None]] = {}
        self.plan_states: dict[str, _PlanState] = {}


def _make_text_block(content: str) -> dict[str, Any]:
    """Build an internal text block (a plain dict, not an ACP model)."""
    return {"type": "text", "text": content}


def _make_reasoning_block(content: str) -> dict[str, Any]:
    """Build an internal reasoning block."""
    return {"type": "reasoning", "text": content}


def _make_progress_block(message: str) -> dict[str, Any]:
    """Build an ACP progress block."""
    # ACP doesn't have a dedicated progress block; use text with marker.
    # This is a best-effort projection for editor UX.
    return {"type": "progress", "text": message}


def _iter_wire_frames(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a broadcast message into its individual stream frames.

    A coalesced batch arrives as ``{"type": "event_batch", "events": [...]}``;
    anything else is already a single frame.
    """
    if event.get("type") != "event_batch":
        return [event]
    events = event.get("events")
    if not isinstance(events, list):
        return []
    return [frame for frame in events if isinstance(frame, dict)]


def _is_idle_status(frame: dict[str, Any]) -> bool:
    """True for the loop-scoped frame that marks a turn as finished."""
    return frame.get("type") == "status" and frame.get("state") == "idle"


def _session_update_from_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Map one internal content block to a conformant ACP ``SessionUpdate``.

    ``progress`` has no ACP equivalent and rides the thought channel.
    """
    text = block.get("text")
    if not isinstance(text, str) or not text:
        return None

    block_type = block.get("type")
    if block_type == "text":
        if _acp_helpers is not None:
            update = _acp_helpers.update_agent_message_text(text)
            return update.model_dump(by_alias=True, exclude_none=True)
        return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}

    if block_type in ("reasoning", "progress"):
        if _acp_helpers is not None:
            update = _acp_helpers.update_agent_thought_text(text)
            return update.model_dump(by_alias=True, exclude_none=True)
        return {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}

    return None


class ACPChannel(Channel):
    """ACP channel — JSON-RPC 2.0 over stdio or WebSocket.

    The transport is selected by `ACPConfig.transport`:
    - `stdio` (default): single-connection NDJSON JSON-RPC over
      stdin/stdout. Used by the `soothe-acp` console script.
    - `websocket`: registers a WebSocket route on the unified FastAPI app
      at `ACPConfig.ws_path`. Supports multiple concurrent connections,
      each with isolated session state.

    When enabled as the sole channel (WebSocket disabled), the daemon runs in
    standalone ACP mode. The `soothe-acp` console script boots this mode.
    """

    name = "acp"
    display_name = "ACP"
    supports_inbound = True
    supports_outbound = True
    supports_streaming = True

    def __init__(
        self,
        config: ACPConfig,
        manager: ChannelManager,
        *,
        unified_app: FastAPI | None = None,
    ) -> None:
        """Initialize ACP channel.

        Args:
            config: ACP channel configuration.
            manager: Channel manager that owns this channel.
            unified_app: Optional shared FastAPI application. When provided
                and `config.transport == "websocket"`, the ACP WebSocket
                route is registered on this app instead of launching a
                standalone server.
        """
        super().__init__(config, manager)
        self._acp_config = config
        self._unified_parent_app = unified_app

        # Per-connection state keyed by connection identifier.
        # In stdio mode the single connection uses `_STDIO_SENTINEL`.
        # In WebSocket mode each WebSocket client is a key.
        self._connections: dict[Any, _ConnectionState] = {}

        # Stdin reader task (stdio mode only).
        self._stdin_task: asyncio.Task[None] | None = None

        # Running flag
        self._running = False

        # Monotonic request ID counter for outbound JSON-RPC requests.
        self._next_request_id = 1

        # Whether the WS route has been registered on the unified app.
        self._ws_route_registered = False

    # ------------------------------------------------------------------
    # Per-connection state accessors
    # ------------------------------------------------------------------

    def _get_state(self, connection: Any = None) -> _ConnectionState:
        """Return the `_ConnectionState` for the given connection.

        If `connection` is `None`, the current connection (from the
        context var) is used. If no state exists for the connection, a new
        `_ConnectionState` is created and stored.

        Args:
            connection: Connection key (WebSocket or `_STDIO_SENTINEL`).
                Defaults to `_current_connection` context var.

        Returns:
            The `_ConnectionState` for the connection.
        """
        if connection is None:
            connection = _current_connection.get()
        state = self._connections.get(connection)
        if state is None:
            state = _ConnectionState()
            self._connections[connection] = state
        return state

    async def start(self) -> None:
        """Start the ACP channel — dispatch on configured transport.

        - `stdio`: launch the stdin reader loop (existing behavior).
        - `websocket`: register a WS route on the unified FastAPI app.
        """
        if not self._acp_config.enabled:
            logger.info("[ACP] Channel disabled")
            return

        self._running = True

        if self._acp_config.transport == "websocket":
            await self._start_ws_transport()
        else:
            await self._start_stdio_transport()

    async def _start_stdio_transport(self) -> None:
        """Launch the stdin reader loop for stdio transport."""
        self._stdin_task = asyncio.create_task(self._read_stdin_loop())
        logger.info(
            "[ACP] Channel started (agent_name=%s, stdio JSON-RPC)",
            self._acp_config.agent_name,
        )

    async def _start_ws_transport(self) -> None:
        """Register the ACP WebSocket route on the unified FastAPI app."""
        if self._unified_parent_app is None:
            logger.error(
                "[ACP] WebSocket transport requires a unified FastAPI app; falling back to stdio"
            )
            await self._start_stdio_transport()
            return

        if not self._ws_route_registered:
            ws_path = self._acp_config.ws_path

            @self._unified_parent_app.websocket(ws_path)
            async def _acp_ws_endpoint(websocket: WebSocket) -> None:
                await self._handle_ws_connection(websocket)

            self._ws_route_registered = True
            logger.info(
                "[ACP] WebSocket route registered at %s (agent_name=%s)",
                ws_path,
                self._acp_config.agent_name,
            )

    async def stop(self) -> None:
        """Stop the ACP channel — cancel all tasks and clean up.

        Iterates over all connections (stdio sentinel + any WebSocket
        clients) and tears down their consumer tasks, event subscriptions,
        and pending permission futures.
        """
        self._running = False

        # Cancel stdin reader (stdio mode)
        if self._stdin_task is not None:
            self._stdin_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stdin_task
            self._stdin_task = None

        # Tear down every connection's consumer tasks and subscriptions.
        event_bus = getattr(self._manager, "_event_bus", None)
        for conn_key, state in list(self._connections.items()):
            await self._cleanup_connection(conn_key, state, event_bus)

        self._connections.clear()

        # Flush stdout (stdio mode)
        await _flush_stdout()

        logger.info("[ACP] Channel stopped")

    async def _cleanup_connection(
        self,
        conn_key: Any,
        state: _ConnectionState,
        event_bus: Any,
    ) -> None:
        """Tear down a single connection's state.

        Cancels consumer tasks, unsubscribes EventBus queues, and resolves
        pending permission futures with cancellation.

        Args:
            conn_key: Connection identifier (WebSocket or `_STDIO_SENTINEL`).
            state: The `_ConnectionState` to clean up.
            event_bus: Daemon EventBus (for unsubscribing event queues).
        """
        for loop_id, task in list(state.consumer_tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            queue = state.event_queues.pop(loop_id, None)
            if queue is not None and event_bus is not None:
                topic = loop_event_topic(loop_id)
                with contextlib.suppress(Exception):
                    await event_bus.unsubscribe(topic, queue)
            # Release the loop's dispatcher queue/worker. Without this every
            # session we ever opened leaks one worker for the daemon's lifetime.
            dispatcher = getattr(self._manager, "_loop_input_dispatcher", None)
            if dispatcher is not None:
                with contextlib.suppress(Exception):
                    await dispatcher.cleanup_loop(loop_id)

        state.consumer_tasks.clear()
        state.session_map.clear()

        # Release anything still waiting on a turn, so a `session/prompt` cannot
        # outlive the connection that asked for it.
        for waiters in state.pending_turns.values():
            for waiter in waiters:
                with contextlib.suppress(asyncio.InvalidStateError):
                    waiter.set_result("cancelled")
        state.pending_turns.clear()

        # Resolve any pending permission futures with a cancellation error
        for fut in list(state.pending_permissions.values()):
            if not fut.done():
                fut.cancel()
        state.pending_permissions.clear()

    async def send(self, chat_id: str, message: ChannelMessage) -> None:
        """Deliver outbound message as ACP `session/update` notification.

        Args:
            chat_id: ACP session_id (maps to loop_id via the session map).
            message: ChannelMessage to deliver.
        """
        session_id = self._loop_to_session(chat_id)
        if session_id is None:
            logger.warning("[ACP] No session for loop_id %s", chat_id)
            return

        block = _make_text_block(message.content)
        await self._send_session_update(session_id, [block])

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Stream incremental text chunk as ACP `session/update`.

        Args:
            chat_id: Loop ID identifying the session.
            delta: Text chunk to stream.
            metadata: Stream metadata (_stream_id, _stream_end, etc.).
        """
        session_id = self._loop_to_session(chat_id)
        if session_id is None:
            return

        block = _make_text_block(delta)
        await self._send_session_update(session_id, [block], metadata=metadata)

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Stream reasoning content as ACP `session/update`.

        Args:
            chat_id: Loop ID identifying the session.
            delta: Reasoning text chunk.
            metadata: Stream metadata.
        """
        if not self.show_reasoning:
            return

        session_id = self._loop_to_session(chat_id)
        if session_id is None:
            return

        block = _make_reasoning_block(delta)
        await self._send_session_update(session_id, [block], metadata=metadata)

    # ------------------------------------------------------------------
    # JSON-RPC transport loops
    # ------------------------------------------------------------------

    async def _read_stdin_loop(self) -> None:
        """Read NDJSON lines from stdin and dispatch JSON-RPC requests.

        Sets the `_current_connection` context var to the stdio sentinel so
        that protocol handlers and the EventBus consumer route output to
        stdout via `_write_jsonrpc`.
        """
        token = _current_connection.set(_STDIO_SENTINEL)
        try:
            while self._running:
                try:
                    line = await asyncio.to_thread(self._read_line)
                    if line is None:
                        # EOF on stdin — client disconnected
                        logger.info("[ACP] stdin EOF, shutting down")
                        self._running = False
                        break
                    line = line.strip()
                    if not line:
                        continue

                    request = json.loads(line)
                    await self._dispatch_request(request)
                except asyncio.CancelledError:
                    raise
                except json.JSONDecodeError as e:
                    await self._write_jsonrpc(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32700,
                                "message": f"Parse error: {e}",
                            },
                        }
                    )
                except Exception:
                    logger.exception("[ACP] Error processing stdin line")
        finally:
            _current_connection.reset(token)

    def _read_line(self) -> str | None:
        """Read one line from stdin (blocking). Returns None on EOF."""
        line = sys.stdin.readline()
        if not line:
            return None
        return line

    async def _handle_ws_connection(self, websocket: WebSocket) -> None:
        """Handle a single WebSocket client connection lifecycle.

        Mirrors `_read_stdin_loop` but reads from
        `websocket.receive_text()` instead of stdin. Each connection gets
        its own `_ConnectionState` with isolated session map, permission
        futures, and event queues. On `WebSocketDisconnect`, the
        connection's state is cleaned up.

        Args:
            websocket: The connected WebSocket instance.
        """
        await websocket.accept()
        # Ensure a _ConnectionState exists for this connection.
        self._get_state(websocket)

        token = _current_connection.set(websocket)
        try:
            while self._running:
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    logger.info("[ACP] WebSocket client disconnected")
                    break

                line = raw.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                    await self._dispatch_request(request)
                except json.JSONDecodeError as e:
                    await self._write_jsonrpc(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32700,
                                "message": f"Parse error: {e}",
                            },
                        },
                        connection=websocket,
                    )
                except Exception:
                    logger.exception("[ACP] Error processing WebSocket message")
        finally:
            _current_connection.reset(token)
            # Clean up this connection's state.
            event_bus = getattr(self._manager, "_event_bus", None)
            state = self._connections.pop(websocket, None)
            if state is not None:
                await self._cleanup_connection(websocket, state, event_bus)

    async def _dispatch_request(self, request: dict[str, Any]) -> None:
        """Dispatch a JSON-RPC 2.0 request to the appropriate handler.

        Handles both inbound requests (from the ACP client) and responses
        to outbound requests (e.g., `session/request_permission` responses).

        Args:
            request: Parsed JSON-RPC request dict.
        """
        # If this is a response to a pending permission request, resolve it.
        req_id = request.get("id")
        if "method" not in request and req_id is not None:
            await self._handle_response(request)
            return

        method = request.get("method", "")
        params = request.get("params", {})

        handlers = {
            "initialize": self._handle_initialize,
            "session/new": self._handle_session_new,
            "session/prompt": self._handle_session_prompt,
            "session/cancel": self._handle_session_cancel,
            "session/load": self._handle_session_load,
            "session/list": self._handle_session_list,
            "session/delete": self._handle_session_delete,
            "session/fork": self._handle_session_fork,
            "session/resume": self._handle_session_resume,
            "session/close": self._handle_session_close,
            "session/set_mode": self._handle_session_set_mode,
            "session/set_config_option": self._handle_session_set_config_option,
            "authenticate": self._handle_authenticate,
            "providers/list": self._handle_providers_list,
            "providers/set": self._handle_providers_set,
            "providers/disable": self._handle_providers_disable,
            "logout": self._handle_logout,
            "mcp/message": self._handle_mcp_message,
            "nes/start": self._handle_nes_start,
            "nes/suggest": self._handle_nes_suggest,
            "nes/accept": self._handle_nes_accept,
            "nes/reject": self._handle_nes_reject,
            "nes/close": self._handle_nes_close,
            "document/didOpen": self._handle_document_did_open,
            "document/didChange": self._handle_document_did_change,
            "document/didClose": self._handle_document_did_close,
            "document/didSave": self._handle_document_did_save,
            "document/didFocus": self._handle_document_did_focus,
        }

        handler = handlers.get(method)
        if handler is None:
            await self._write_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )
            return

        try:
            result = await handler(params)
            await self._write_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result,
                }
            )
        except ValueError as e:
            # ValueError from session validation = invalid params (-32602)
            logger.warning("[ACP] Invalid params for %s: %s", method, e)
            await self._write_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": _ERR_INVALID_PARAMS,
                        "message": str(e),
                    },
                }
            )
        except Exception as e:
            logger.exception("[ACP] Handler error for %s", method)
            await self._write_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": _ERR_INTERNAL,
                        "message": f"Internal error: {e}",
                    },
                }
            )

    async def _handle_response(self, response: dict[str, Any]) -> None:
        """Handle a JSON-RPC response to an outbound request.

        Resolves the pending future for the corresponding request ID.
        Used for `session/request_permission` responses from the ACP client.

        Args:
            response: Parsed JSON-RPC response dict with `id`, `result` or `error`.
        """
        req_id = response.get("id")
        if not isinstance(req_id, int):
            logger.warning("[ACP] Response with non-integer id: %s", req_id)
            return

        fut = self._get_state(_current_connection.get()).pending_permissions.pop(req_id, None)
        if fut is None:
            logger.warning("[ACP] No pending permission for request id %s", req_id)
            return

        if "error" in response:
            fut.set_result({"outcome": "cancelled"})
        else:
            result = response.get("result", {})
            fut.set_result(result if isinstance(result, dict) else {"outcome": "cancelled"})

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `initialize` — return server capabilities and agent info.

        Advertises full ACP capabilities: session lifecycle (list, delete,
        fork, resume, close), modes, config options, auth/logout, providers,
        nes, and MCP. Soothe uses its own workspace tools — client fs/terminal
        capabilities are NOT advertised (editors handle those locally).
        """
        return {
            "protocolVersion": _ACP_PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                "mcpCapabilities": {
                    "http": False,
                    "sse": False,
                    "acp": False,
                },
                "sessionCapabilities": {
                    "list": {},
                    "delete": {},
                    "fork": {},
                    "resume": {},
                    "close": {},
                },
                "auth": {
                    "logout": {},
                },
                "providers": {},
                "nes": {},
                "positionEncoding": "utf-16",
            },
            "agentInfo": {
                "name": self._acp_config.agent_name,
                "version": "1.0.0",
                "description": self._acp_config.agent_description,
            },
        }

    async def _subscribe_loop_events(self, loop_id: str) -> None:
        """Subscribe to the loop's EventBus topic and start consumer task.

        This must be called **before** ``handle_inbound`` to avoid the
        publish-before-subscribe race where the first ``ChannelMessageReceived``
        event is dropped (see daemon log ``No subscribers for topic …``).

        Args:
            loop_id: Daemon loop identifier to subscribe to.
        """
        state = self._get_state(_current_connection.get())
        # Skip if already subscribed (e.g. session/load on existing session)
        if loop_id in state.event_queues:
            return

        event_bus = getattr(self._manager, "_event_bus", None)
        if event_bus is None:
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        state.event_queues[loop_id] = queue
        topic = loop_event_topic(loop_id)
        await event_bus.subscribe(topic, queue)

        consumer = asyncio.create_task(self._consume_loop_events(loop_id, queue))
        state.consumer_tasks[loop_id] = consumer

    async def _handle_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/new` — create a daemon loop and subscribe to events.

        Args:
            params: ACP session/new params (may contain `model`, `cwd`).

        Returns:
            ACP session info with session_id, modes, and config options.
        """
        session_id = str(uuid.uuid4())
        cwd = params.get("cwd", "/tmp")

        # Pre-create the loop_id so we can subscribe to its topic before anything
        # can publish to it.
        loop_id = self._manager.ensure_loop_id("acp", session_id)

        state = self._get_state(_current_connection.get())
        state.session_map[session_id] = loop_id
        state.session_states[session_id] = _SessionState(cwd=cwd)

        # Subscribe to the loop's EventBus topic before anything publishes.
        await self._subscribe_loop_events(loop_id)

        # Register the loop and persist the ACP cwd as its workspace (the
        # runner's cwd). Nothing consumes a ChannelMessageReceived event, and an
        # unregistered loop makes `bind_execution_thread_for_loop` fail silently.
        await self._manager.ensure_loop_registered(loop_id, workspace=cwd)

        logger.info("[ACP] session/new: session=%s → loop=%s, cwd=%s", session_id, loop_id, cwd)

        return {
            "sessionId": session_id,
            "modes": {
                "currentModeId": "default",
                "availableModes": [
                    {
                        "id": "default",
                        "name": "Default",
                    },
                ],
            },
            "configOptions": [],
        }

    def _require_session(self, params: dict[str, Any]) -> tuple[str, str]:
        """Validate that a session exists and return (session_id, loop_id).

        Raises ValueError (mapped to JSON-RPC -32602 by the dispatcher)
        if the session is not found in the current connection's session map.

        Args:
            params: ACP request params containing `sessionId`.

        Returns:
            Tuple of (session_id, loop_id).

        Raises:
            ValueError: If sessionId is missing or not found.
        """
        session_id = params.get("sessionId", "")
        if not session_id:
            raise ValueError("Missing required parameter: sessionId")
        state = self._get_state(_current_connection.get())
        loop_id = state.session_map.get(session_id)
        if loop_id is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session_id, loop_id

    async def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/prompt` — enqueue a user turn.

        Args:
            params: ACP session/prompt params with `sessionId` and `prompt`.
        """
        session_id, loop_id = self._require_session(params)
        prompt_text = ""

        # Extract text from prompt (ACP uses a list of content parts)
        prompt_parts = params.get("prompt", [])
        if isinstance(prompt_parts, str):
            prompt_text = prompt_parts
        elif isinstance(prompt_parts, list):
            for part in prompt_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    prompt_text += part.get("text", "")
                elif isinstance(part, str):
                    prompt_text += part

        # ACP requires the prompt response to be sent when the turn *ends*, so
        # register interest before submitting — the loop can go idle before we
        # get back here.
        state = self._get_state(_current_connection.get())
        waiters = state.pending_turns.setdefault(loop_id, [])
        completion: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiters.append(completion)

        try:
            # The loop-native path runs the turn; handle_inbound only publishes
            # an event nothing consumes.
            await self._manager.submit_loop_input(
                loop_id,
                prompt_text,
                channel="acp",
                chat_id=session_id,
            )
            stop_reason = await asyncio.wait_for(
                completion,
                timeout=max(1.0, float(self.config.session_timeout_seconds)),
            )
        except TimeoutError:
            logger.warning(
                "[ACP] session/prompt gave up after %ss waiting for loop %s to go idle",
                self.config.session_timeout_seconds,
                loop_id,
            )
            stop_reason = "end_turn"
        finally:
            with contextlib.suppress(ValueError):
                waiters.remove(completion)
            if not waiters:
                state.pending_turns.pop(loop_id, None)

        logger.debug(
            "[ACP] session/prompt: session=%s, loop=%s, stopReason=%s",
            session_id,
            loop_id,
            stop_reason,
        )
        return {
            "stopReason": stop_reason,
            "usage": {
                "totalTokens": 0,
                "inputTokens": 0,
                "outputTokens": 0,
            },
        }

    async def _handle_session_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/cancel` — abort the running turn on this loop.

        Releases the waiting `session/prompt` with the `cancelled` stop reason
        and enqueues a `/cancel` command through the dispatcher.

        Args:
            params: ACP session/cancel params with `sessionId`.
        """
        session_id, loop_id = self._require_session(params)
        state = self._get_state(_current_connection.get())

        # The oldest waiter owns the turn that is actually running.
        waiters = state.pending_turns.get(loop_id)
        if waiters:
            with contextlib.suppress(asyncio.InvalidStateError):
                waiters[0].set_result("cancelled")

        # The loop's cancel entry point is a `/cancel` command through the
        # dispatcher; a `command: cancel` EventBus event reaches nothing.
        dispatcher = getattr(self._manager, "_loop_input_dispatcher", None)
        if dispatcher is not None:
            await dispatcher.enqueue(
                loop_id,
                {"type": "command", "cmd": "/cancel", "client_id": None},
            )

        logger.info("[ACP] session/cancel: session=%s, loop=%s", session_id, loop_id)
        return {}

    async def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/load` — resume an existing session.

        Re-subscribes to the loop's EventBus topic so output events are
        translated to ACP `session/update` notifications. If the session
        is not in the current connection's map (e.g. loaded from a prior
        daemon run), a new loop is created.

        Args:
            params: ACP session/load params with `sessionId` and `cwd`.

        Returns:
            ACP load-session response with modes and config options.
        """
        session_id = params.get("sessionId", "")
        cwd = params.get("cwd", "/tmp")
        state = self._get_state(_current_connection.get())

        # If the session already exists in this connection, re-subscribe.
        loop_id = state.session_map.get(session_id)
        if loop_id is None:
            # Session not in current connection — pre-create loop_id,
            # subscribe, THEN publish via handle_inbound (race-safe).
            loop_id = self._manager.ensure_loop_id("acp", session_id)
            state.session_map[session_id] = loop_id

            # Track session state
            if session_id not in state.session_states:
                state.session_states[session_id] = _SessionState(cwd=cwd)

            # Subscribe to the loop's EventBus topic BEFORE handle_inbound publishes.
            await self._subscribe_loop_events(loop_id)

            # Now publish the ChannelMessageReceived event (subscribers are ready).
            await self._manager.handle_inbound(
                channel="acp",
                chat_id=session_id,
                sender_id="acp-client",
                content="",
                metadata={"resume": True, "cwd": cwd},
            )

        logger.info("[ACP] session/load: session=%s → loop=%s", session_id, loop_id)
        return {
            "modes": {
                "currentModeId": "default",
                "availableModes": [
                    {
                        "id": "default",
                        "name": "Default",
                    },
                ],
            },
            "configOptions": [],
        }

    # ------------------------------------------------------------------
    # Session lifecycle methods
    # ------------------------------------------------------------------

    async def _handle_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/list` — list sessions for the current connection.

        Returns all sessions tracked in the current connection's session map,
        including the real cwd each session was created with. Pagination via
        cursor is supported but not needed (all sessions returned in a single
        page).

        Args:
            params: ACP session/list params (optional `cwd`, `cursor`).

        Returns:
            List of session info dicts and optional next cursor.
        """
        state = self._get_state(_current_connection.get())
        sessions: list[dict[str, Any]] = []
        for session_id, loop_id in state.session_map.items():
            ss = state.session_states.get(session_id)
            cwd = ss.cwd if ss is not None else "/tmp"
            sessions.append(
                {
                    "sessionId": session_id,
                    "cwd": cwd,
                }
            )
        return {
            "sessions": sessions,
            "nextCursor": None,
        }

    async def _handle_session_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/delete` — remove a session and clean up resources.

        Cancels the consumer task, unsubscribes from EventBus, and removes
        the session from the connection's session map and session state.

        Args:
            params: ACP session/delete params with `sessionId`.

        Returns:
            Empty response on success.
        """
        session_id, loop_id = self._require_session(params)
        state = self._get_state(_current_connection.get())
        state.session_map.pop(session_id, None)
        state.session_states.pop(session_id, None)

        if loop_id is not None:
            # Cancel consumer task
            task = state.consumer_tasks.pop(loop_id, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            # Unsubscribe from EventBus
            queue = state.event_queues.pop(loop_id, None)
            if queue is not None:
                event_bus = getattr(self._manager, "_event_bus", None)
                if event_bus is not None:
                    topic = loop_event_topic(loop_id)
                    with contextlib.suppress(Exception):
                        await event_bus.unsubscribe(topic, queue)

        logger.info("[ACP] session/delete: session=%s", session_id)
        return {}

    async def _handle_session_fork(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/fork` — create a new session from an existing one.

        Creates a new daemon loop and returns a new session ID. The new
        session inherits the parent session's cwd.

        Args:
            params: ACP session/fork params with `sessionId` and `cwd`.

        Returns:
            New session info with modes and config options.
        """
        parent_session_id, _parent_loop_id = self._require_session(params)
        new_session_id = str(uuid.uuid4())
        cwd = params.get("cwd", "/tmp")

        # Inherit cwd from parent session if available
        state = self._get_state(_current_connection.get())
        parent_ss = state.session_states.get(parent_session_id)
        if parent_ss is not None and cwd == "/tmp":
            cwd = parent_ss.cwd

        # Pre-create loop_id, subscribe, THEN publish (race-safe).
        loop_id = self._manager.ensure_loop_id("acp", new_session_id)
        state.session_map[new_session_id] = loop_id
        state.session_states[new_session_id] = _SessionState(cwd=cwd)

        # Subscribe to the loop's EventBus topic BEFORE handle_inbound publishes.
        await self._subscribe_loop_events(loop_id)

        # Now publish the ChannelMessageReceived event (subscribers are ready).
        await self._manager.handle_inbound(
            channel="acp",
            chat_id=new_session_id,
            sender_id="acp-client",
            content="",
            metadata={"fork_from": parent_session_id, "cwd": cwd},
        )

        logger.info(
            "[ACP] session/fork: parent=%s → new=%s, loop=%s",
            parent_session_id,
            new_session_id,
            loop_id,
        )
        return {
            "sessionId": new_session_id,
            "modes": {
                "currentModeId": "default",
                "availableModes": [{"id": "default", "name": "Default"}],
            },
            "configOptions": [],
        }

    async def _handle_session_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/resume` — resume a previously closed session.

        Similar to `session/load` but specifically for sessions that were
        closed via `session/close`. Re-creates the loop and re-subscribes
        to EventBus events. The session state (cwd, mode, config options)
        is preserved if the session was closed (not deleted).

        Args:
            params: ACP session/resume params with `sessionId` and `cwd`.

        Returns:
            Session info with modes and config options.
        """
        session_id = params.get("sessionId", "")
        cwd = params.get("cwd", "/tmp")
        state = self._get_state(_current_connection.get())

        # session/resume is for sessions that were closed (still in map)
        # or for sessions that need to be re-created. Validate that the
        # session exists in the connection's map.
        if session_id not in state.session_map:
            raise ValueError(f"Unknown session: {session_id}")

        # Pre-create loop_id, subscribe, THEN publish (race-safe).
        loop_id = self._manager.ensure_loop_id("acp", session_id)
        state.session_map[session_id] = loop_id

        # Preserve or create session state
        if session_id not in state.session_states:
            state.session_states[session_id] = _SessionState(cwd=cwd)

        # Subscribe to the loop's EventBus topic BEFORE handle_inbound publishes.
        await self._subscribe_loop_events(loop_id)

        # Now publish the ChannelMessageReceived event (subscribers are ready).
        await self._manager.handle_inbound(
            channel="acp",
            chat_id=session_id,
            sender_id="acp-client",
            content="",
            metadata={"resume": True, "cwd": cwd},
        )

        logger.info("[ACP] session/resume: session=%s → loop=%s", session_id, loop_id)
        return {
            "modes": {
                "currentModeId": "default",
                "availableModes": [{"id": "default", "name": "Default"}],
            },
            "configOptions": [],
        }

    async def _handle_session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/close` — close a session without deleting it.

        Unlike `session/delete`, the session remains resumable via
        `session/resume`. The consumer task is cancelled and EventBus
        subscription removed, but the session ID and session state
        (cwd, mode, config options) are retained in the map.

        Args:
            params: ACP session/close params with `sessionId`.

        Returns:
            Empty response on success.
        """
        session_id, loop_id = self._require_session(params)
        state = self._get_state(_current_connection.get())

        if loop_id is not None:
            # Cancel consumer task but keep session in map for resume
            task = state.consumer_tasks.pop(loop_id, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            # Unsubscribe from EventBus
            queue = state.event_queues.pop(loop_id, None)
            if queue is not None:
                event_bus = getattr(self._manager, "_event_bus", None)
                if event_bus is not None:
                    topic = loop_event_topic(loop_id)
                    with contextlib.suppress(Exception):
                        await event_bus.unsubscribe(topic, queue)

        logger.info("[ACP] session/close: session=%s", session_id)
        return {}

    async def _handle_session_set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/set_mode` — set the active mode for a session.

        Soothe currently supports a single "default" mode. The mode change
        is tracked in the session state and logged.

        Args:
            params: ACP session/set_mode params with `sessionId` and `modeId`.

        Returns:
            Empty response on success.
        """
        session_id, _loop_id = self._require_session(params)
        mode_id = params.get("modeId", "default")
        state = self._get_state(_current_connection.get())
        ss = state.session_states.get(session_id)
        if ss is not None:
            ss.current_mode = mode_id
        logger.info("[ACP] session/set_mode: session=%s, mode=%s", session_id, mode_id)
        return {}

    async def _handle_session_set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/set_config_option` — set a config option for a session.

        Soothe does not expose configurable session options via ACP yet.
        The request is accepted, the config option value is tracked in
        session state, and the current config options list is returned.

        Args:
            params: ACP session/set_config_option params with `sessionId`,
                `configId`, and `value`.

        Returns:
            Response with current config options list.
        """
        session_id, _loop_id = self._require_session(params)
        config_id = params.get("configId", "")
        value = params.get("value")
        state = self._get_state(_current_connection.get())
        ss = state.session_states.get(session_id)
        if ss is not None and config_id:
            ss.config_options[config_id] = value
        logger.info(
            "[ACP] session/set_config_option: session=%s, config=%s, value=%s",
            session_id,
            config_id,
            value,
        )
        # Return the tracked config options as ACP schema expects
        config_options_list = []
        if ss is not None:
            for cid, val in ss.config_options.items():
                config_options_list.append(
                    {
                        "configId": cid,
                        "value": val,
                        "type": "select",
                    }
                )
        return {
            "configOptions": config_options_list,
        }

    # ------------------------------------------------------------------
    # Authentication and providers
    # ------------------------------------------------------------------

    async def _handle_authenticate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `authenticate` — authenticate with a provider.

        Soothe manages API keys via its own configuration system. This method
        accepts the authentication request and returns an empty response
        (no additional data needed for the ACP client).

        Args:
            params: ACP authenticate params with `methodId`.

        Returns:
            Empty response on success.
        """
        method_id = params.get("methodId", "")
        logger.info("[ACP] authenticate: method=%s", method_id)
        return {}

    async def _handle_providers_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `providers/list` — list configured model providers.

        Returns the providers configured in the daemon's model config. The
        default provider is always returned; its models list contains the
        configured default model if set.

        Args:
            params: ACP providers/list params (empty).

        Returns:
            List of provider info dicts.
        """
        default_model = self._acp_config.default_model
        if default_model:
            providers: list[dict[str, Any]] = [
                {
                    "providerId": "soothe",
                    "name": "Soothe Default",
                    "models": [default_model],
                }
            ]
        else:
            providers = [
                {
                    "providerId": "soothe",
                    "name": "Soothe Default",
                    "models": [],
                }
            ]
        return {
            "providers": providers,
        }

    async def _handle_providers_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `providers/set` — set or update a model provider.

        Accepts provider configuration (API type, base URL, headers) and
        stores it. The actual provider configuration is managed by the
        daemon's model config system.

        Args:
            params: ACP providers/set params with `providerId`, `apiType`,
                `baseUrl`, and optional `headers`.

        Returns:
            Empty response on success.
        """
        provider_id = params.get("providerId", "")
        api_type = params.get("apiType", "")
        base_url = params.get("baseUrl", "")
        logger.info(
            "[ACP] providers/set: provider=%s, apiType=%s, baseUrl=%s",
            provider_id,
            api_type,
            base_url,
        )
        return {}

    async def _handle_providers_disable(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `providers/disable` — disable a model provider.

        Args:
            params: ACP providers/disable params with `providerId`.

        Returns:
            Empty response on success.
        """
        provider_id = params.get("providerId", "")
        logger.info("[ACP] providers/disable: provider=%s", provider_id)
        return {}

    async def _handle_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `logout` — log out and clear authentication state.

        Args:
            params: ACP logout params (empty).

        Returns:
            Empty response on success.
        """
        logger.info("[ACP] logout")
        return {}

    # ------------------------------------------------------------------
    # MCP message handling
    # ------------------------------------------------------------------

    async def _handle_mcp_message(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `mcp/message` — forward an MCP message to a connected server.

        Soothe does not expose MCP servers via ACP. The message is
        accepted and an empty result returned. When MCP server support is
        added, this will forward the message to the appropriate MCP connection.

        Args:
            params: ACP mcp/message params with `connectionId`, `method`,
                and optional `params`.

        Returns:
            Empty result dict.
        """
        connection_id = params.get("connectionId", "")
        method = params.get("method", "")
        logger.info("[ACP] mcp/message: connection=%s, method=%s", connection_id, method)
        return {}

    # ------------------------------------------------------------------
    # NES (Neural Engine Suggestions) methods
    # ------------------------------------------------------------------

    async def _handle_nes_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `nes/start` — start a NES session for code suggestions.

        Creates a new NES session ID, tracks it in the connection state,
        and returns it. The session is associated with the connection so
        that subsequent nes/suggest, nes/accept, nes/reject, and nes/close
        calls can validate the session.

        Args:
            params: ACP nes/start params (optional `workspaceUri`,
                `workspaceFolders`, `repository`).

        Returns:
            NES session info with `sessionId`.
        """
        nes_session_id = str(uuid.uuid4())
        workspace_uri = params.get("workspaceUri", "")
        state = self._get_state(_current_connection.get())
        state.nes_sessions[nes_session_id] = {
            "workspaceUri": workspace_uri,
            "suggestions": {},
        }
        logger.info(
            "[ACP] nes/start: session=%s, workspace=%s",
            nes_session_id,
            workspace_uri,
        )
        return {
            "sessionId": nes_session_id,
        }

    async def _handle_nes_suggest(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `nes/suggest` — request code suggestions at a position.

        Returns an empty suggestions list. The NES engine integration is
        a future enhancement; the session is validated against tracked
        NES sessions.

        Args:
            params: ACP nes/suggest params with `sessionId`, `uri`,
                `version`, `position`, `triggerKind`.

        Returns:
            Response with empty suggestions list.
        """
        session_id = params.get("sessionId", "")
        state = self._get_state(_current_connection.get())
        if session_id not in state.nes_sessions:
            raise ValueError(f"Unknown NES session: {session_id}")
        logger.info("[ACP] nes/suggest: session=%s", session_id)
        return {
            "suggestions": [],
        }

    async def _handle_nes_accept(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `nes/accept` — accept a NES suggestion (notification).

        This is a notification (no response expected), but the dispatcher
        wraps it in a result. The acceptance is logged and the suggestion
        is marked as accepted in the NES session state.

        Args:
            params: ACP nes/accept params with `sessionId` and `id`.

        Returns:
            Empty response.
        """
        session_id = params.get("sessionId", "")
        suggestion_id = params.get("id", "")
        state = self._get_state(_current_connection.get())
        nes_ss = state.nes_sessions.get(session_id)
        if nes_ss is None:
            raise ValueError(f"Unknown NES session: {session_id}")
        if suggestion_id:
            nes_ss["suggestions"][suggestion_id] = "accepted"
        logger.info("[ACP] nes/accept: session=%s, suggestion=%s", session_id, suggestion_id)
        return {}

    async def _handle_nes_reject(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `nes/reject` — reject a NES suggestion (notification).

        This is a notification (no response expected), but the dispatcher
        wraps it in a result. The rejection is logged and the suggestion
        is marked as rejected in the NES session state.

        Args:
            params: ACP nes/reject params with `sessionId` and `id`.

        Returns:
            Empty response.
        """
        session_id = params.get("sessionId", "")
        suggestion_id = params.get("id", "")
        state = self._get_state(_current_connection.get())
        nes_ss = state.nes_sessions.get(session_id)
        if nes_ss is None:
            raise ValueError(f"Unknown NES session: {session_id}")
        if suggestion_id:
            nes_ss["suggestions"][suggestion_id] = "rejected"
        logger.info("[ACP] nes/reject: session=%s, suggestion=%s", session_id, suggestion_id)
        return {}

    async def _handle_nes_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `nes/close` — close a NES session.

        Cleans up NES session state by removing it from the tracked
        sessions map.

        Args:
            params: ACP nes/close params with `sessionId`.

        Returns:
            Empty response.
        """
        session_id = params.get("sessionId", "")
        state = self._get_state(_current_connection.get())
        if session_id not in state.nes_sessions:
            raise ValueError(f"Unknown NES session: {session_id}")
        state.nes_sessions.pop(session_id, None)
        logger.info("[ACP] nes/close: session=%s", session_id)
        return {}

    # ------------------------------------------------------------------
    # Document synchronization (LSP-style notifications)
    # ------------------------------------------------------------------

    async def _handle_document_did_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `document/didOpen` — document opened in editor.

        Tracks the document state (URI, language, version, text) for
        context-aware workspace tool calls.

        Args:
            params: ACP document/didOpen params with `sessionId`, `uri`,
                `languageId`, `version`, `text`.

        Returns:
            Empty response.
        """
        session_id, _loop_id = self._require_session(params)
        uri = params.get("uri", "")
        language_id = params.get("languageId", "")
        version = params.get("version", 0)
        text = params.get("text", "")
        state = self._get_state(_current_connection.get())
        ss = state.session_states.get(session_id)
        if ss is not None:
            ss.documents[uri] = {
                "languageId": language_id,
                "version": version,
                "text": text,
            }
        logger.info(
            "[ACP] document/didOpen: session=%s, uri=%s, lang=%s",
            session_id,
            uri,
            language_id,
        )
        return {}

    async def _handle_document_did_change(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `document/didChange` — document changed in editor.

        Updates the tracked document state with content changes. Applies
        full text replacement from `contentChanges[0].text` (the common
        ACP/LSP pattern for full-document sync).

        Args:
            params: ACP document/didChange params with `sessionId`, `uri`,
                `version`, `contentChanges`.

        Returns:
            Empty response.
        """
        session_id, _loop_id = self._require_session(params)
        uri = params.get("uri", "")
        version = params.get("version", 0)
        content_changes = params.get("contentChanges", [])
        state = self._get_state(_current_connection.get())
        ss = state.session_states.get(session_id)
        if ss is not None and uri in ss.documents:
            doc = ss.documents[uri]
            doc["version"] = version
            if content_changes and isinstance(content_changes, list):
                first = content_changes[0]
                if isinstance(first, dict) and "text" in first:
                    doc["text"] = first["text"]
        logger.info(
            "[ACP] document/didChange: session=%s, uri=%s, version=%s", session_id, uri, version
        )
        return {}

    async def _handle_document_did_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `document/didClose` — document closed in editor.

        Removes the document from tracked state.

        Args:
            params: ACP document/didClose params with `sessionId`, `uri`.

        Returns:
            Empty response.
        """
        session_id, _loop_id = self._require_session(params)
        uri = params.get("uri", "")
        state = self._get_state(_current_connection.get())
        ss = state.session_states.get(session_id)
        if ss is not None:
            ss.documents.pop(uri, None)
            ss.focused_uri.discard(uri)
        logger.info("[ACP] document/didClose: session=%s, uri=%s", session_id, uri)
        return {}

    async def _handle_document_did_save(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `document/didSave` — document saved in editor.

        Args:
            params: ACP document/didSave params with `sessionId`, `uri`.

        Returns:
            Empty response.
        """
        session_id, _loop_id = self._require_session(params)
        uri = params.get("uri", "")
        logger.info("[ACP] document/didSave: session=%s, uri=%s", session_id, uri)
        return {}

    async def _handle_document_did_focus(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `document/didFocus` — document focused in editor.

        Updates the active/focused document for context-aware operations.

        Args:
            params: ACP document/didFocus params with `sessionId`, `uri`,
                `version`, `position`, `visibleRange`.

        Returns:
            Empty response.
        """
        session_id, _loop_id = self._require_session(params)
        uri = params.get("uri", "")
        state = self._get_state(_current_connection.get())
        ss = state.session_states.get(session_id)
        if ss is not None:
            ss.focused_uri.add(uri)
        logger.info("[ACP] document/didFocus: session=%s, uri=%s", session_id, uri)
        return {}

    async def _consume_loop_events(
        self,
        loop_id: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        """Drain EventBus events and translate to ACP `session/update`.

        Also detects tool-approval interrupts (`__interrupt__` with
        `action_requests`) and bridges them to ACP
        `session/request_permission` requests.

        The connection that owns `loop_id` is resolved on each iteration so
        that output is routed to the correct transport endpoint (stdout or
        WebSocket) via the `_current_connection` context var.

        Args:
            loop_id: Daemon loop identifier.
            queue: EventBus subscription queue for this loop.
        """
        while self._running:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue

            # EventBus delivers 2-tuples (event_dict, event_meta) or just event_dict
            if isinstance(item, tuple) and len(item) == 2:
                event = item[0]
            elif isinstance(item, dict):
                event = item
            else:
                continue

            # Resolve the connection that owns this loop_id and set the context
            # var so _write_jsonrpc routes output to the correct endpoint.
            conn_key = self._find_connection_for_loop(loop_id)
            if conn_key is None:
                continue
            _current_connection.set(conn_key)

            session_id = self._loop_to_session(loop_id)
            if not session_id:
                continue

            # Check for tool-approval interrupt (permission bridge)
            if self._is_tool_approval_event(event):
                await self._bridge_permission_request(session_id, loop_id, event)
                continue

            # A coalescer step that yields several frames arrives wrapped;
            # translate each member so batched text is not dropped.
            for frame in _iter_wire_frames(event):
                if _is_idle_status(frame):
                    self._resolve_pending_turn(conn_key, loop_id, "end_turn")
                # Plan frames are whole `session/update` bodies, not content
                # blocks, so they bypass the block fan-out below.
                plan_update = self._apply_plan_event(conn_key, loop_id, frame)
                if plan_update is not None:
                    await self._send_plan_update(session_id, plan_update)
                    continue
                blocks = self._translate_event(frame)
                if blocks:
                    await self._send_session_update(session_id, blocks)

    def _resolve_pending_turn(self, conn_key: Any, loop_id: str, stop_reason: str) -> None:
        """Release the oldest `session/prompt` waiting on ``loop_id``."""
        state = self._connections.get(conn_key)
        if state is None:
            return
        waiters = state.pending_turns.get(loop_id)
        if not waiters:
            return
        oldest = waiters.pop(0)
        if not waiters:
            state.pending_turns.pop(loop_id, None)
        with contextlib.suppress(asyncio.InvalidStateError):
            oldest.set_result(stop_reason)

    def _is_tool_approval_event(self, event: dict[str, Any]) -> bool:
        """Check if an EventBus wire event contains a tool-approval interrupt.

        Tool-approval interrupts arrive as `updates` mode stream tuples with
        `__interrupt__` key containing `action_requests`.

        Args:
            event: Wire-format event dict from EventBus.

        Returns:
            True if the event contains a tool-approval interrupt.
        """
        # The wire event may be a broadcast message with type "event" and data
        # containing the stream tuple, or it may be the raw stream tuple itself.
        data = event.get("data", event)
        if not isinstance(data, dict):
            return False

        # Check for __interrupt__ key in updates data
        if "__interrupt__" not in data:
            # Also check nested data structures
            inner = data.get("data", {})
            if isinstance(inner, dict) and "__interrupt__" in inner:
                data = inner
            else:
                return False

        interrupt_data = data.get("__interrupt__")
        if not isinstance(interrupt_data, dict):
            return False

        # Check for action_requests (deepagents tool-approval interrupt shape)
        return "action_requests" in interrupt_data

    async def _bridge_permission_request(
        self,
        session_id: str,
        loop_id: str,
        event: dict[str, Any],
    ) -> None:
        """Bridge a tool-approval interrupt to ACP `session/request_permission`.

        Sends a `session/request_permission` request to the ACP client and
        awaits the response. The response determines whether the tool call
        is approved or denied. The decision is routed back to the daemon
        via the loop input path to resume the interrupted graph.

        Args:
            session_id: ACP session identifier.
            loop_id: Daemon loop identifier.
            event: Wire-format event dict containing the interrupt.
        """
        # Extract action_requests from the interrupt
        data = event.get("data", event)
        if not isinstance(data, dict):
            return
        interrupt_data = data.get("__interrupt__", {})
        if not isinstance(interrupt_data, dict):
            return

        action_requests = interrupt_data.get("action_requests", [])
        if not isinstance(action_requests, list) or not action_requests:
            return

        # Get the interrupt_id for resume routing
        interrupt_id = interrupt_data.get("interrupt_id", "")
        if not interrupt_id:
            # Try to get it from the event metadata
            interrupt_id = event.get("interrupt_id", str(uuid.uuid4()))

        # Build ACP permission options
        options = [
            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
            {"optionId": "reject_once", "name": "Deny once", "kind": "reject_once"},
            {"optionId": "reject_always", "name": "Always deny", "kind": "reject_always"},
        ]

        # Build tool_call update for each action request
        for ar in action_requests:
            if not isinstance(ar, dict):
                continue

            tool_call_id = ar.get("tool_call_id", str(uuid.uuid4()))
            tool_name = ar.get("tool_name", "unknown")
            tool_args = ar.get("args", {})

            # Build the ToolCallUpdate for the permission request
            tool_call_update = {
                "toolCallId": tool_call_id,
                "title": f"Tool call: {tool_name}",
                "rawInput": {"tool": tool_name, "args": tool_args},
            }

            # Send session/request_permission and await response
            req_id = self._next_request_id
            self._next_request_id += 1

            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._get_state(_current_connection.get()).pending_permissions[req_id] = fut

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": tool_call_update,
                    "options": options,
                },
            }

            logger.info(
                "[ACP] Permission request: session=%s, tool=%s, loop=%s",
                session_id,
                tool_name,
                loop_id,
            )

            await self._write_jsonrpc(request)

            try:
                response = await asyncio.wait_for(fut, timeout=_PERMISSION_TIMEOUT_S)
            except TimeoutError:
                logger.warning(
                    "[ACP] Permission request timed out for session=%s, tool=%s",
                    session_id,
                    tool_name,
                )
                response = {"outcome": "cancelled"}
            except asyncio.CancelledError:
                logger.info("[ACP] Permission request cancelled for session=%s", session_id)
                return

            # Route the response back to the daemon to resume the interrupted graph
            await self._route_permission_response(
                session_id,
                loop_id,
                interrupt_id,
                response,
                tool_call_id,
            )

    async def _route_permission_response(
        self,
        session_id: str,
        loop_id: str,
        interrupt_id: str,
        response: dict[str, Any],
        tool_call_id: str,
    ) -> None:
        """Route the ACP client's permission response back to the daemon.

        Translates the ACP permission outcome (allowed/denied) into a
        LangGraph resume payload and publishes it on the loop's EventBus topic
        so the StrangeLoop can resume the interrupted graph.

        Args:
            session_id: ACP session identifier.
            loop_id: Daemon loop identifier.
            interrupt_id: LangGraph interrupt ID for resume routing.
            response: ACP permission response dict with `outcome` key.
            tool_call_id: The tool call ID from the action request.
        """
        outcome = response.get("outcome", "cancelled")

        if outcome == "selected":
            # User selected an option — check if it's allow or deny
            option_id = response.get("optionId", "")
            if option_id.startswith("allow"):
                decision = {"type": "approve"}
                logger.info(
                    "[ACP] Permission allowed: session=%s, tool=%s", session_id, tool_call_id
                )
            else:
                decision = {"type": "reject"}
                logger.info(
                    "[ACP] Permission denied: session=%s, tool=%s", session_id, tool_call_id
                )
        else:
            # Cancelled or denied
            decision = {"type": "reject"}
            logger.info("[ACP] Permission cancelled: session=%s, tool=%s", session_id, tool_call_id)

        # Build the resume payload for the deepagents HumanInTheLoopMiddleware
        # The shape is {interrupt_id: {"decisions": [decision]}}
        resume_payload = {
            interrupt_id: {"decisions": [decision]},
        }

        # Publish the resume command on the loop's EventBus topic
        event_bus = getattr(self._manager, "_event_bus", None)
        if event_bus is not None:
            resume_msg = {
                "type": "command",
                "command": "resume",
                "loop_id": loop_id,
                "resume_payload": resume_payload,
            }
            topic = loop_event_topic(loop_id)
            await event_bus.publish(topic, resume_msg)

    def _translate_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Translate a daemon wire event to ACP content blocks.

        Dispatches on ``mode``: ``messages`` frames carry assistant prose;
        ``custom`` frames carry control/UI events.

        Args:
            event: Wire-format event dict from EventBus.

        Returns:
            List of ACP content blocks (may be empty if event is not translatable).
        """
        mode = str(event.get("mode") or "")
        data = event.get("data")

        if mode == "messages":
            return self._translate_messages_frame(event, data)
        if mode == "custom" and isinstance(data, dict):
            return self._translate_custom_frame(data)
        return []

    def _translate_messages_frame(
        self,
        event: dict[str, Any],
        data: Any,
    ) -> list[dict[str, Any]]:
        """Translate one ``mode="messages"`` frame.

        ``data`` is the ``(message, metadata)`` pair the runner stream produced;
        the message is a flat LangChain wire dict the SDK text extractor reads.
        """
        if not isinstance(data, (tuple, list)) or not data:
            return []
        message = data[0]
        if not isinstance(message, dict):
            return []
        if message.get("type") not in _ASSISTANT_MESSAGE_TYPES:
            return []

        # Only loop-tagged finals are user-facing.
        #
        # An untagged AI message is the execute wave's own narration — the step
        # executor working through its task. Forwarding it makes that working
        # output read as the answer: a live run showed the step's report and the
        # goal-completion summary arriving as one concatenated message, because
        # untagged prose outnumbered the real finals 265:157.
        #
        # This mirrors the headless CLI, whose `_suppress_main_assistant_body_*`
        # keeps stdout to loop-tagged finals for the same reason.
        phase = assistant_output_phase(message)
        if phase is None:
            return []

        # Subgraph prose is user-facing only for the goal synthesis.
        namespace = event.get("namespace") or []
        if namespace and phase != "goal_completion":
            return []

        text = "".join(extract_text_from_ai_message(message))
        if not text:
            return []
        return [_make_text_block(text)]

    def _translate_custom_frame(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Translate one ``mode="custom"`` control/UI frame."""
        event_type = data.get("type", "")

        if event_type in (OUTPUT_TEXT_DELTA, OUTPUT_TEXT_COMPLETE):
            content = data.get("content", "")
            return [_make_text_block(content)] if content else []

        if event_type == OUTPUT_TEXT_END:
            # Stream end marker — no content block needed
            return []

        if event_type == OUTPUT_PROGRESS:
            if not self.send_progress:
                return []
            message = data.get("message", "")
            return [_make_progress_block(message)] if message else []

        if event_type == OUTPUT_REASONING:
            if not self.show_reasoning:
                return []
            content = data.get("content", "")
            return [_make_reasoning_block(content)] if content else []

        return []

    # ------------------------------------------------------------------
    # Plan projection
    # ------------------------------------------------------------------

    def _apply_plan_event(
        self,
        conn_key: Any,
        loop_id: str,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Fold a cognition plan event into this loop's plan and serialize it.

        Returns the full `session/update` body to send, or None when ``frame``
        is not a plan event (the caller then falls through to the block path).

        Every update is the complete entry list: ACP requires the client to
        replace its plan wholesale, so partial lists would erase steps.
        """
        if str(frame.get("mode") or "") != "custom":
            return None
        data = frame.get("data")
        if not isinstance(data, dict):
            return None
        event_type = str(data.get("type") or "")

        if event_type == STRANGE_LOOP_PLAN_DECISION:
            state = self._plan_state(conn_key, loop_id)
            raw_steps = data.get("steps")
            state.sync_plan(
                raw_steps if isinstance(raw_steps, list) else [],
                int(data.get("iteration") or 0),
            )
            return state.emit_update()

        if event_type in (STRANGE_LOOP_STEP_STARTED, STRANGE_LOOP_STEP_QUEUED):
            step_id = str(data.get("step_id") or "").strip()
            if not step_id:
                return None
            # `queued` means "ready but not yet dispatched" -> ACP `pending`.
            phase = "running" if event_type == STRANGE_LOOP_STEP_STARTED else "queued"
            state = self._plan_state(conn_key, loop_id)
            state.mark(step_id, phase, str(data.get("description") or ""))
            return state.emit_update()

        if event_type == STRANGE_LOOP_STEP_COMPLETED:
            step_id = str(data.get("step_id") or "").strip()
            if not step_id:
                return None
            state = self._plan_state(conn_key, loop_id)
            # `step.completed` carries no description, so `settle` must not
            # clobber the one already recorded from the plan/started frame.
            state.settle(
                step_id,
                bool(data.get("success", True)),
                summary=data.get("summary") or data.get("output_preview") or "",
                duration_ms=data.get("duration_ms"),
                tool_call_count=data.get("tool_call_count"),
                total_tokens_used=data.get("total_tokens_used"),
            )
            return state.emit_update()

        if event_type == STRANGE_LOOP_COMPLETED:
            state = self._plan_state(conn_key, loop_id)
            if not state.steps:
                return None
            state.close_open_steps()
            return state.emit_update()

        return None

    def _plan_state(self, conn_key: Any, loop_id: str) -> _PlanState:
        """Return (creating if needed) the plan state for this connection+loop."""
        state = self._get_state(conn_key)
        plan = state.plan_states.get(loop_id)
        if plan is None:
            plan = _PlanState()
            state.plan_states[loop_id] = plan
        return plan

    async def _send_plan_update(
        self,
        session_id: str,
        update: dict[str, Any],
    ) -> None:
        """Write a `session/update` whose `update` is a whole plan body.

        Deliberately separate from `_send_session_update`, which takes content
        blocks and fans out one notification per block. A plan is one
        notification carrying an `entries` array, so routing it through the
        block path would not fit.
        """
        await self._write_jsonrpc(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    async def _send_session_update(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write an ACP `session/update` notification to the active transport.

        In stdio mode the notification is written to stdout; in WebSocket
        mode it is sent via `websocket.send_text` to the connection that
        owns the session (determined via `_current_connection` context var).

        Args:
            session_id: ACP session identifier.
            blocks: List of internal content blocks.
            metadata: Optional stream metadata, attached as ACP `_meta`.
        """
        visible_metadata = (
            {k: v for k, v in metadata.items() if not k.startswith("_")} if metadata else None
        )
        # `session/update` carries exactly one SessionUpdate, so a frame holding
        # several blocks becomes several notifications.
        for block in blocks:
            update = _session_update_from_block(block)
            if update is None:
                continue
            if visible_metadata:
                update["_meta"] = visible_metadata
            await self._write_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": update,
                    },
                }
            )

    async def _write_jsonrpc(
        self,
        msg: dict[str, Any],
        *,
        connection: Any = None,
    ) -> None:
        """Serialize dict to JSON and write to the active transport.

        In stdio mode (or when `connection` is the stdio sentinel), writes
        NDJSON to stdout via `asyncio.to_thread`. In WebSocket mode, sends
        the JSON text via `websocket.send_text` to the specified connection
        (or the connection from `_current_connection` if not given).

        Args:
            msg: JSON-RPC message dict.
            connection: Optional explicit connection key (WebSocket or
                `_STDIO_SENTINEL`). If `None`, falls back to the
                `_current_connection` context var, then to stdio.
        """
        # Resolve the connection to route output to.
        conn_key = connection if connection is not None else _current_connection.get()

        if conn_key is not _STDIO_SENTINEL and isinstance(conn_key, WebSocket):
            text = json.dumps(msg) + "\n"
            await conn_key.send_text(text)
        else:
            # Stdio path — write to stdout via a thread to avoid blocking.
            text = json.dumps(msg) + "\n"
            await asyncio.to_thread(_write_stdout, text)

    # ------------------------------------------------------------------
    # Session mapping helpers
    # ------------------------------------------------------------------

    def _loop_to_session(self, loop_id: str) -> str | None:
        """Look up ACP session_id from daemon loop_id.

        Searches the session map of the current connection (determined by
        `_current_connection` context var) so that multi-client WebSocket
        connections have isolated session lookups.

        Args:
            loop_id: Daemon loop identifier.

        Returns:
            ACP session_id, or None if not found.
        """
        state = self._get_state(_current_connection.get())
        for session_id, lid in state.session_map.items():
            if lid == loop_id:
                return session_id
        return None

    def _find_connection_for_loop(self, loop_id: str) -> Any:
        """Find the connection key that owns the given loop_id.

        Used by the EventBus consumer to resolve which connection a loop
        belongs to, so output can be routed to the correct WebSocket client.

        Args:
            loop_id: Daemon loop identifier.

        Returns:
            Connection key (WebSocket or `_STDIO_SENTINEL`), or `None`
            if no connection owns this loop.
        """
        for conn_key, state in self._connections.items():
            if loop_id in state.session_map.values():
                return conn_key
        return None

    @property
    def client_count(self) -> int:
        """Return number of active ACP sessions across all connections."""
        return sum(len(s.session_map) for s in self._connections.values())


# ---------------------------------------------------------------------------
# Module-level helpers (avoid blocking the event loop)
# ---------------------------------------------------------------------------


def _write_stdout(text: str) -> None:
    """Write text to stdout (blocking, called via asyncio.to_thread)."""
    sys.stdout.write(text)
    sys.stdout.flush()


async def _flush_stdout() -> None:
    """Flush stdout via to_thread to avoid blocking."""
    await asyncio.to_thread(sys.stdout.flush)
