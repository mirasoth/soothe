"""Confinement guard: `Command(update=..., goto=...)` is orphan-recovery only.

Normal turns enter the graph with `Command(resume=...)` or a plain input
dict. The orphan goto shape exists solely for a pending clarification whose
live `interrupt()` was destroyed by a worker crash. The source scan fails if
the builder leaks to any other call site.
"""

from __future__ import annotations

import re
from pathlib import Path

from langgraph.types import Command

from soothe.sloop.relay._adapter import (
    build_live_interrupt_resume_command,
    build_orphan_goto_command,
)

PKG_ROOT = Path(__file__).parents[5] / "src"


def test_orphan_goto_builder_has_no_call_sites_outside_relay() -> None:
    """`build_orphan_goto_command` may be referenced only from
    `relay/relay.py` (the orphan branch of `build_resume_command`)."""
    pattern = re.compile(r"build_orphan_goto_command")
    offenders: list[str] = []
    for path in (PKG_ROOT).rglob("*.py"):
        rel = path.relative_to(PKG_ROOT)
        rel_str = str(rel)
        if not rel_str.startswith("soothe/") and not rel_str.startswith("soothe_"):
            continue
        if "test" in path.parts:
            continue
        if rel_str == "soothe/sloop/relay/_adapter.py":
            continue
        if rel_str == "soothe/sloop/relay/relay.py":
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(rel_str)
    assert not offenders, (
        "build_orphan_goto_command must stay confined to the relay orphan "
        f"recovery path; found references in: {offenders}"
    )


def test_live_interrupt_resume_command_is_resume_only() -> None:
    cmd = build_live_interrupt_resume_command(["approve"])
    assert isinstance(cmd, Command)
    assert cmd.resume == {"answers": ["approve"]}


def test_orphan_goto_command_shape() -> None:
    cmd = build_orphan_goto_command(
        answer_state={
            "answers": ["approve"],
            "source": "human",
            "confidence": None,
            "defer": False,
            "audit": {},
        },
        goto="execute",
        relay_state={"inbox": [], "active_origin": "execute", "audit": []},
    )
    assert isinstance(cmd, Command)
    assert cmd.goto == "execute"
    assert "relay_state" in (cmd.update or {})
    updated = (cmd.update or {})["relay_state"]
    assert updated["answers"] == [
        {
            "interrupt_id": "",
            "answer": {
                "answers": ["approve"],
                "source": "human",
                "confidence": None,
                "defer": False,
                "audit": {},
            },
        }
    ]
