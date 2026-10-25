"""Conftest for daemon integration tests — exposes shared fixtures and hooks."""

from __future__ import annotations

import gc
import resource

import pytest

from tests.integration.daemon_fixtures import (  # noqa: F401
    integration_config,
    llm_idle_timeout,
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_configure,
    requires_llm_api,
    requires_postgresql,
    soothe_runner,
    temp_workspace,
    test_config,
    web_enabled_config,
)
from tests.integration.test_timeouts import (  # noqa: F401
    get_timeout,
    integration_llm_idle_timeout,
    timeout_ack,
    timeout_config_reload,
    timeout_default,
    timeout_delete,
    timeout_event_poll,
    timeout_event_wait,
    timeout_llm,
    timeout_postgres,
    timeout_ray,
    timeout_subscribe,
    timeout_unit_wait,
)


@pytest.fixture(autouse=True)
def _raise_fd_limit_and_gc():
    """Raise the per-process FD soft limit and GC between tests.

    Integration tests spawn many short-lived WebSocket connections and daemon
    subprocesses.  On macOS the default soft limit is 256, which is too low
    for the full suite — FDs leaked by a timed-out ``daemon.stop()`` accumulate
    and eventually cause ``[Errno 24] Too many open files`` mid-suite.  Raising
    the soft limit to the hard limit gives headroom, and ``gc.collect()``
    releases closed-socket objects that Python hasn't finalized yet.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except (ValueError, OSError):
        pass
    yield
    gc.collect()
