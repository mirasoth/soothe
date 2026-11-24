"""Tests for the Skillify background indexer hash-cache semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from soothe.skillify.models import SkillRecord

from soothe_daemon.skillify.indexer import SkillIndexer

EMBEDDING_DIMS = 1536


class _FakeWarehouse:
    """Minimal warehouse stand-in returning a fixed record set."""

    def __init__(self, records: list[SkillRecord]) -> None:
        self._records = records

    def scan(self) -> list[SkillRecord]:
        return list(self._records)


class _FakeStore:
    """Vector store stand-in that can be told to fail inserts once."""

    def __init__(self, fail_first_insert: bool = False) -> None:
        self.inserted: list[str] = []
        self.deleted: list[str] = []
        self._fail_first_insert = fail_first_insert

    async def create_collection(self, **kwargs: Any) -> None:
        pass

    async def insert(
        self,
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        if self._fail_first_insert:
            self._fail_first_insert = False
            raise RuntimeError("insert unavailable")
        self.inserted.extend(ids)

    async def delete(self, id: str) -> None:
        self.deleted.append(id)

    async def list_records(self, limit: int = 10000) -> list[Any]:
        return []


class _FlakyEmbeddings:
    """Embeddings that raise on the first call, then succeed."""

    def __init__(self) -> None:
        self.calls = 0

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("embedding provider unavailable")
        return [[0.1] * EMBEDDING_DIMS for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        raise NotImplementedError


def _record(skill_id: str = "s1", content_hash: str = "h1") -> SkillRecord:
    return SkillRecord(
        id=skill_id,
        name="Skill",
        description="desc",
        path="skills/skill",
        tags=[],
        indexed_at=datetime.now(UTC),
        content_hash=content_hash,
    )


def _indexer(
    records: list[SkillRecord],
    store: _FakeStore,
    embeddings: Any,
) -> SkillIndexer:
    return SkillIndexer(
        warehouse=_FakeWarehouse(records),  # type: ignore[arg-type]
        vector_store=store,  # type: ignore[arg-type]
        embeddings=embeddings,
        embedding_dims=EMBEDDING_DIMS,
    )


@pytest.mark.asyncio
async def test_failed_embed_is_retried_on_next_pass() -> None:
    """A record whose embedding failed must not be cached as upserted."""
    store = _FakeStore()
    embeddings = _FlakyEmbeddings()
    indexer = _indexer([_record()], store, embeddings)

    stats_first = await indexer.run_once()
    assert stats_first["new"] == 1
    assert store.inserted == []
    assert "s1" not in indexer._hash_cache

    stats_second = await indexer.run_once()
    assert stats_second["new"] == 1  # retried, not skipped
    assert store.inserted == ["s1"]
    assert indexer._hash_cache["s1"] == "h1"

    stats_third = await indexer.run_once()
    assert stats_third["new"] == 0
    assert stats_third["changed"] == 0


@pytest.mark.asyncio
async def test_failed_upsert_is_retried_on_next_pass() -> None:
    """A record whose vector-store insert failed must be retried next pass."""

    class _OkEmbeddings:
        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * EMBEDDING_DIMS for _ in texts]

        async def aembed_query(self, text: str) -> list[float]:
            raise NotImplementedError

    store = _FakeStore(fail_first_insert=True)
    indexer = _indexer([_record()], store, _OkEmbeddings())

    stats_first = await indexer.run_once()
    assert stats_first["new"] == 1
    assert store.inserted == []
    assert "s1" not in indexer._hash_cache

    stats_second = await indexer.run_once()
    assert stats_second["new"] == 1
    assert store.inserted == ["s1"]
    assert indexer._hash_cache["s1"] == "h1"


@pytest.mark.asyncio
async def test_successful_upsert_is_cached() -> None:
    """A successfully upserted record is cached and not re-embedded."""
    store = _FakeStore()
    indexer = _indexer([_record()], store, _FlakyEmbeddings())

    await indexer.run_once()  # first call fails (flaky embeddings)
    stats = await indexer.run_once()
    assert stats["new"] == 1
    assert store.inserted == ["s1"]

    stats_third = await indexer.run_once()
    assert stats_third["new"] == 0
    assert stats_third["changed"] == 0
    assert len(store.inserted) == 1
