"""Shared SootheConfig builders for unit and integration tests."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from soothe.config.models import ModelRouter, RouterProfile
from soothe.config.settings import SootheConfig

logger = logging.getLogger(__name__)


def config_with_router_profile(
    router: ModelRouter | dict[str, Any] | None = None,
    *,
    profile_name: str = "test",
    embedding_dims: int = 1536,
    **kwargs: Any,
) -> SootheConfig:
    """Build ``SootheConfig`` with a single router profile."""
    if not router:
        return SootheConfig(**kwargs)
    if isinstance(router, dict):
        router = ModelRouter(**router)
    embedding_profile = kwargs.pop(
        "embedding_profile",
        {"model_role": "openai:text-embedding-3-small", "embedding_dims": embedding_dims},
    )
    return SootheConfig(
        router_profiles=[
            RouterProfile(
                name=profile_name,
                router=router,
            )
        ],
        embedding_profile=embedding_profile,
        active_router_profile=profile_name,
        **kwargs,
    )


async def reset_pool_singletons(agent_mod: Any, registry_cls: Any) -> None:
    """Reset all PostgreSQL pool singleton references.

    Stops pool worker tasks and nullifies all singleton references so
    subsequent tests create fresh pools. We avoid calling the full
    ``AsyncConnectionPool.close()`` because it can hang indefinitely on dead
    sockets (psycopg C extension ignores asyncio cancellation). Instead, we
    signal the workers to stop and mark the pool as closed, which prevents the
    event loop teardown from hanging on pending pool tasks.

    ``AsyncConnectionPool._signal_stop_worker`` became a coroutine in
    psycopg_pool 3.3 (it was a plain ``put_nowait`` before). Calling it without
    ``await`` leaves the worker and scheduler tasks alive, and pytest-asyncio's
    event-loop teardown then blocks forever in ``_cancel_all_tasks`` — so the
    result is awaited when awaitable and the worker tasks are joined under a
    bounded timeout.
    """

    def _signalable(candidate: Any) -> Any:
        """Unwrap host pool wrappers down to the psycopg ``AsyncConnectionPool``.

        ``SharedPostgreSQLPool`` owns its psycopg pool in ``_pool`` (or borrows
        the registry's), so worker tasks live on that inner pool and signalling
        the wrapper itself would be a no-op. Guard on ``_signal_stop_worker``:
        ``AsyncConnectionPool`` also has a ``_pool`` attribute, holding the
        connection deque, which must not be unwrapped.
        """
        inner = getattr(candidate, "_pool", None)
        if inner is not None and hasattr(inner, "_signal_stop_worker"):
            return inner
        return candidate

    pools_to_reset: list[Any] = []

    registry = registry_cls.try_get_instance()
    if registry is not None:
        pools_to_reset.extend(registry._pools.values())
    if getattr(agent_mod, "_shared_pool", None) is not None:
        pools_to_reset.append(_signalable(agent_mod._shared_pool))

    import soothe_nano.persistence.shared_checkpointer_pool as nano_cp_mod

    if getattr(nano_cp_mod, "_shared_checkpointer_pool", None) is not None:
        pools_to_reset.append(_signalable(nano_cp_mod._shared_checkpointer_pool))

    # Signal worker tasks to stop and mark pools as closed.
    for pool in pools_to_reset:
        if pool is None:
            continue
        workers: list[Any] = []
        try:
            signal = pool._signal_stop_worker()  # type: ignore[attr-defined]
            workers = list(await signal if inspect.isawaitable(signal) else signal or [])
        except Exception:
            pass
        try:
            pool._closed = True  # type: ignore[attr-defined]
        except Exception:
            pass
        if workers:
            try:
                async with asyncio.timeout(5.0):
                    await asyncio.gather(*workers, return_exceptions=True)
            except Exception:
                pass

    registry_cls.reset_instance()
    agent_mod._shared_pool = None

    # Reset the asyncio.Lock so a locked lock from a prior test doesn't block
    # the next get_shared_instance() call. With session-scoped event loop,
    # the lock persists across tests.
    agent_mod._pool_lock = asyncio.Lock()

    nano_cp_mod._shared_checkpointer_pool = None
    nano_cp_mod._checkpointer_setup_done = False
    nano_cp_mod._setup_waiter = None
