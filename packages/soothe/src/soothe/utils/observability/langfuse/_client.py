"""Langfuse SDK client resolution for host-side spans."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_langfuse_missing_warned = False


def _warn_langfuse_missing_once() -> None:
    """Warn once that the runtime langfuse import is unavailable.

    Langfuse is a runtime dependency on the tracing hot path but ships in the
    optional `observability` extra; without it tracing is silently disabled.
    One warning makes the missing install observable without spamming every
    flush attempt.
    """
    global _langfuse_missing_warned
    if not _langfuse_missing_warned:
        _langfuse_missing_warned = True
        logger.warning(
            "Langfuse is not installed — LLM tracing is disabled. Install it "
            "with `pip install 'soothe[observability]'` to enable tracing."
        )


def host_langfuse_client(soothe_config: Any) -> Any:
    """Return the Langfuse client for this config's public key.

    Raises:
        ImportError: When langfuse is unavailable — callers keep observability
            soft, but the first failure is surfaced as a warning so a missing
            `soothe[observability]` install is observable.
        Exception: On client resolution failure; callers keep observability soft.
    """
    try:
        from langfuse import get_client
    except ImportError:
        _warn_langfuse_missing_once()
        raise
    from soothe_nano.observability.langfuse._client import ensure_langfuse_client, resolve_str

    ensure_langfuse_client(soothe_config)
    pub = resolve_str(soothe_config.observability.langfuse.public_key)
    return get_client(public_key=pub) if pub else get_client()


def flush_langfuse_events(soothe_config: Any) -> None:
    """Export buffered observations now; best-effort."""
    try:
        host_langfuse_client(soothe_config).flush()
    except ImportError:
        pass  # already surfaced once by `_warn_langfuse_missing_once`
    except Exception:
        logger.debug("Langfuse flush failed", exc_info=True)


__all__ = ["flush_langfuse_events", "host_langfuse_client"]
