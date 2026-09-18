"""Reconcile the relay inbox against LangGraph checkpoint state.

The CoreAgent checkpoint is the source of truth for pending interrupts: the
inbox is a projection. On hydrate (fresh worker, resumed turn), every
thread-bearing inbox entry is verified against its CoreAgent thread's pending
interrupts — stale entries (thread advanced / interrupt resolved elsewhere)
are dropped, and clarification interrupts present in the checkpoint but absent
from the inbox are alerted (lost capture; no auto-recovery).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from soothe.sloop.clarification.interrupt_kinds import (
    is_clarification_interrupt_payload,
)
from soothe.sloop.relay.events import RELAY_RECONCILED
from soothe.sloop.relay.inbox import RelayInbox

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, Any], Awaitable[None]]


async def _thread_pending_interrupts(core_agent: Any, thread_id: str) -> dict[str, Any] | None:
    """Read a CoreAgent thread's pending interrupts as `{interrupt_id: value}`.

    Returns `None` when the state cannot be read (unreadable backend, missing
    thread) — callers treat unreadable as "cannot verify" and keep the entry.
    """
    get_state = getattr(core_agent, "aget_state", None)
    if not callable(get_state):
        return None
    try:
        state = await get_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.debug(
            "[relay] aget_state failed for thread %s during reconcile",
            thread_id[:24],
            exc_info=True,
        )
        return None
    if state is None:
        return None
    interrupts: list[Any] = []
    raw = getattr(state, "interrupts", None)
    if isinstance(raw, (list, tuple)):
        interrupts.extend(raw)
    tasks = getattr(state, "tasks", None)
    if isinstance(tasks, (list, tuple)):
        for task in tasks:
            task_interrupts = getattr(task, "interrupts", None)
            if isinstance(task_interrupts, (list, tuple)):
                interrupts.extend(task_interrupts)
    pending: dict[str, Any] = {}
    for interrupt_obj in interrupts:
        iid = getattr(interrupt_obj, "id", None)
        if iid:
            pending[str(iid)] = getattr(interrupt_obj, "value", None)
    return pending


async def reconcile_inbox_with_checkpoints(
    core_agent: Any,
    inbox: RelayInbox,
    *,
    loop_id: str,
    emit: EmitFn = None,
) -> int:
    """Drop inbox entries whose interrupt is no longer pending on its thread.

    The LangGraph checkpoint is canonical; the inbox is a projection that can
    drift across worker exits. Entries whose `origin_interrupt_id` is absent
    from a *non-empty* pending set on their thread are dropped (a thread with
    no pending interrupts is not verified — it may not be CoreAgent-backed,
    e.g. plan-review or planner-ask origins). Clarification-kind interrupts
    pending on a thread but missing from the inbox are alerted.

    Returns the number of dropped entries.
    """
    if getattr(core_agent, "can_read_graph_state", True) is False:
        return 0

    thread_ids = sorted(
        {entry.resume_ticket.thread_id for entry in inbox if entry.resume_ticket.thread_id}
    )
    if not thread_ids:
        return 0

    pending_by_thread: dict[str, dict[str, Any]] = {}
    for thread_id in thread_ids:
        pending = await _thread_pending_interrupts(core_agent, thread_id)
        if pending is not None:
            pending_by_thread[thread_id] = pending

    dropped = 0
    alerts = 0
    inbox_ids_by_thread: dict[str, set[str]] = {}
    for entry in list(inbox):
        thread_id = entry.resume_ticket.thread_id
        if not thread_id or thread_id not in pending_by_thread:
            continue
        inbox_ids_by_thread.setdefault(thread_id, set()).add(entry.request.origin_interrupt_id)
        pending = pending_by_thread[thread_id]
        if pending and entry.request.origin_interrupt_id not in pending:
            logger.warning(
                "[relay] dropping stale inbox entry: interrupt %s no longer pending "
                "on thread %s (loop=%s)",
                entry.request.origin_interrupt_id[:16],
                thread_id[:24],
                loop_id,
            )
            if inbox.drop(entry):
                dropped += 1

    for thread_id, pending in pending_by_thread.items():
        inbox_ids = inbox_ids_by_thread.get(thread_id, set())
        for iid, value in pending.items():
            if iid in inbox_ids:
                continue
            if is_clarification_interrupt_payload(value):
                alerts += 1
                logger.warning(
                    "[relay] checkpoint has clarification interrupt %s on thread %s "
                    "with no inbox entry (lost capture; loop=%s)",
                    iid[:16],
                    thread_id[:24],
                    loop_id,
                )

    if (dropped or alerts) and emit is not None:
        await emit(
            RELAY_RECONCILED,
            {"loop_id": loop_id, "dropped": dropped, "alerts": alerts},
        )
    return dropped


__all__ = ["reconcile_inbox_with_checkpoints"]
