"""Audit store backend selection: unified persistence compliance."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from soothe.logging.audit_store import (
    SqliteAuditStore,
    configure_audit_store,
    reset_audit_store_for_tests,
)
from soothe.logging.audit_store_postgres import PostgresAuditStore


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_audit_store_for_tests()
    yield
    reset_audit_store_for_tests()


def _config(backend: str, databases: dict[str, str] | None = None) -> MagicMock:
    config = MagicMock()
    config.persistence.default_backend = backend
    config.persistence.postgres_databases = databases or {}
    return config


def test_sqlite_backend_selected_when_default_backend_is_sqlite() -> None:
    store = configure_audit_store(_config("sqlite"))
    assert isinstance(store, SqliteAuditStore)


def test_postgres_backend_selected_when_audit_key_registered() -> None:
    config = _config("postgresql", {"audit": "soothe_audit"})
    config.resolve_postgres_dsn_for_database.return_value = (
        "postgresql://postgres@localhost:5432/soothe_audit"
    )

    store = configure_audit_store(config)

    assert isinstance(store, PostgresAuditStore)
    assert store.dsn == "postgresql://postgres@localhost:5432/soothe_audit"


def test_postgres_missing_audit_key_fails_loud() -> None:
    """Unified persistence rule: never silently fall back to SQLite in PG mode."""
    config = _config(
        "postgresql",
        {"checkpoints": "soothe_checkpoints", "metadata": "soothe_metadata"},
    )
    config.resolve_postgres_dsn_for_database.side_effect = ValueError(
        "db_key is not in postgres_databases mapping: 'audit'"
    )

    with pytest.raises(ValueError, match="audit"):
        configure_audit_store(config)
