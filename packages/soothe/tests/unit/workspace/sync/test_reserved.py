"""Contract tests for the engine-reserved workspace names.

The reserved names cannot be shared across repos (nano is a separate
package), so each side pins the literal with a twin test like this one:
`.soothe` is the single agent-plane private namespace (framework/engine
state, backend manifest key). Changing these values is a cross-repo
contract change — update the nano-side exclusions in the same change.
"""

from soothe.workspace.sync.reserved import (
    DEFAULT_WATCHED_SUBDIRS,
    ENGINE_SKIP_DIRS,
    ENGINE_SKIP_PREFIXES,
    merge_watched_subdirs,
    normalize_watched_subdirs,
)


class TestReservedNameContract:
    def test_private_namespace_is_soothe(self) -> None:
        assert ".soothe/" in ENGINE_SKIP_PREFIXES

    def test_skip_prefixes_are_the_pinned_set(self) -> None:
        assert ENGINE_SKIP_PREFIXES == (".soothe/", "__pycache__/", ".git/")

    def test_skip_dirs_derive_from_prefixes(self) -> None:
        assert ENGINE_SKIP_DIRS == {".soothe", "__pycache__", ".git"}

    def test_default_watched_subdirs_are_the_pinned_set(self) -> None:
        assert DEFAULT_WATCHED_SUBDIRS == ("input", "working", "output")

    def test_manifest_lives_in_the_private_namespace(self) -> None:
        from fsspec.implementations.memory import MemoryFileSystem

        from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend

        backend = FsspecSyncBackend(fs=MemoryFileSystem(), root="/run", max_workers=1)
        assert backend._manifest_path("run-001") == "/run/.soothe/manifest.json"


class TestMergeSemantics:
    def test_none_is_exactly_the_defaults(self) -> None:
        assert merge_watched_subdirs(None) == DEFAULT_WATCHED_SUBDIRS

    def test_normalizer_none_is_exactly_the_defaults(self) -> None:
        assert normalize_watched_subdirs(None) == DEFAULT_WATCHED_SUBDIRS
