"""Background uploader with backpressure.

Drains pending checkpoints from the state store and pushes them to the
remote backend.  Checkpoints are written to the state DB with
`status='pending_upload'` and their serialized payload **before** the
uploader attempts the push (local durability first — option A: the
object store is the durable source of truth, the state DB stages the
payload until the push lands).  FIFO ordering; backpressure feedback
to the debouncer when the pending count exceeds the threshold.  On
startup, scans for pending rows and re-attempts (crash recovery).

After a SNAPSHOT payload is successfully pushed, checkpoints older
than it are pruned from the remote (compaction garbage collection) —
they are unreachable from recovery, which always anchors on the
latest snapshot.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from soothe.workspace.sync.workspace_sync import CheckpointPayload

if TYPE_CHECKING:
    from soothe.workspace.sync.workspace_sync import WorkspaceSyncBackend

logger = logging.getLogger(__name__)


class PendingCheckpointStore(Protocol):
    """Protocol for querying pending checkpoints from the state store."""

    async def list_pending_checkpoints(self) -> list[dict[str, Any]]:
        """Return pending checkpoints in FIFO order."""
        ...

    async def mark_checkpoint_uploaded(self, checkpoint_id: str) -> None:
        """Mark a checkpoint uploaded and drop its staged payload bytes."""
        ...

    async def update_checkpoint_status(self, checkpoint_id: str, status: str) -> None:
        """Update the status of a checkpoint."""
        ...


def _seq_of(checkpoint_id: str) -> int:
    """Best-effort numeric sequence of a checkpoint ID (0 when unparsable)."""
    seq = checkpoint_id.rsplit("-", 1)[-1]
    if seq.startswith("c") and seq[1:].isdigit():
        return int(seq[1:])
    return 0


class BackgroundUploader:
    """Background task that drains pending checkpoints to the remote backend.

    Args:
        backend: Remote storage backend.
        store: State store for pending checkpoint queries.
        run_id: Run identifier owning the checkpoints (used for
            compaction pruning after snapshot uploads).
        max_pending: Backpressure threshold.  When exceeded, the
            `on_backpressure` callback is invoked.
        poll_interval: Seconds between drain cycles.
        on_backpressure: Optional callback invoked when pending count
            exceeds `max_pending`.  The debouncer uses this to increase
            the debounce window.
    """

    def __init__(
        self,
        *,
        backend: WorkspaceSyncBackend,
        store: PendingCheckpointStore,
        run_id: str,
        max_pending: int = 20,
        poll_interval: float = 2.0,
        on_backpressure: Any = None,
    ) -> None:
        """Initialize the uploader with backend, checkpoint store, and thresholds."""
        self._backend = backend
        self._store = store
        self._run_id = run_id
        self._max_pending = max_pending
        self._poll_interval = poll_interval
        self._on_backpressure = on_backpressure
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._closing = False

    @property
    def is_running(self) -> bool:
        """Return `True` if the upload loop is active."""
        return self._running

    @property
    def is_closing(self) -> bool:
        """Return `True` if the uploader is in shutdown (draining)."""
        return self._closing

    def start(self) -> None:
        """Start the background upload loop."""
        if self._running:
            return
        self._running = True
        self._closing = False
        self._task = asyncio.create_task(self._upload_loop())
        logger.info("Background uploader started: max_pending=%d", self._max_pending)

    async def stop(self) -> None:
        """Stop the uploader after draining pending checkpoints.

        Sets `_closing=True` (no new checkpoints accepted), waits for
        the upload loop to drain the queue, then shuts down.
        """
        if not self._running:
            return
        self._closing = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=30.0)
            except TimeoutError:
                logger.warning("Background uploader drain timed out after 30s")
                if self._task:
                    self._task.cancel()
        self._running = False
        self._closing = False
        logger.info("Background uploader stopped")

    async def _upload_loop(self) -> None:
        """Main upload loop — drain pending checkpoints."""
        while self._running or self._closing:
            try:
                pending = await self._store.list_pending_checkpoints()

                if len(pending) > self._max_pending and self._on_backpressure:
                    self._on_backpressure(len(pending))

                if not pending:
                    if self._closing:
                        break
                    await asyncio.sleep(self._poll_interval)
                    continue

                all_processed = True
                for item in pending:
                    if not self._running and not self._closing:
                        break
                    success = await self._upload_one(item)
                    if not success:
                        all_processed = False

                if self._closing:
                    break

                if not all_processed:
                    await asyncio.sleep(self._poll_interval)

            except Exception:
                logger.exception("Error in background upload loop")
                await asyncio.sleep(self._poll_interval)

    async def _upload_one(self, item: dict[str, Any]) -> bool:
        """Upload a single pending checkpoint.

        Args:
            item: Pending checkpoint dict with `checkpoint_id` and
                `data` keys (the serialized payload bytes).

        Returns:
            `True` if the upload succeeded (or the row is unrecoverable
            and was moved out of the queue), `False` on a retryable
            failure.
        """
        checkpoint_id: str = item["checkpoint_id"]
        data: bytes | None = item.get("data")

        if not data:
            # A pending row without payload bytes can never be pushed.
            # Mark it failed so the drain loop does not spin on it
            # forever; the object store remains authoritative.
            logger.error(
                "Pending checkpoint %s has no payload data — marking failed",
                checkpoint_id,
            )
            try:
                await self._store.update_checkpoint_status(checkpoint_id, "failed")
            except Exception:  # noqa: BLE001
                logger.exception("Failed to mark payload-less checkpoint %s", checkpoint_id)
            return True

        try:
            await self._backend.put_checkpoint(checkpoint_id, data)
        except Exception:
            logger.exception("Failed to upload checkpoint %s; will retry", checkpoint_id)
            return False

        try:
            await self._store.mark_checkpoint_uploaded(checkpoint_id)
        except Exception:  # noqa: BLE001
            # The remote object exists; the row transition is a cache
            # bookkeeping step. Log and continue — a re-push of an
            # already-stored payload is idempotent.
            logger.exception("Failed to mark checkpoint %s uploaded", checkpoint_id)

        logger.debug("Uploaded checkpoint %s", checkpoint_id)
        await self._prune_after_snapshot(checkpoint_id, data)
        return True

    async def _prune_after_snapshot(self, checkpoint_id: str, data: bytes) -> None:
        """Prune remote checkpoints superseded by a pushed SNAPSHOT.

        Runs only when the just-pushed payload is a SNAPSHOT: every
        checkpoint with a strictly smaller sequence is unreachable from
        recovery (which anchors on the latest snapshot) and is deleted.
        Pruning failures are logged, not fatal — stale objects are
        harmless beyond storage cost.

        Args:
            checkpoint_id: The just-uploaded checkpoint ID.
            data: The serialized payload that was uploaded.
        """
        try:
            payload = CheckpointPayload.model_validate_json(data)
            if not payload.is_snapshot():
                return
            delete = getattr(self._backend, "delete_checkpoint", None)
            if delete is None:
                return
            snapshot_seq = _seq_of(checkpoint_id)
            if snapshot_seq <= 0:
                return
            siblings = await self._backend.list_checkpoints(self._run_id)
            stale = [c for c in siblings if 0 < _seq_of(c) < snapshot_seq]
            for ckpt_id in stale:
                await delete(ckpt_id)
            if stale:
                logger.info(
                    "Pruned %d checkpoint(s) superseded by snapshot %s",
                    len(stale),
                    checkpoint_id,
                )
        except Exception:  # noqa: BLE001
            logger.warning("Checkpoint pruning failed after %s", checkpoint_id, exc_info=True)
