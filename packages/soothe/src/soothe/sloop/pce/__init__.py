"""Proactive Coverage Evaluation (RFC-SLP-005).

Signal-only goal-DAG observer: typesafe-first verdicts, fingerprint
cache, LLM escalation, CoverageSignal records consumed by ROOT_EVAL and
finalize. Never drives execution directly.
"""

from soothe.sloop.pce.events import (
    PceEvent,
    PceEventQueue,
    emit_pce_event,
    register_loop,
    reset_pce_registry,
    unregister_loop,
)
from soothe.sloop.pce.monitor import PceMonitor, start_pce_monitor, stop_pce_monitor
from soothe.sloop.pce.projection import PceSnapshot, fingerprint, snapshot_goal_dag
from soothe.sloop.pce.typesafe_pce import PceVerdict

__all__ = [
    "PceEvent",
    "PceEventQueue",
    "PceMonitor",
    "PceSnapshot",
    "PceVerdict",
    "emit_pce_event",
    "fingerprint",
    "register_loop",
    "reset_pce_registry",
    "snapshot_goal_dag",
    "start_pce_monitor",
    "stop_pce_monitor",
    "unregister_loop",
]
