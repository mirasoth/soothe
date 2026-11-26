"""Tests for the sync backend factory security (SSRF prevention).

The scheme allowlist lives in `soothe.workspace.sync.factory` —
`construct_sync_backend` is the only URI entry point for sync sources.
"""

from __future__ import annotations

import pytest


class TestConstructSyncBackend:
    """Factory rejects disallowed URI schemes."""

    def test_rejects_file_scheme(self) -> None:
        from soothe.workspace.sync.factory import construct_sync_backend

        with pytest.raises(ValueError, match="unsupported.*scheme"):
            construct_sync_backend("file:///etc/passwd")

    def test_rejects_sftp_scheme(self) -> None:
        from soothe.workspace.sync.factory import construct_sync_backend

        with pytest.raises(ValueError, match="unsupported.*scheme"):
            construct_sync_backend("sftp://host/path")

    def test_rejects_http_scheme(self) -> None:
        from soothe.workspace.sync.factory import construct_sync_backend

        with pytest.raises(ValueError, match="unsupported.*scheme"):
            construct_sync_backend("http://localhost:8080/")

    def test_rejects_no_scheme(self) -> None:
        from soothe.workspace.sync.factory import construct_sync_backend

        with pytest.raises(ValueError, match="no scheme"):
            construct_sync_backend("/local/path")

    def test_rejects_memory_scheme(self) -> None:
        from soothe.workspace.sync.factory import construct_sync_backend

        with pytest.raises(ValueError, match="unsupported.*scheme"):
            construct_sync_backend("memory://test/")
