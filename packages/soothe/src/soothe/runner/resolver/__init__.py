"""Runner-level resolution: durability, checkpointer, planner, and policy.

Agent/tool/subagent resolution happens inside soothe-nano's AgentBuilder
(`soothe_nano.resolve`); this module only binds host-owned pools and
runner-scoped protocols for `SootheRunner`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from soothe_nano.resolve import resolve_policy

from soothe.config import SootheConfig

from ._resolver_infra import resolve_checkpointer, resolve_durability

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from soothe_nano.protocols.planner import PlannerProtocol

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_checkpointer",
    "resolve_durability",
    "resolve_planner",
    "resolve_policy",
]


def resolve_planner(
    config: SootheConfig,
    model: BaseChatModel | None,
) -> PlannerProtocol | None:
    """Host planner resolution — always `None`; the loop plans inline."""
    del config, model
    return None
