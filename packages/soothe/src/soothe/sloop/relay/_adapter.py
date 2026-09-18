"""Private LangGraph `Command` construction adapters.

Isolates `langgraph.types.Command` imports from the relay orchestration so
`relay.relay` can be unit-tested without LangGraph at import time. The two
resume shapes (live-interrupt resume vs orphan goto) are centralized here.

Boundary contract: normal turns enter the graph with `Command(resume=...)`
or a plain input dict only. `Command(update=..., goto=...)` is exclusively
the orphan-recovery shape — a persisted pending clarification whose live
LangGraph `interrupt()` was destroyed by a worker crash. Any other use as
graph input is a protocol violation (enforced by
`test_adapter_orphan_confinement.py`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_live_interrupt_resume_command(answers: list[str]) -> Any:
    """Build `Command(resume={"answers": [...]})` for a live StrangeLoop interrupt.

    Resumes the `interrupt()` call in `InteractiveClarificationPolicy._answer`
    so the policy returns the answer and `await_clarification` continues.
    """
    from langgraph.types import Command

    return Command(resume={"answers": list(answers)})


def build_orphan_goto_command(
    *,
    answer_state: dict[str, Any],
    goto: str,
    relay_state: Mapping[str, Any] | None,
) -> Any:
    """Build `Command(update={...}, goto=...)` for an orphaned clarification.

    Used when the persisted state shows a pending clarification but no live
    LangGraph `interrupt()` to resume (e.g. after a worker crash destroyed the
    in-flight interrupt). Routes directly to the origin's resume node with the
    answer merged into `relay_state` as a single answer record (the empty
    interrupt id maps to the inbox head on consume).
    """
    from langgraph.types import Command

    merged_relay_state: dict[str, Any] = dict(relay_state or {})
    merged_relay_state["answers"] = [{"interrupt_id": "", "answer": answer_state}]
    return Command(
        update={"relay_state": merged_relay_state},
        goto=goto,
    )


__all__ = [
    "build_live_interrupt_resume_command",
    "build_orphan_goto_command",
]
