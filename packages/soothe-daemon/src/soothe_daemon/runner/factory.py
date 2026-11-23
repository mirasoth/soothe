"""LoopRunnerFactory — creates per-loop runner instances."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from soothe.protocols.runner import LoopRunnerProtocol

if TYPE_CHECKING:
    from soothe.config.settings import SootheConfig
    from soothe.identity.runtime import IdentityRuntime

    from soothe_daemon.config import SootheDaemonConfig

logger = logging.getLogger(__name__)


class LoopRunnerFactory:
    """Creates a `LoopRunnerProtocol` instance for each new `loop_id`."""

    def __init__(
        self,
        daemon_config: SootheDaemonConfig,
        agent_config: SootheConfig,
        *,
        identity_runtime: IdentityRuntime | None = None,
    ) -> None:
        self._daemon_config = daemon_config
        self._agent_config = agent_config
        self._identity_runtime = identity_runtime
        self._pool_initialized = False

        # Select runner mode from the unified loop_runner config block.
        mode = daemon_config.loop_runner.runner_mode
        self._mode = mode

        if identity_runtime is not None and identity_runtime.enabled and mode == "process_pool":
            raise ValueError(
                f"Identity service requires thread_pool mode: {mode} uses "
                "subprocess/VM/container spawn and cannot propagate IdentityService to "
                "isolated workers."
            )

        if mode == "process_pool":
            pool = daemon_config.loop_runner.process_pool
            logger.info(
                "LoopRunnerFactory: process pool mode (min=%d, max=%d workers, max_requests=%d)",
                pool.min_pool_size,
                pool.max_pool_size,
                pool.max_requests_per_worker,
            )
        elif mode == "thread_pool":
            pool = daemon_config.loop_runner.thread_pool
            logger.info(
                "LoopRunnerFactory: thread pool mode (min=%d, max=%d threads, max_requests=%d)",
                pool.min_pool_size,
                pool.max_pool_size,
                pool.max_requests_per_thread,
            )
        elif mode == "ray":
            try:
                import ray  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Ray is required for ray mode. Install with: pip install ray"
                ) from exc
            logger.info("LoopRunnerFactory: ray mode (Ray actor per loop)")

    async def get_shared_execution_pool(self) -> Any | None:
        """Return the shared thread/process pool, if this factory uses one."""
        if self._mode == "thread_pool":
            from soothe_daemon.runner.thread_runner import ThreadPool

            return await ThreadPool.get_shared_instance(
                self._agent_config,
                self._daemon_config,
                identity_runtime=self._identity_runtime,
            )
        if self._mode == "process_pool":
            from soothe_daemon.runner.pool_runner import ProcessPool

            return await ProcessPool.get_shared_instance(self._agent_config, self._daemon_config)
        if self._mode == "ray":
            from soothe_daemon.runner.ray_runner import await_loop_dispatchable

            class _RayPoolNamespace:
                """Pool facade exposing `await_loop_dispatchable`."""

                await_loop_dispatchable = staticmethod(await_loop_dispatchable)

            return _RayPoolNamespace()
        return None

    async def initialize_pool(self) -> None:
        """Pre-warm worker pool if enabled."""
        if self._pool_initialized:
            return

        if self._mode == "process_pool":
            from soothe_daemon.runner.pool_runner import ProcessPool

            await ProcessPool.get_shared_instance(self._agent_config, self._daemon_config)
            self._pool_initialized = True
            logger.info("LoopRunnerFactory: process worker pool pre-warmed")
        elif self._mode == "thread_pool":
            from soothe_daemon.runner.thread_runner import ThreadPool

            await ThreadPool.get_shared_instance(
                self._agent_config,
                self._daemon_config,
                identity_runtime=self._identity_runtime,
            )
            self._pool_initialized = True
            logger.info("LoopRunnerFactory: thread pool pre-warmed")
        elif self._mode == "ray":
            from soothe_daemon.runner.ray_runner import _ensure_ray_init

            _ensure_ray_init(self._daemon_config)
            self._pool_initialized = True
            logger.info("LoopRunnerFactory: ray cluster connected")

    async def shutdown_pool(self) -> None:
        """Shutdown worker pool if active.

        Called by SootheDaemon.stop() during shutdown.
        """
        if not self._pool_initialized:
            return

        if self._mode == "process_pool":
            from soothe_daemon.runner.pool_runner import ProcessPool

            await ProcessPool.close_shared_instance()
            logger.info("LoopRunnerFactory: process worker pool shutdown")
        elif self._mode == "thread_pool":
            from soothe_daemon.runner.thread_runner import ThreadPool

            await ThreadPool.close_shared_instance()
            logger.info("LoopRunnerFactory: thread pool shutdown")
        elif self._mode == "ray":
            import ray

            if ray.is_initialized():
                ray.shutdown()
                logger.info("LoopRunnerFactory: ray cluster shutdown")

        self._pool_initialized = False

        from soothe_daemon.persistence.pools import close_shared_postgres_pools

        await close_shared_postgres_pools()

    def create_runner(self, loop_id: str) -> LoopRunnerProtocol:
        """Return a runner instance for `loop_id`."""
        if self._mode == "process_pool":
            from soothe_daemon.runner.pool_runner import ProcessLoopRunner

            return ProcessLoopRunner(loop_id, self._agent_config, self._daemon_config)
        if self._mode == "thread_pool":
            from soothe_daemon.runner.thread_runner import ThreadLoopRunner

            return ThreadLoopRunner(
                loop_id,
                self._agent_config,
                self._daemon_config,
                identity_runtime=self._identity_runtime,
            )
        # mode == "ray"
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        return RayLoopRunner(loop_id, self._agent_config, self._daemon_config)


__all__ = ["LoopRunnerFactory"]
