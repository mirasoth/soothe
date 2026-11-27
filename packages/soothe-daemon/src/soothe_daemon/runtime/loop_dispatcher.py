"""Loop-scoped isolation for the daemon client plane."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from soothe.workspace import resolve_daemon_workspace, resolve_loop_workspace

from soothe_daemon.bootstrap.logging import set_loop_id

logger = logging.getLogger(__name__)


async def bind_execution_thread_for_loop(daemon: Any, loop_id: str) -> str:
    """Bind the loop's checkpoint thread and return its id."""
    # Set loop_id in logging context for full ID in daemon.log
    set_loop_id(loop_id)

    # Check loop exists in DB
    metadata = await daemon._persistence_manager.get_loop_metadata(loop_id)
    if metadata is None:
        msg = f"Loop {loop_id} not found"
        raise RuntimeError(msg)

    # RFC-223 / IG-764: main thread id == loop id. No ``thread_ids`` history is
    # maintained — fork threads (execute-step, synth, intake) use random opaque
    # ids built by ``orchestrator/checkpoint.py`` and are reachable via the
    # shared checkpointer, not indexed in loop metadata. Only normalize the
    # ``current_thread_id`` here.
    thread_id = loop_id
    if str(metadata.get("current_thread_id") or "").strip() != thread_id:
        try:
            await daemon._persistence_manager.update_loop_metadata(
                loop_id,
                current_thread_id=thread_id,
                status="running",
            )
        except Exception as e:
            logger.warning("Failed to update loop metadata for %s: %s", loop_id, e)

    daemon._thread_registry.ensure(thread_id, is_draft=False)

    raw_user = metadata.get("user_id") or metadata.get("user")
    raw_client_ws = metadata.get("client_workspace")
    raw_client_ws_id = metadata.get("client_workspace_id")

    user = str(raw_user).strip() if raw_user else None
    client_ws = str(raw_client_ws).strip() if raw_client_ws else None
    client_ws_id = str(raw_client_ws_id).strip() if raw_client_ws_id else None
    workspace_mapping = metadata.get("workspace_mapping")
    if not isinstance(workspace_mapping, dict):
        workspace_mapping = None

    # Trust persisted current_workspace — already contains the container path
    # from loop_new. Only re-resolve when the field is missing (legacy or corrupt).
    persisted_workspace = metadata.get("current_workspace")
    if persisted_workspace and str(persisted_workspace).strip():
        loop_workspace = Path(str(persisted_workspace).strip())
    else:
        try:
            loop_workspace = resolve_loop_workspace(
                loop_id=loop_id,
                client_workspace=client_ws,
                user_id=user,
                client_workspace_id=client_ws_id,
                workspace_mapping=workspace_mapping,
            )
        except ValueError as e:
            logger.warning(
                "Loop %s: workspace resolution failed (%s); falling back to daemon workspace",
                loop_id,
                e,
            )
            loop_workspace = resolve_daemon_workspace()

    daemon._thread_registry.set_workspace(thread_id, loop_workspace)
    daemon._thread_registry.set_thread_loop(thread_id, loop_id)

    # RFC-221: set_current_thread_id() removed — thread binding is passed via LoopRunRequest
    # and applied inside the per-loop subprocess. The utility _runner singleton is no longer
    # mutated here, eliminating the data race under concurrent loop execution.
    return str(thread_id)


class LoopInputDispatcher:
    """Isolated input/command processing: one queue and worker task per `loop_id`.

    Queued payloads are written to the durable loop-input store before they
    enter the in-memory queue (IG-DAEM-004): a worker deletes the durable row
    when it picks a message up (complete-before-process), so pending rows
    survive both graceful restarts and crashes and are re-enqueued on startup
    by :meth:`recover_pending_inputs`.
    """

    def __init__(self, daemon: Any, *, max_queue_size: int) -> None:
        self._daemon = daemon
        self._max_queue_size = max_queue_size if max_queue_size > 0 else 0
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._shutting_down: bool = False
        # Durable write-ahead store (IG-DAEM-004). None → memory-only fallback.
        self._store: Any | None = None
        # Approximate count of durable rows not yet picked up by a worker
        # (visible to queue monitoring via `total_queued`).
        self._durable_pending = 0

    def _durable_store(self) -> Any | None:
        """Lazily build the durable store; failures degrade to memory-only."""
        if self._store is not None:
            return self._store
        try:
            from soothe_daemon.persistence.loop_input_store import create_loop_input_store

            store = create_loop_input_store(self._daemon._config)
        except Exception:
            logger.warning(
                "Durable loop-input store unavailable; queued input will not survive restarts",
                exc_info=True,
            )
            return None
        self._store = store
        return store

    def total_queued(self) -> int:
        """Approximate sum of pending items across loop queues (for metrics).

        Includes durable rows not yet picked up by a worker (IG-DAEM-004), so
        queue monitoring also sees restart-survivable backlog.
        """
        return sum(q.qsize() for q in self._queues.values()) + self._durable_pending

    async def enqueue(self, loop_id: str, message: dict[str, Any]) -> None:
        """Enqueue a message for the given loop; starts a worker on first use.

        The payload is persisted to the durable store first (IG-DAEM-004) so a
        restart re-enqueues the original payload. Silently drops the message if
        shutdown has already been initiated so that concurrent callers cannot
        create new orphan workers after the snapshot in `shutdown()` has been
        taken. The queue reference is also re-checked after the (unlocked)
        put: if `cleanup_loop`/`shutdown` ran while the message was being
        queued, the message is dropped with a warning instead of silently
        vanishing into an orphaned queue (its durable row stays pending and is
        recovered on the next start).
        """
        async with self._lock:
            if self._shutting_down:
                logger.debug(
                    "LoopInputDispatcher shutting down; dropping message for loop %s",
                    loop_id,
                )
                return

        # IG-DAEM-004: persist before the memory queue so a restart (graceful
        # or crash) can re-enqueue the original payload. Failures degrade to
        # memory-only — the enqueue path must never break.
        durable_id: str | None = None
        store = self._durable_store()
        if store is not None:
            from soothe_daemon.persistence.loop_input_store import serialize_durable_payload

            payload_json = serialize_durable_payload(message)
            if payload_json is None:
                logger.warning(
                    "Loop input for loop %s not persistable (oversized or non-serializable); "
                    "it will be lost if the daemon restarts before pickup",
                    loop_id,
                )
            else:
                durable_id = uuid.uuid4().hex
                try:
                    await store.enqueue(loop_id, durable_id, payload_json)
                except Exception:
                    logger.warning(
                        "Durable persist failed for loop %s input; continuing memory-only",
                        loop_id,
                        exc_info=True,
                    )
                    durable_id = None
                else:
                    self._durable_pending += 1

        queued = dict(message)
        if durable_id is not None:
            queued["__durable_id"] = durable_id
        queue = await self._enqueue_memory(loop_id, queued)
        if queue is None:
            return
        async with self._lock:
            if self._queues.get(loop_id) is not queue:
                logger.warning(
                    "LoopInputDispatcher cleaned up while enqueueing loop %s; "
                    "dropping orphaned message",
                    loop_id,
                )
                return

    async def shutdown(self) -> None:
        """Cancel all loop workers (daemon stop).

        Durable rows for not-yet-picked messages stay on disk; the next daemon
        start re-enqueues them via :meth:`recover_pending_inputs`.
        """
        async with self._lock:
            self._shutting_down = True  # prevent new enqueue() from spawning workers
            workers = list(self._workers.items())
            self._workers.clear()
            self._queues.clear()
        for _, task in workers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._log_surviving_rows()

    async def _log_surviving_rows(self) -> None:
        """Log how many durable inputs will survive this shutdown."""
        store = self._store
        if store is None:
            return
        try:
            rows = await store.pending_rows()
        except Exception:
            logger.debug("Surviving durable-input count failed", exc_info=True)
            return
        if rows:
            logger.info(
                "%d queued loop input(s) persisted; will re-enqueue on next daemon start",
                len(rows),
            )

    async def recover_pending_inputs(self) -> int:
        """Re-enqueue durable payloads that never reached a worker (startup).

        Rows carry their original `__durable_id`, so they are deleted only when
        a worker picks them up — the queue is safe across repeated restarts.
        Returns the number of re-enqueued messages.
        """
        store = self._durable_store()
        if store is None:
            return 0
        try:
            rows = await store.pending_rows()
        except Exception:
            logger.warning("Durable input recovery read failed", exc_info=True)
            return 0
        if not rows:
            return 0
        recovered = 0
        for row in rows:
            loop_id = str(row.get("loop_id") or "").strip()
            message_id = str(row.get("id") or "").strip()
            if not loop_id or not message_id:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except ValueError:
                logger.warning(
                    "Dropping corrupt durable input %s for loop %s", message_id[:8], loop_id
                )
                with contextlib.suppress(Exception):
                    await store.delete(message_id)
                continue
            if not isinstance(payload, dict):
                continue
            payload = dict(payload)
            payload["__durable_id"] = message_id
            await self._enqueue_memory(loop_id, payload)
            recovered += 1
        if recovered:
            logger.info(
                "Recovered %d persisted loop input(s) from previous daemon run",
                recovered,
            )
        return recovered

    async def _enqueue_memory(
        self, loop_id: str, message: dict[str, Any]
    ) -> asyncio.Queue[dict[str, Any]] | None:
        """Put a message into the in-memory queue, spawning the worker if needed.

        Returns the target queue, or None when shutdown has already started.
        """
        async with self._lock:
            if self._shutting_down:
                logger.debug(
                    "LoopInputDispatcher shutting down; dropping message for loop %s",
                    loop_id,
                )
                return None
            if loop_id not in self._queues:
                q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
                    maxsize=self._max_queue_size if self._max_queue_size > 0 else 0
                )
                self._queues[loop_id] = q
                self._workers[loop_id] = asyncio.create_task(self._worker(loop_id, q))
            queue = self._queues[loop_id]
        await queue.put(message)
        return queue

    async def cleanup_loop(self, loop_id: str) -> None:
        """Remove queue and worker for a specific loop (loop deletion)."""
        async with self._lock:
            worker = self._workers.pop(loop_id, None)
            self._queues.pop(loop_id, None)
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        store = self._durable_store()
        if store is not None:
            try:
                deleted = await store.delete_loop(loop_id)
                self._durable_pending = max(0, self._durable_pending - int(deleted))
            except Exception:
                logger.debug("Durable input cleanup failed for loop %s", loop_id, exc_info=True)
        logger.debug("Cleaned up input dispatcher state for loop %s", loop_id)

    async def _worker(self, loop_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        d = self._daemon
        while d._running and not self._shutting_down:
            try:
                msg = await queue.get()
            except asyncio.CancelledError:
                break
            # IG-DAEM-004: the durable row is deleted once a worker owns the
            # message (complete-before-process). A crash after this point
            # loses that single in-flight message; the loop itself is resumed
            # by auto-resume. Re-processing a half-ingested turn would risk a
            # duplicate user message in loop history.
            durable_id = msg.pop("__durable_id", None)
            if durable_id:
                store = self._durable_store()
                if store is not None:
                    with contextlib.suppress(Exception):
                        await store.delete(str(durable_id))
                    self._durable_pending = max(0, self._durable_pending - 1)
            try:
                await d._process_loop_input_message(loop_id, msg)
            except Exception:
                logger.exception("Loop worker failed for loop_id=%s", loop_id)

    async def drain_for_tests(self) -> None:
        """Cancel workers and clear queues (unit/integration tests only)."""
        await self.shutdown()


__all__ = [
    "LoopInputDispatcher",
    "bind_execution_thread_for_loop",
]
