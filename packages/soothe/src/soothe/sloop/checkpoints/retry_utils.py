"""Host aliases for shared persistence retry helpers."""

import sqlite3

from soothe_nano.persistence.retry_utils import ConnectionRetry

_RECOVERABLE_SQLITE_MESSAGES = (
    "unable to open database file",
    "database is locked",
    "database table is locked",
    "disk i/o error",
    "database disk image is malformed",
)


def is_recoverable_sqlite_error(exc: Exception) -> bool:
    """Return True for transient SQLite failures worth retrying.

    Detects `OperationalError` / `DatabaseError` whose message matches
    known transient patterns (file-lock contention, WAL races, I/O hiccup).

    Args:
        exc: Exception raised during SQLite connect or setup.

    Returns:
        True when a short backoff retry is likely to succeed.
    """
    if not isinstance(exc, (sqlite3.OperationalError, sqlite3.DatabaseError)):
        return False
    text = str(exc).lower()
    return any(needle in text for needle in _RECOVERABLE_SQLITE_MESSAGES)


__all__ = [
    "ConnectionRetry",
    "is_recoverable_sqlite_error",
]
