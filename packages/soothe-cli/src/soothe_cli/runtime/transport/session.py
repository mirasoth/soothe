"""Daemon-backed session helpers for the Textual TUI."""

from __future__ import annotations

import logging
from typing import Any

from soothe_client.appkit import DaemonSession
from soothe_client.helpers import websocket_url_from_config

from soothe_cli.runtime.state.session_stats import TurnEventStats
from soothe_cli.runtime.wire.chunk_filter import should_drop_stream_chunk_early

logger = logging.getLogger(__name__)

# Match headless daemon client post-idle drain window.
_POST_IDLE_DRAIN_DEADLINE_S = 0.5

# Brief close handshake on TUI exit — the daemon cleans up on disconnect anyway.
TUI_EXIT_HANDSHAKE_TIMEOUT_S = 0.3


class TuiDaemonSession(DaemonSession):
    """Own the daemon websocket session used by the TUI."""

    def __init__(
        self,
        cfg: Any,
        *,
        workspace: str | None = None,
        post_idle_drain_deadline: float = _POST_IDLE_DRAIN_DEADLINE_S,
    ) -> None:
        self._cfg = cfg
        super().__init__(
            websocket_url_from_config(cfg),
            workspace=workspace,
            stream_delivery=self._resolve_stream_delivery_mode,
            post_idle_drain_deadline=post_idle_drain_deadline,
            early_drop_fn=should_drop_stream_chunk_early,
            stats_factory=TurnEventStats,
        )

    def _resolve_stream_delivery_mode(self) -> str:
        """Determine stream delivery mode from config."""
        if (
            self._cfg
            and hasattr(self._cfg, "output_streaming_mode")
            and self._cfg.output_streaming_mode
        ):
            return str(self._cfg.output_streaming_mode)

        if self._cfg and hasattr(self._cfg, "agent"):
            streaming_cfg = self._cfg.agent.loop.output_streaming
            return str(streaming_cfg.mode)

        return "adaptive"

    async def list_loops(
        self,
        *,
        limit: int = 20,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Return `loop_list` via the RPC sidecar."""
        params: dict[str, Any] = {"limit": limit}
        if workspace:
            params["filter"] = {"workspace": workspace}
        async with self._rpc_lock:
            await self._ensure_rpc_connected()
            return await self._rpc_client.request("loop_list", params, timeout=15.0)

    async def fetch_daemon_info(self) -> dict[str, Any] | None:
        """Fetch daemon identity for restart detection (IG-DAEM-004).

        Returns None when the RPC fails (older daemon without the method,
        transport issues) so callers treat the epoch as unknown rather than as
        a restart.
        """
        async with self._rpc_lock:
            try:
                await self._ensure_rpc_connected()
                return await self._rpc_client.request("daemon_info", {}, timeout=5.0)
            except Exception:
                logger.debug("daemon_info RPC failed", exc_info=True)
                return None


__all__ = [
    "TUI_EXIT_HANDSHAKE_TIMEOUT_S",
    "TuiDaemonSession",
]
