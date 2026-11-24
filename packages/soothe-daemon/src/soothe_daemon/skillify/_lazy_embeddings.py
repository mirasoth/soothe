"""Per-event-loop lazy embedding instances shared by indexer and retriever."""

from __future__ import annotations

import asyncio
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.embeddings import Embeddings


class LazyEmbeddings:
    """Wrapper that creates fresh embedding instances per event loop.

    Keeps one instance per live event loop as ``(loop weakref, instance)``
    pairs; dead loops are pruned on access so instances are never bound to
    (or returned for) a garbage-collected loop, and no loop-id reuse can
    hand back a stale instance.
    """

    def __init__(self, factory: Callable[[], Embeddings]) -> None:
        self._factory = factory
        self._entries: list[tuple[weakref.ref[asyncio.AbstractEventLoop], Embeddings]] = []

    def _get_instance(self) -> Embeddings:
        loop = asyncio.get_running_loop()
        found: Embeddings | None = None
        alive: list[tuple[weakref.ref[asyncio.AbstractEventLoop], Embeddings]] = []
        for loop_ref, instance in self._entries:
            current = loop_ref()
            if current is None:
                continue  # prune dead loop
            if current is loop:
                found = instance
            alive.append((loop_ref, instance))
        self._entries = alive
        if found is None:
            found = self._factory()
            self._entries.append((weakref.ref(loop), found))
        return found

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._get_instance().aembed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await self._get_instance().aembed_query(text)
