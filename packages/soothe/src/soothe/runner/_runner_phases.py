"""Phase orchestration mixin for SootheRunner (LangGraph checkpointer helpers)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from soothe_nano.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


def _create_host_serde() -> Any:
    """Checkpoint serde extended with host/routing types that ride in loop state.

    Every host enum written into loop state must be listed: an unlisted type is
    serialized but blocked on deserialize, which silently drops the value on
    restore (`Blocked deserialization of ... - not in allowed_msgpack_modules`).
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from soothe_nano.protocols.routing import RoutingClassification, TaskComplexity
    from soothe_nano.utils.serde import create_soothe_serde

    serde: JsonPlusSerializer = create_soothe_serde().with_msgpack_allowlist(
        [
            RoutingClassification,
            TaskComplexity,
        ]
    )
    return serde


class PhasesMixin:
    """LangGraph checkpointer initialization.

    Mixed into `SootheRunner` -- all `self.*` attributes are defined
    on the concrete class.
    """

    # -- LangGraph stream with interrupt auto-resume -------------------------

    async def _ensure_checkpointer_initialized(self) -> None:
        """Attach the async checkpointer (AsyncSqliteSaver / AsyncPostgresSaver).

        Created from `self._checkpointer_pool` and wired onto
        `self._core_agent.graph`. Prefer `_materialize_core_agent()` from
        StrangeLoop / autopilot entry points so LazyCoreAgent is compiled first;
        this method is also the LazyCoreAgent `materialize_hook`.

        Raises:
            ConfigurationError: Checkpointer initialization failed.
        """
        from soothe.coreagent.lazy import LazyCoreAgent

        if isinstance(self._core_agent, LazyCoreAgent) and not self._core_agent.is_materialized:
            # Materialize so the async hook can attach the checkpointer. A silent
            # return here previously left interactive StrangeLoop graphs without
            # durable ``interrupt()`` state.
            await self._core_agent.amaterialize()
            return

        if self._checkpointer_initialized or self._checkpointer_pool is None:
            return

        agent = (
            self._core_agent.materialize()
            if isinstance(self._core_agent, LazyCoreAgent)
            else self._core_agent
        )

        # Check if agent supports LangGraph checkpointer (has .graph property)
        try:
            _ = agent.graph
        except NotImplementedError:
            # Agent doesn't use LangGraph checkpointer
            logger.debug("Agent does not support LangGraph checkpointer, skipping initialization")
            self._checkpointer_initialized = True
            return

        try:
            # Check if pool is a string (SQLite path) or an object (PostgreSQL pool)
            is_sqlite = isinstance(self._checkpointer_pool, str)
        except Exception:
            is_sqlite = False

        try:
            if is_sqlite:
                await self._init_sqlite_checkpointer_with_retry()
            else:
                # PostgreSQL: wrap initialization with retry for DB restart resilience
                await self._init_postgres_checkpointer_with_retry()
        except ModuleNotFoundError as exc:
            logger.error("Missing dependency for checkpointer: %s", exc)
            missing = str(exc.name) if exc.name else "unknown"
            raise ConfigurationError(
                f"Checkpointer initialization failed: missing module '{missing}'.\n"
                f"Install the required package and restart.\n"
                f"Persistent storage required - no fallback available."
            ) from exc
        except Exception as exc:
            logger.error("Failed to initialize async checkpointer: %s", exc)
            raise ConfigurationError(
                f"Checkpointer initialization failed: {exc}\n"
                f"Persistent storage required - no fallback available."
            )

    async def _init_sqlite_checkpointer_with_retry(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 4.0,
    ) -> None:
        """Initialize SQLite checkpointer with directory safety and retry.

        Ensures the parent directory exists before connecting (defensive
        against cleanup races and worker-subprocess directory absence),
        wraps `aiosqlite.connect` + `AsyncSqliteSaver.setup` in a
        try/except that closes the connection on failure (prevents leaked
        aiosqlite background threads), and retries transient SQLite errors
        (WAL contention, file-lock races) with exponential backoff.

        Args:
            max_attempts: Maximum retry attempts (default: 3).
            base_delay: Initial retry delay in seconds (default: 1.0).
            max_delay: Maximum retry delay cap (default: 4.0).

        Raises:
            ConfigurationError if all retries exhausted or error is unrecoverable.
        """
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from soothe.sloop.checkpoints.retry_utils import is_recoverable_sqlite_error

        db_path = Path(self._checkpointer_pool)
        delay = base_delay
        for attempt in range(1, max_attempts + 1):
            # Defensive: ensure parent directory exists before connecting.
            # The nano resolver creates it at init, but worker subprocesses
            # or post-cleanup re-inits may encounter a missing directory.
            db_path.parent.mkdir(parents=True, exist_ok=True)

            conn = None
            try:
                conn = await aiosqlite.connect(str(db_path))
                checkpointer = AsyncSqliteSaver(conn, serde=_create_host_serde())
                await checkpointer.setup()

                self._checkpointer = checkpointer
                self._materialized_core_agent().graph.checkpointer = checkpointer
                self._checkpointer_initialized = True
                logger.info(
                    "AsyncSqliteSaver created and tables initialized at %s",
                    db_path,
                )
                return
            except Exception as exc:
                # Close the connection if it was opened but setup failed,
                # preventing a leaked aiosqlite background thread whose
                # __del__ would emit a ResourceWarning + asyncio traceback.
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        logger.debug(
                            "Failed to close aiosqlite connection after error", exc_info=True
                        )

                if not is_recoverable_sqlite_error(exc):
                    logger.error(
                        "[checkpointer_init] Unrecoverable SQLite error: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    raise

                if attempt >= max_attempts:
                    logger.error(
                        "[checkpointer_init] All %d SQLite retries exhausted, last error: %s: %s",
                        max_attempts,
                        type(exc).__name__,
                        exc,
                    )
                    raise

                logger.warning(
                    "[checkpointer_init] Recoverable SQLite error on attempt %d/%d: %s. "
                    "Retrying in %.1fs...",
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                    delay,
                )

                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

        # Should never reach here
        raise RuntimeError("Unexpected retry loop exit for SQLite checkpointer initialization")

    async def _init_postgres_checkpointer_with_retry(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 8.0,
    ) -> None:
        """Initialize PostgreSQL checkpointer with connection retry.

        Wraps pool.open() and checkpointer.setup() with exponential backoff
        retry for transient connection errors (AdminShutdown, server restart).

        Args:
            max_attempts: Maximum retry attempts (default: 3).
            base_delay: Initial retry delay in seconds (default: 1.0).
            max_delay: Maximum retry delay cap (default: 8.0).

        Raises:
            ConfigurationError if all retries exhausted.
        """
        from soothe.runner.resolver.shared_checkpointer_pool import SharedCheckpointerPool
        from soothe.sloop.checkpoints.retry_utils import ConnectionRetry

        delay = base_delay
        for attempt in range(1, max_attempts + 1):
            try:
                await self._checkpointer_pool.open()

                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                checkpointer = AsyncPostgresSaver(
                    self._checkpointer_pool, serde=_create_host_serde()
                )
                if SharedCheckpointerPool.is_shared_pool(self._checkpointer_pool):
                    await SharedCheckpointerPool.setup_checkpointer(
                        self._checkpointer_pool,
                        checkpointer.setup,
                    )
                else:
                    await checkpointer.setup()

                self._checkpointer = checkpointer
                self._materialized_core_agent().graph.checkpointer = checkpointer
                self._checkpointer_initialized = True
                logger.info(
                    "AsyncPostgresSaver pool open and tables initialized, checkpointer replaced"
                )
                return
            except Exception as exc:
                # Check if this is a recoverable connection error
                if not ConnectionRetry.is_recoverable_connection_error(exc):
                    logger.error(
                        "[checkpointer_init] Unrecoverable error: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    raise

                # Last attempt - no more retries
                if attempt >= max_attempts:
                    logger.error(
                        "[checkpointer_init] All %d retries exhausted, last error: %s: %s",
                        max_attempts,
                        type(exc).__name__,
                        exc,
                    )
                    raise

                # Log retry and reset pool
                logger.warning(
                    "[checkpointer_init] Recoverable connection error on attempt %d/%d: %s. "
                    "Resetting pool and retrying in %.1fs...",
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                    delay,
                )

                # Reset shared pool to force fresh connections
                if SharedCheckpointerPool.is_shared_pool(self._checkpointer_pool):
                    self._checkpointer_pool = await SharedCheckpointerPool.reset_shared_instance(
                        self._config
                    )
                    if self._checkpointer_pool is None:
                        raise RuntimeError(
                            "Failed to recreate shared checkpointer pool after reset"
                        )

                # Exponential backoff with cap
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

        # Should never reach here
        raise RuntimeError("Unexpected retry loop exit for checkpointer initialization")
