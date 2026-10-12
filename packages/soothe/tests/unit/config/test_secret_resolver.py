"""Direct tests for the SootheConfig.secret_resolver property (soothe variant).

Validates the fix for issue #22 problem 1 on the host-package SootheConfig:
the property exists, is callable, and resolves ``${ENV_VAR}`` so the daemon
(``server/core.py``) and per-thread runner (``runner/_thread_manager.py``) no
longer raise ``AttributeError``.
"""

from __future__ import annotations

import pytest

from soothe.config.settings import SootheConfig


def test_secret_resolver_is_callable() -> None:
    config = SootheConfig()
    assert callable(config.secret_resolver)


def test_secret_resolver_expands_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SOOTHE_SECRET_VAR", "resolved-value")
    assert SootheConfig().secret_resolver("${TEST_SOOTHE_SECRET_VAR}") == "resolved-value"


def test_secret_resolver_leaves_unresolved_intact() -> None:
    assert (
        SootheConfig().secret_resolver("${DEFINITELY_MISSING_VAR_XYZ}")
        == "${DEFINITELY_MISSING_VAR_XYZ}"
    )


def test_secret_resolver_handles_embedded_and_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOOTHE_VAR_A", "alpha")
    monkeypatch.setenv("SOOTHE_VAR_B", "beta")
    resolver = SootheConfig().secret_resolver
    assert resolver("Bearer ${SOOTHE_VAR_A}") == "Bearer alpha"
    assert resolver("${SOOTHE_VAR_A}/${SOOTHE_VAR_B}") == "alpha/beta"


def test_thread_manager_wiring_does_not_raise_attribute_error() -> None:
    """The exact access pattern in _thread_manager._ensure_mcp_session works.

    Regression for the AttributeError reported in issue #22 —
    ``self._config.secret_resolver`` must resolve to a callable, not raise.
    """
    config = SootheConfig()
    resolver = config.secret_resolver
    assert callable(resolver)
    # The registry constructor stores the resolver; it must accept it.
    from soothe_nano.mcp.mcp_registry import MCPRegistry

    MCPRegistry(servers=[], secret_resolver=resolver)
