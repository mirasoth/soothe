"""Engine-reserved names inside the agent workspace.

Single source of truth for the skip lists shared by the materialize /
reconcile passes and the dirty tracker. Keep the values aligned with the
nano-side exclusions: `.soothe` is the one agent-plane private namespace
(nano runtime + host engine state, including the backend manifest key)
and is pinned by a contract test in each repo — cross-repo twins cannot
share an import.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

# Default subtrees pre-created at open and watched by the dirty tracker.
DEFAULT_WATCHED_SUBDIRS = ("input", "working", "output")

# Prefixes never materialized, never reconciled away, and never tracked: the
# private `.soothe` namespace (framework/engine state, backend manifest key)
# plus VCS/bytecode noise.
ENGINE_SKIP_PREFIXES = (".soothe/", "__pycache__/", ".git/")

# The same reserved names as directory components (dirty-tracker walk filter).
ENGINE_SKIP_DIRS = frozenset(prefix.rstrip("/") for prefix in ENGINE_SKIP_PREFIXES)


def normalize_watched_subdirs(
    watched_subdirs: Sequence[str] | None,
) -> tuple[str, ...]:
    """Normalize configured watched subdirs; `None` keeps the engine defaults.

    Entries are slash-separated workspace-relative paths (e.g. `deliverables`).
    Blank entries are dropped and duplicates removed (order preserved). Unsafe
    entries — absolute paths, backslashes, or traversal segments — raise
    `ValueError` so a bad deployment config fails loud at workspace open.

    Args:
        watched_subdirs: Configured entries, or `None` for the defaults.

    Returns:
        Normalized tuple of watched subdir paths.
    """
    if watched_subdirs is None:
        return DEFAULT_WATCHED_SUBDIRS
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in watched_subdirs:
        entry = str(raw).strip()
        if not entry:
            continue
        if "\\" in entry or PurePosixPath(entry).is_absolute():
            raise ValueError(f"unsafe watched subdir: {raw!r}")
        parts = tuple(p for p in PurePosixPath(entry).parts if p not in ("", "."))
        if not parts or ".." in parts:
            raise ValueError(f"unsafe watched subdir: {raw!r}")
        joined = "/".join(parts)
        if joined not in seen:
            seen.add(joined)
            normalized.append(joined)
    if not normalized:
        return DEFAULT_WATCHED_SUBDIRS
    return tuple(normalized)


def merge_watched_subdirs(
    configured: Sequence[str] | None,
) -> tuple[str, ...]:
    """Return the engine defaults plus configured extras (dedup, order kept).

    The manager-facing composition: `None` yields exactly the defaults, and
    configured entries extend them instead of replacing them.

    Args:
        configured: Extra workspace-relative subtrees, or `None`.

    Returns:
        Defaults followed by any configured extras not already present.
    """
    extras = normalize_watched_subdirs(configured)
    if extras == DEFAULT_WATCHED_SUBDIRS:
        return DEFAULT_WATCHED_SUBDIRS
    merged = list(DEFAULT_WATCHED_SUBDIRS)
    for entry in extras:
        if entry not in merged:
            merged.append(entry)
    return tuple(merged)
