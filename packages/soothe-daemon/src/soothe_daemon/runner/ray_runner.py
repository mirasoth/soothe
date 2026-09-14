"""Ray-based loop runner — one actor per loop_id.

Manages cluster connection, request timeouts, dead-actor detection,
cooperative cancellation, and busy-loop serialization.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import ray
from ray.exceptions import RayActorError
from ray.util.queue import Queue
from soothe.protocols.runner import LoopRunRequest

from soothe_daemon.runner.stream_cancel import emit_terminal_for_cancelled_error

if TYPE_CHECKING:
    from soothe.config.settings import SootheConfig
    from soothe.runner._runner_shared import StreamChunk

    from soothe_daemon.config import SootheDaemonConfig

logger = logging.getLogger(__name__)

# Timeout for each queue.get_async() poll — short enough to detect dead
# actors quickly, long enough to avoid excessive CPU spinning.
_QUEUE_GET_TIMEOUT_S = 5.0

# Timeout for the liveness ping in _is_actor_alive. Ray raises RayActorError
# quickly once an actor process is gone, so this only bounds the wait for a
# responsive-but-busy actor (e.g. one still in __init__ loading a local model).
_ACTOR_LIVENESS_PING_TIMEOUT_S = 30.0

# ---------------------------------------------------------------------------
# Cluster connection state (module-level singleton).
# ---------------------------------------------------------------------------

_ray_initialized: bool = False
_ray_init_lock = threading.Lock()

# Per-actor resource options derived from RayConfig (set during _ensure_ray_init).
_actor_options: dict[str, Any] = {}

# Module-level semaphore limiting concurrent live actors.
_actor_semaphore: asyncio.Semaphore | None = None


def _ensure_ray_init(daemon_config: SootheDaemonConfig | None) -> None:
    """Initialise the Ray driver connection once per process.

    Reads RayConfig fields (address, num_cpus, object_store_memory,
    max_concurrent_actors, log_to_driver) and populates _actor_options
    and _actor_semaphore.
    """
    global _ray_initialized, _actor_options, _actor_semaphore  # noqa: PLW0603

    if _ray_initialized:
        return

    with _ray_init_lock:
        if _ray_initialized:
            return

        ray_config = None
        if daemon_config is not None:
            ray_config = daemon_config.loop_runner.ray

        init_kwargs: dict[str, Any] = {}
        actor_opts: dict[str, Any] = {}
        max_actors = 10  # default

        if ray_config is not None:
            if ray_config.address:
                init_kwargs["address"] = ray_config.address
            if ray_config.log_to_driver:
                init_kwargs["log_to_driver"] = ray_config.log_to_driver

            if ray_config.num_cpus > 0:
                actor_opts["num_cpus"] = ray_config.num_cpus
            if ray_config.num_gpus > 0:
                actor_opts["num_gpus"] = ray_config.num_gpus
            if ray_config.object_store_memory > 0:
                actor_opts["object_store_memory"] = ray_config.object_store_memory
            if ray_config.runtime_env:
                actor_opts["runtime_env"] = ray_config.runtime_env
            max_actors = ray_config.max_concurrent_actors

        if not ray.is_initialized():
            if init_kwargs:
                ray.init(**init_kwargs)
            else:
                ray.init()
            logger.info("RayLoopRunner: ray.init() completed")

        _actor_options = actor_opts
        _actor_semaphore = asyncio.Semaphore(max_actors)
        _ray_initialized = True


def _reset_ray_state_for_testing() -> None:
    """Reset module-level state — test-only hook."""
    global _ray_initialized, _actor_options, _actor_semaphore  # noqa: PLW0603
    _ray_initialized = False
    _actor_options = {}
    _actor_semaphore = None


# ---------------------------------------------------------------------------
# Busy-loop tracking (for await_loop_dispatchable).
# ---------------------------------------------------------------------------

_busy_loops: set[str] = set()
_busy_loops_lock = threading.Lock()
_dispatch_condition: asyncio.Condition | None = None


def _mark_loop_busy(loop_id: str) -> None:
    with _busy_loops_lock:
        _busy_loops.add(loop_id)


def _mark_loop_idle(loop_id: str) -> None:
    with _busy_loops_lock:
        _busy_loops.discard(loop_id)


def _is_loop_busy(loop_id: str) -> bool:
    with _busy_loops_lock:
        return loop_id in _busy_loops


async def _get_dispatch_condition() -> asyncio.Condition:
    global _dispatch_condition  # noqa: PLW0603
    if _dispatch_condition is None:
        _dispatch_condition = asyncio.Condition()
    return _dispatch_condition


async def await_loop_dispatchable(loop_id: str) -> None:
    """Block until no in-flight actor request is mapped to `loop_id`.

    Serializes consecutive turns on the same loop so the next turn
    waits for the prior turn's actor teardown to complete.

    Uses a bounded cond.wait so a missed notify is recovered within
    a short poll window.
    """
    cond = await _get_dispatch_condition()
    while _is_loop_busy(loop_id):
        try:
            async with cond:
                await asyncio.wait_for(cond.wait(), timeout=1.0)
        except TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Runner.
# ---------------------------------------------------------------------------


class RayLoopRunner:
    """Manages one LoopRunnerActor (Ray remote actor) per loop_id.

    One instance per loop. Created by LoopRunnerFactory when
    SootheDaemonConfig.loop_runner.runner_mode='ray'.
    """

    def __init__(
        self,
        loop_id: str,
        config: SootheConfig,
        daemon_config: SootheDaemonConfig | None = None,
    ) -> None:
        self._loop_id = loop_id
        self._config = config
        self._daemon_config = daemon_config
        self._actor: ray.actor.ActorHandle | None = None
        self._cancel_event = _RayCancelFlag()

        # Extract local model config for GPU-bound actor mode.
        self._local_model_config = None
        if daemon_config is not None and daemon_config.loop_runner.ray.local_model is not None:
            self._local_model_config = daemon_config.loop_runner.ray.local_model

    async def run(self, request: LoopRunRequest) -> AsyncIterator[StreamChunk]:  # type: ignore[override]
        from soothe_daemon.runner.ray_actor import LoopRunnerActor

        _ensure_ray_init(self._daemon_config)

        # Throttle concurrent actors.
        global _actor_semaphore  # noqa: PLW0603
        sem = _actor_semaphore
        if sem is not None:
            await sem.acquire()

        # Build actor with resource options from RayConfig.
        actor_cls = LoopRunnerActor
        if _actor_options:
            actor_cls = actor_cls.options(**_actor_options)  # type: ignore[attr-defined]

        self._actor = actor_cls.remote(
            self._config,
            local_model_config=self._local_model_config,
        )
        queue: Queue = Queue(maxsize=1000)
        self._cancel_event.clear()

        # Mark this loop as busy so consecutive turns serialize.
        _mark_loop_busy(self._loop_id)

        # Determine timeout: request-specific or None (no timeout).
        timeout_seconds = (
            request.timeout_seconds
            if request.timeout_seconds and request.timeout_seconds > 0
            else None
        )

        # Non-blocking remote call — actor pushes chunks into queue.
        self._actor.run.remote(request, queue)
        logger.debug("RayLoopRunner: actor started for loop=%s", self._loop_id[:16])

        try:
            if timeout_seconds and timeout_seconds > 0:
                async with asyncio.timeout(timeout_seconds):
                    async for chunk in self._drain_queue(queue):
                        yield chunk
            else:
                async for chunk in self._drain_queue(queue):
                    yield chunk
        except asyncio.CancelledError:
            logger.debug("RayLoopRunner: run cancelled, exiting gracefully")
            emit_terminal_for_cancelled_error(
                cancel_event=self._cancel_event,
                emit_cancelled=lambda: None,
                emit_error=lambda exc: None,
                worker_id="ray",
                loop_id=self._loop_id,
                request_id=request.loop_id,
                where="run",
            )
            raise
        except TimeoutError:
            logger.warning(
                "RayLoopRunner: request timeout (%ss) loop=%s",
                timeout_seconds,
                self._loop_id[:16],
            )
            raise RuntimeError(f"Request exceeded {timeout_seconds}s timeout") from None
        finally:
            _mark_loop_idle(self._loop_id)
            if sem is not None:
                sem.release()
            # Notify any waiting dispatchable turns.
            cond = await _get_dispatch_condition()
            async with cond:
                cond.notify_all()

    async def _drain_queue(self, queue: Queue) -> AsyncIterator[StreamChunk]:
        """Drain chunks from the Ray queue, detecting dead actors.

        On each iteration, checks actor liveness after a queue poll
        timeout. If the actor has died (process crash), raises
        RayActorError so the stream consumer unblocks.
        """
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get_async(),
                    timeout=_QUEUE_GET_TIMEOUT_S,
                )
            except TimeoutError:
                # No item within the poll window — check actor liveness.
                if self._actor is not None and not self._is_actor_alive():
                    logger.error(
                        "RayLoopRunner: actor died mid-stream loop=%s",
                        self._loop_id[:16],
                    )
                    raise RayActorError(
                        f"Ray actor for loop {self._loop_id} died mid-stream"
                    ) from None
                # Actor still alive — continue polling.
                continue

            if kind == "done":
                return
            if kind == "error":
                raise payload
            if kind == "cancelled":
                raise asyncio.CancelledError()
            if kind == "timeout":
                raise TimeoutError(str(payload) if payload else "Ray actor timeout")
            # kind == "chunk"
            yield payload

    def _is_actor_alive(self) -> bool:
        """Return True unless the actor is confirmed dead.

        A ping is queued behind any in-flight ``__init__`` (e.g. a local-model
        actor loading vLLM, which can take a minute), so a *timeout* does NOT
        mean the actor is dead — only ``RayActorError`` (the process is gone)
        is treated as death. This avoids false-positive ``RayActorError``
        mid-stream while a GPU-bound actor is still constructing.
        """
        if self._actor is None:
            return False
        try:
            ray.get(self._actor.ping.remote(), timeout=_ACTOR_LIVENESS_PING_TIMEOUT_S)
            return True
        except RayActorError:
            return False
        except Exception:  # noqa: BLE001  (timeout/network — actor still busy)
            return True

    async def cancel(self) -> None:
        if self._actor is None:
            return
        logger.info("RayLoopRunner: cancelling actor for loop=%s", self._loop_id[:16])

        # Set the cancel flag so emit_terminal_for_cancelled_error maps
        # any leaked CancelledError to a cooperative cancel terminal.
        self._cancel_event.set()

        # Ask actor to cancel gracefully — it should emit "done" to queue.
        cancel_ref = self._actor.cancel.remote()

        # Wait for actor's cancel method to complete.
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(cancel_ref.future()),  # type: ignore[attr-defined]
                timeout=10.0,
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            logger.warning("RayLoopRunner: actor cancel timed out or failed")

        # Brief grace period for driver to receive pending queue items before hard kill.
        await asyncio.sleep(0.5)

        # Hard kill actor as cleanup.
        try:
            ray.kill(self._actor)
        except Exception:  # noqa: BLE001
            pass
        self._actor = None

    async def is_idle(self) -> bool:
        """True when this loop's actor is gone (no longer busy)."""
        return self._actor is None

    async def force_kill(self, *, timeout: float = 10.0) -> None:
        """Hard-kill the actor backing this loop (cancel backstop)."""
        if self._actor is None:
            return
        logger.warning("RayLoopRunner: force-killing actor for loop=%s", self._loop_id[:16])
        try:
            ray.kill(self._actor)
        except Exception:  # noqa: BLE001
            pass
        self._actor = None

    async def set_clarification_mode(
        self,
        mode: str,
        *,
        interaction_mode: str | None = None,
    ) -> bool:
        """Hot-swap agent mode — not supported for distributed mode.

        Ray actors don't expose their SootheRunner. Returns False; the
        caller falls back to the next-turn path.
        """
        return False


class _RayCancelFlag(threading.Event):
    """Cancel flag implementing the `is_set()` protocol for stream_cancel."""


__all__ = [
    "RayLoopRunner",
    "await_loop_dispatchable",
    "_ensure_ray_init",
    "_reset_ray_state_for_testing",
]
