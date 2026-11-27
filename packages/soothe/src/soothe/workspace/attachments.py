"""Attachment landing into agent workspaces.

Hosts (soothe-daemon, flowjet) land wire-format image attachments into the
turn workspace at a content-addressed path before the agent runs, so the
vision tools can address them. Attachments are engine-local cache: they land
under the `.soothe/` private namespace, so they are never tracked by the
workspace sync (no upload, no deletion mirroring) and are hidden from agent
file listings — the landed-paths note gives the agent explicit paths, which
the vision tools read directly. Retention is TTL pruning only (7-day
default, `0` disables).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path

__all__ = [
    "ATTACHMENTS_DIR",
    "DEFAULT_ATTACHMENT_TTL_S",
    "attachment_rel_path",
    "land_attachments",
    "landed_paths_note",
    "prune_attachments",
]

logger = logging.getLogger(__name__)

ATTACHMENTS_DIR = ".soothe/cache/attachments"
"""Workspace-relative directory where wire attachments are landed.

Engine-local cache under the `.soothe/` private namespace (IG-CORE-013):
excluded from workspace sync (`ENGINE_SKIP_PREFIXES`) and hidden from agent
file listings; TTL pruning is the only GC.
"""

DEFAULT_ATTACHMENT_TTL_S = 7 * 24 * 60 * 60
"""Default age after which landed attachments are pruned (7 days)."""

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
        ``.soothe/cache/attachments/<sha256[:16]>.<ext>`` path relative to the workspace.
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    ext = _MIME_EXT.get(mime_type, "bin")
    return f"{ATTACHMENTS_DIR}/{digest}.{ext}"


def land_attachments(workspace_root: Path, attachments: list[dict[str, str]]) -> list[str]:
    """Write attachments into ``<workspace>/.soothe/cache/attachments/`` (idempotent).

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


def prune_attachments(workspace_root: Path, *, max_age_s: int | None) -> list[str]:
    """Delete attachments older than *max_age_s* from the landed directory.

    Run before landing new attachments. The cache is engine-local (never
    synced), so pruning is pure local GC — no deletion mirroring.

    Args:
        workspace_root: Agent workspace root.
        max_age_s: Maximum file age in seconds; `None` or ``<= 0`` disables
            pruning.

    Returns:
        Workspace-relative paths that were removed.
    """
    if not max_age_s or max_age_s <= 0:
        return []
    attachments_dir = workspace_root / ATTACHMENTS_DIR
    if not attachments_dir.is_dir():
        return []
    now = time.time()
    removed: list[str] = []
    for file in attachments_dir.iterdir():
        if not file.is_file():
            continue
        try:
            age = now - file.stat().st_mtime
        except OSError:
            continue
        if age > max_age_s:
            try:
                file.unlink()
            except OSError:
                logger.warning("Could not prune stale attachment %s", file, exc_info=True)
                continue
            removed.append(f"{ATTACHMENTS_DIR}/{file.name}")
    if removed:
        logger.info("Pruned %d stale attachment(s)", len(removed))
    return removed
