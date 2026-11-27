"""Attachment landing into agent workspaces.

Hosts (soothe-daemon, flowjet) land wire-format image attachments into the
turn workspace at a content-addressed path before the agent runs, so the
vision tools can address them and the files persist with the workspace.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

__all__ = [
    "ATTACHMENTS_DIR",
    "attachment_rel_path",
    "land_attachments",
    "landed_paths_note",
]

logger = logging.getLogger(__name__)

ATTACHMENTS_DIR = "input/attachments"
"""Workspace-relative directory where wire attachments are landed."""

_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}

_LANDED_NOTE = (
    "--- Image attachments ---\n"
    "The images sent with this message are saved in the workspace at:\n"
    "{paths}\n"
    "For targeted inspection (specific text, regions, or follow-up questions), "
    "call the `analyze_image` or `extract_text_from_image` tools with these paths.\n"
    "---"
)


def attachment_rel_path(data: bytes, mime_type: str) -> str:
    """Content-addressed workspace-relative path for an attachment payload.

    Args:
        data: Decoded attachment bytes.
        mime_type: Normalized MIME type (extension derived from it).

    Returns:
        ``input/attachments/<sha256[:16]>.<ext>`` path relative to the workspace.
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    ext = _MIME_EXT.get(mime_type, "bin")
    return f"{ATTACHMENTS_DIR}/{digest}.{ext}"


def land_attachments(workspace_root: Path, attachments: list[dict[str, str]]) -> list[str]:
    """Write attachments into ``<workspace>/input/attachments/`` (idempotent).

    Args:
        workspace_root: Agent workspace root for the turn.
        attachments: List of ``{"mime_type": ..., "data": <base64>}`` dicts.

    Returns:
        Workspace-relative landed paths, one per attachment, in input order.
    """
    paths: list[str] = []
    for att in attachments:
        data = base64.b64decode(att["data"])
        rel = attachment_rel_path(data, att["mime_type"])
        target = workspace_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        paths.append(rel)
    if paths:
        logger.info("Landed %d attachment(s) under %s", len(paths), ATTACHMENTS_DIR)
    return paths


def landed_paths_note(paths: list[str]) -> str:
    """Agent-facing note listing the landed attachment paths."""
    rendered = "\n".join(f"- {path}" for path in paths)
    return _LANDED_NOTE.format(paths=rendered)
