"""Per-run context readers for the `AutoModeMiddleware` inline gate.

All values come from the LangGraph `configurable` written by the executor
per step thread — the same channel the old `interrupt_on` `when` predicates
used (workspace, tool-approval allowlist, interaction mode) plus the two
RFC-634 additions (clarification mode, human attachment).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from soothe.sloop.utils.config_keys import (
    SOOTHE_CLARIFICATION_MODE_KEY,
    SOOTHE_HUMAN_ATTACHED_KEY,
    SOOTHE_INTERACTION_MODE_KEY,
)


def _configurable_from_runtime(runtime: Any) -> Mapping[str, Any]:
    """Extract the LangGraph configurable for the running node.

    Node-level `after_model` runtimes do not carry the runnable config
    directly; `get_config()` inside a graph node returns it (the same
    pattern `HumanInTheLoopMiddleware._should_interrupt` uses). Falls back
    to a `runtime.config` attribute when present, then to an empty mapping.
    """
    try:
        from langgraph.config import get_config

        config = get_config()
        if isinstance(config, Mapping):
            configurable = config.get("configurable")
            if isinstance(configurable, Mapping):
                return configurable
    except Exception:  # noqa: BLE001 — no graph context; try runtime fallback
        pass
    config = getattr(runtime, "config", None)
    if isinstance(config, Mapping):
        configurable = config.get("configurable")
        if isinstance(configurable, Mapping):
            return configurable
    return {}


@dataclass(frozen=True)
class AutoModeContext:
    """Per-run gate context resolved from `configurable`."""

    workspace: str | None = None
    allowlist: tuple[Mapping[str, Any], ...] = ()
    clarification_mode: str | None = None
    human_attached: bool = False
    interaction_mode: str | None = None
    configurable: Mapping[str, Any] = field(default_factory=dict)

    @property
    def bypass(self) -> bool:
        """True when the owning step runs in bypass interaction mode."""
        return self.interaction_mode == "bypass"

    @classmethod
    def from_runtime(
        cls,
        runtime: Any,
        *,
        default_mode: str = "auto",
    ) -> AutoModeContext:
        """Read gate context from an agent runtime's configurable.

        Missing keys fail safe: clarification mode falls back to the
        build-time `default_mode`; `human_attached` defaults to `False`
        (autopilot — the strict direction for safety escalations).
        """
        configurable = _configurable_from_runtime(runtime)
        workspace = configurable.get("workspace")
        allowlist = configurable.get("tool_approval_allowlist")
        mode = configurable.get(SOOTHE_CLARIFICATION_MODE_KEY)
        human = configurable.get(SOOTHE_HUMAN_ATTACHED_KEY)
        interaction = configurable.get(SOOTHE_INTERACTION_MODE_KEY)
        return cls(
            workspace=workspace if isinstance(workspace, str) and workspace.strip() else None,
            allowlist=tuple(allowlist) if isinstance(allowlist, list) else (),
            clarification_mode=(
                mode
                if isinstance(mode, str) and mode.strip() in ("auto", "manual")
                else default_mode
            ),
            human_attached=bool(human),
            interaction_mode=interaction if isinstance(interaction, str) else None,
            configurable=configurable,
        )


__all__ = ["AutoModeContext"]
