"""Unit tests for the MS Teams webhook body size bound (HTTP 413)."""

from __future__ import annotations

from soothe_daemon.channels.msteams import MSTEAMS_MAX_BODY_BYTES, _read_post_body


class FakeRfile:
    """Minimal input-stream double recording read() calls."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.read_calls: list[int] = []

    def read(self, n: int) -> bytes:
        self.read_calls.append(n)
        return self.payload[:n] if n > 0 else b""


def test_oversized_body_is_rejected_without_reading():
    rfile = FakeRfile(b"x")
    assert _read_post_body(rfile, MSTEAMS_MAX_BODY_BYTES + 1) is None
    # Nothing may be read off the wire when the declared length is over the cap.
    assert rfile.read_calls == []


def test_body_at_limit_is_read():
    payload = b"a" * 8
    rfile = FakeRfile(payload)
    assert _read_post_body(rfile, len(payload)) == payload
    assert rfile.read_calls == [len(payload)]


def test_empty_body_defaults_to_json_object():
    rfile = FakeRfile(b"")
    assert _read_post_body(rfile, 0) == b"{}"


def test_limit_is_about_one_mib():
    assert 512 * 1024 <= MSTEAMS_MAX_BODY_BYTES <= 4 * 1024 * 1024
