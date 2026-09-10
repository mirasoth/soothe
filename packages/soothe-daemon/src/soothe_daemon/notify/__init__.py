"""Daemon job lifecycle notify sinks."""

from soothe_daemon.notify.factory import (
    build_notify_dispatcher,
    build_notify_dispatcher_from_config,
)
from soothe_daemon.notify.models import (
    DeliveryResult,
    NotifyIntent,
    NotifyTarget,
)
from soothe_daemon.notify.protocol import NotifyDispatcher, NotifySink

__all__ = [
    "NotifyDispatcher",
    "NotifySink",
    "NotifyTarget",
    "NotifyIntent",
    "DeliveryResult",
    "build_notify_dispatcher",
    "build_notify_dispatcher_from_config",
]
