"""Tests for shared attachment landing."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from soothe.workspace.attachments import (
    ATTACHMENTS_DIR,
    attachment_rel_path,
    land_attachments,
    landed_paths_note,
)

TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_attachment_rel_path_is_content_addressed() -> None:
    raw = base64.b64decode(TINY_PNG_B64)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    assert attachment_rel_path(raw, "image/png") == f"{ATTACHMENTS_DIR}/{digest}.png"
    assert attachment_rel_path(raw, "image/jpeg") == f"{ATTACHMENTS_DIR}/{digest}.jpg"


def test_land_attachments_writes_and_is_idempotent(tmp_path: Path) -> None:
    raw = base64.b64decode(TINY_PNG_B64)
    attachments = [
        {"mime_type": "image/png", "data": TINY_PNG_B64},
        {"mime_type": "image/jpeg", "data": TINY_PNG_B64},
    ]

    paths = land_attachments(tmp_path, attachments)
    assert len(paths) == 2
    for rel in paths:
        assert (tmp_path / rel).read_bytes() == raw

    # Re-landing the same payloads is a no-op that returns the same paths.
    assert land_attachments(tmp_path, attachments) == paths


def test_landed_paths_note_lists_paths_and_tool_hint() -> None:
    note = landed_paths_note([f"{ATTACHMENTS_DIR}/ab12cd34ef56.png"])
    assert f"{ATTACHMENTS_DIR}/ab12cd34ef56.png" in note
    assert "analyze_image" in note
    assert "extract_text_from_image" in note


def test_attachments_dir_literal_is_pinned() -> None:
    """Cross-host contract: the layout literal must not drift (IG-CORE-012 D2)."""
    assert ATTACHMENTS_DIR == "input/attachments"
