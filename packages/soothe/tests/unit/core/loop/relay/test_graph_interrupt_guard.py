"""Guard: `GraphInterrupt` must never be swallowed on the stream path.

LangGraph implements pauses as exceptions — a broad `except` around
interrupt-raising code silently breaks the pause. The inventory test AST-scans
the stream-path modules and fails when a NEW broad handler appears that
neither re-raises nor is consciously allowlisted below. The behavioral tests
pin propagation through the real wrappers.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from soothe.sloop.engine.execute.graph_interrupt import GraphStreamChunkReader

SRC_ROOT = Path(__file__).parents[5] / "src" / "soothe" / "sloop"

STREAM_PATH_MODULES = [
    "engine/execute/executor.py",
    "engine/execute/graph_interrupt.py",
    "relay/relay.py",
    "relay/outbox.py",
    "relay/inbox.py",
    "relay/channel.py",
    "relay/snapshot.py",
    "relay/reconcile.py",
    "relay/_adapter.py",
    "stations/execute/execute.py",
    "stations/sidecars/await_user.py",
    "clarification/interactive.py",
    "clarification/detector.py",
]

# Handlers proven not to wrap interrupt-raising code. Every entry needs a
# reason; adding a handler means editing this table deliberately.
ALLOWED_SWALLOW_SITES: dict[str, dict[str, str]] = {
    "engine/execute/executor.py": {
        "_maybe_aclose_act_stream": "closes an exhausted stream iterator; no interrupt-raising call",
    },
    "relay/relay.py": {
        "capture_interrupt": "parses an already-caught GraphInterrupt payload; parsing cannot re-raise it",
    },
    "relay/channel.py": {
        "_deserialize_model": "pydantic/dataclass deserialization only",
    },
    "relay/reconcile.py": {
        "_thread_pending_interrupts": "aget_state checkpoint read; does not stream the agent",
    },
    "stations/execute/execute.py": {
        "_ensure_ce_step_for_resume": "Context Engine step recreate",
        "_persist_planner_ask_step_outcome": "Context Engine / plan DAG persistence",
        "node_execute": "CE set_previous_plan and context compaction (raw LLM call, not the agent graph)",
    },
    "stations/sidecars/await_user.py": {
        "_hard_defer": "CE save on park",
        "node_await_clarification": "CE save + goal park mark, both before the policy interrupt()",
    },
}


def _handler_is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names: list[str] = []
    if isinstance(handler.type, ast.Name):
        names = [handler.type.id]
    elif isinstance(handler.type, ast.Tuple):
        names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
    return any(n in ("Exception", "BaseException") for n in names)


def _handler_catches_graph_interrupt(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if isinstance(t, ast.Name):
        return t.id == "GraphInterrupt"
    if isinstance(t, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id == "GraphInterrupt" for e in t.elts)
    return False


def _body_has_raise(body: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
    return False


def _broad_handlers_without_raise(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        # A sibling `except GraphInterrupt: raise` handler makes the whole
        # try statement safe — the pause escapes before the broad handler.
        gi_re_raised = any(
            _handler_catches_graph_interrupt(h) and _body_has_raise(h.body) for h in node.handlers
        )
        if gi_re_raised:
            continue
        for handler in node.handlers:
            if _handler_is_broad(handler) and not _body_has_raise(handler.body):
                yield _enclosing_function(tree, node), handler.lineno


def _enclosing_function(tree: ast.Module, target: ast.Try) -> str:
    best: str = "<module>"
    best_size = -1
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if target in ast.walk(fn):
            size = len(list(ast.walk(fn)))
            if best_size == -1 or size < best_size:
                best = fn.name
                best_size = size
    return best


def test_no_unlisted_broad_except_on_stream_path() -> None:
    violations: dict[str, list[str]] = {}
    for rel in STREAM_PATH_MODULES:
        allowed = ALLOWED_SWALLOW_SITES.get(rel, {})
        for fn_name, lineno in _broad_handlers_without_raise(SRC_ROOT / rel):
            if fn_name not in allowed:
                violations.setdefault(rel, []).append(f"{fn_name}:{lineno}")
    assert not violations, (
        "New broad `except` on the interrupt stream path — a `GraphInterrupt` "
        "raised inside would be swallowed and the pause broken. Either re-raise "
        f"it or add a justified entry to ALLOWED_SWALLOW_SITES: {violations}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    present: dict[str, set[str]] = {}
    for rel in STREAM_PATH_MODULES:
        fns = {fn for fn, _ in _broad_handlers_without_raise(SRC_ROOT / rel)}
        if fns:
            present.setdefault(rel, set()).update(fns)
    for rel, fns in ALLOWED_SWALLOW_SITES.items():
        for fn in fns:
            assert fn in present.get(rel, set()), (
                f"stale allowlist entry {rel}:{fn} — the handler no longer "
                "exists or now re-raises; remove it"
            )


async def test_chunk_reader_propagates_graph_interrupt() -> None:
    """A GraphInterrupt raised by the underlying iterator must propagate."""

    async def _gen():
        yield ("ns", "messages", [SimpleNamespace()])
        raise GraphInterrupt((Interrupt(value={"type": "ask_user", "questions": ["q"]}, id="i1"),))

    reader = GraphStreamChunkReader(_gen(), idle_timeout=0)
    chunks = []
    with pytest.raises(GraphInterrupt):
        while True:
            chunks.append(await reader.read_next())
    assert chunks, "the chunk before the interrupt must have been delivered"


async def test_chunk_reader_propagates_graph_interrupt_after_heartbeat() -> None:
    """Heartbeat sentinels must not mask a later interrupt."""
    import asyncio

    from soothe.sloop.engine.execute.graph_interrupt import _STREAM_HEARTBEAT_SENTINEL

    async def _gen():
        yield ("ns", "messages", [SimpleNamespace()])
        # Must outlive the reader's first 0.5s poll boundary so a heartbeat
        # sentinel is emitted before the interrupt surfaces.
        await asyncio.sleep(0.8)
        raise GraphInterrupt((Interrupt(value={"action_requests": [{"name": "x"}]}, id="i2"),))

    reader = GraphStreamChunkReader(_gen(), idle_timeout=0, heartbeat_interval=0.05)
    chunk = await reader.read_next()
    assert isinstance(chunk, tuple)
    sentinel = await reader.read_next()
    assert sentinel is _STREAM_HEARTBEAT_SENTINEL
    with pytest.raises(GraphInterrupt):
        await reader.read_next()
