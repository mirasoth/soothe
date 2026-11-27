"""Unit tests for image attachment validation and workspace landing."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from soothe_daemon.services.image_understanding import (
    ATTACHMENTS_DIR,
    land_attachments,
    normalize_mime_type,
    validate_and_normalize_image_attachments,
)

TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_normalize_mime_type_jpg_alias() -> None:
    assert normalize_mime_type("image/jpg") == "image/jpeg"


def test_validate_attachments_empty() -> None:
    out, err = validate_and_normalize_image_attachments(None)
    assert out == [] and err is None


def test_validate_attachments_valid_png() -> None:
    out, err = validate_and_normalize_image_attachments(
        [{"mime_type": "image/png", "data": TINY_PNG_B64}]
    )
    assert err is None
    assert len(out) == 1
    assert out[0]["mime_type"] == "image/png"
    assert out[0]["data"] == TINY_PNG_B64


def test_validate_attachments_invalid_mime() -> None:
    out, err = validate_and_normalize_image_attachments(
        [{"mime_type": "image/svg+xml", "data": TINY_PNG_B64}]
    )
    assert out == []
    assert err is not None


def test_validate_attachments_bad_base64() -> None:
    out, err = validate_and_normalize_image_attachments(
        [{"mime_type": "image/png", "data": "not!!!base64"}]
    )
    assert out == []
    assert "base64" in (err or "").lower()


def test_land_attachments_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    raw = base64.b64decode(TINY_PNG_B64)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    attachments = [
        {"mime_type": "image/png", "data": TINY_PNG_B64},
        {"mime_type": "image/jpeg", "data": TINY_PNG_B64},
    ]

    paths = land_attachments(tmp_path, attachments)
    assert paths[0] == f"{ATTACHMENTS_DIR}/{digest}.png"
    assert paths[1] == f"{ATTACHMENTS_DIR}/{digest}.jpg"
    for rel in paths:
        assert (tmp_path / rel).read_bytes() == raw

    # Re-landing the same payloads is a no-op that returns the same paths.
    assert land_attachments(tmp_path, attachments) == paths
