"""Durable per-loop transcript ledger (SQLite or PostgreSQL via display store).

The persisted mutation log is the single source of truth: card mutations
plus ``goal_boundary`` markers that terminate completed goals. The in-memory
fold tracks only the live segment (cards after the last boundary); earlier
segments stay readable via ``history_segments`` for resume replay.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from soothe.display.card_ledger import (
    CardMutation,
    InMemoryCardLedger,
    build_header_mutation,
    card_to_wire_dict,
    utc_now_iso,
)

from soothe_daemon.display.display_store import get_display_card_store

if TYPE_CHECKING:
    from soothe.display.transcript_types import MessageData

logger = logging.getLogger(__name__)


class LoopCardLedger:
    """Durable transcript ledger for one loop (backend from `get_display_card_store`)."""

    def __init__(
        self,
        *,
        loop_id: str,
        created_by: str = "soothe-daemon",
    ) -> None:
        self._loop_id = loop_id
        self._created_by = created_by
        self._store = get_display_card_store()
        self._inner = InMemoryCardLedger(loop_id=loop_id)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._mutations: list[CardMutation] = []

    @property
    def loop_id(self) -> str:
        return self._loop_id

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def card_count(self) -> int:
        return self._inner.card_count()

    def snapshot(self) -> list[MessageData]:
        return self._inner.snapshot()

    def next_seq(self) -> int:
        return self._inner.next_seq

    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def boundaries(self) -> list[CardMutation]:
        """Goal boundary markers committed so far, in order."""
        return self._inner.boundaries

    async def ensure_loaded(self) -> None:
        """Load mutations from the display card store or initialize a header row."""
        async with self._lock:
            if self._loaded:
                return
            await asyncio.to_thread(self._load_or_initialize_sync)
            self._loaded = True

    def _load_or_initialize_sync(self) -> None:
        mutations = self._store.list_mutations(self._loop_id)
        if not mutations:
            header = build_header_mutation(
                loop_id=self._loop_id,
                created_by=self._created_by,
            )
            self._store.append_mutations(self._loop_id, [header])
            mutations = [header]

        ledger = InMemoryCardLedger(loop_id=self._loop_id)
        for mutation in mutations:
            try:
                ledger.apply(mutation)
            except ValueError as exc:
                logger.warning(
                    "Dropping inconsistent card mutation seq=%d for loop %s: %s",
                    mutation.seq,
                    self._loop_id,
                    exc,
                )
        self._inner = ledger
        self._mutations = mutations

    async def append(self, mutation: CardMutation) -> None:
        if not self._loaded:
            await self.ensure_loaded()
        async with self._lock:
            # Persist before applying so a store failure can never leave the
            # in-memory ledger ahead of disk (memory would otherwise never
            # reload, diverging until restart).
            await asyncio.to_thread(self._store.append_mutations, self._loop_id, [mutation])
            self._inner.apply(mutation)
            self._mutations.append(mutation)

    async def append_many(self, mutations: Iterable[CardMutation]) -> None:
        items = list(mutations)
        if not items:
            return
        if not self._loaded:
            await self.ensure_loaded()
        async with self._lock:
            # Persist before applying: memory must never run ahead of disk.
            await asyncio.to_thread(self._store.append_mutations, self._loop_id, items)
            for mutation in items:
                self._inner.apply(mutation)
            self._mutations.extend(items)

    async def replace_with(self, mutations: Iterable[CardMutation]) -> None:
        """Rewrite the live segment's card mutations in the persisted log.

        Archived mutations (header, boundaries, and the cards folded before
        each boundary) are preserved verbatim; only the live segment's
        mutations are replaced by ``mutations`` with fresh sequence numbers.
        """
        items = list(mutations)
        if not self._loaded:
            await self.ensure_loaded()
        async with self._lock:
            prefix = self._mutations[: self._live_segment_start()]
            seq = max((mutation.seq for mutation in prefix), default=0)
            resequenced: list[CardMutation] = []
            for mutation in items:
                seq += 1
                resequenced.append(dataclasses.replace(mutation, seq=seq))
            all_mutations = [*prefix, *resequenced]
            await asyncio.to_thread(self._store.replace_mutations, self._loop_id, all_mutations)
            ledger = InMemoryCardLedger(loop_id=self._loop_id)
            for mutation in all_mutations:
                ledger.apply(mutation)
            self._inner = ledger
            self._mutations = all_mutations
            self._loaded = True

    def _live_segment_start(self) -> int:
        """Index where the live segment begins in the persisted mutation log."""
        for idx in range(len(self._mutations) - 1, -1, -1):
            if self._mutations[idx].op == "goal_boundary":
                return idx + 1
        # No boundaries: the header is the immutable prefix.
        return min(1, len(self._mutations))

    def history_segments(
        self,
    ) -> tuple[list[tuple[CardMutation, list[MessageData]]], list[MessageData]]:
        """Fold the persisted log into ``(boundary marker, cards)`` segments.

        Returns the archived segments plus the live tail cards (the fold
        after the last boundary). Mutation-level splitting keeps segment
        folds independent, so card ids that repeat across goals never
        collapse into one another.
        """
        boundaries: list[CardMutation] = []
        segment_cards: list[list[MessageData]] = []
        fold = InMemoryCardLedger(loop_id=self._loop_id)
        for mutation in self._mutations:
            if mutation.op == "goal_boundary":
                boundaries.append(mutation)
                segment_cards.append(fold.snapshot())
                fold = InMemoryCardLedger(loop_id=self._loop_id)
                continue
            try:
                fold.apply(mutation)
            except ValueError as exc:
                logger.warning(
                    "Dropping inconsistent card mutation seq=%d for loop %s: %s",
                    mutation.seq,
                    self._loop_id,
                    exc,
                )
        return (
            list(zip(boundaries, segment_cards, strict=True)),
            fold.snapshot(),
        )

    def to_mutations_snapshot(
        self,
        cards: Iterable[MessageData] | None = None,
    ) -> list[CardMutation]:
        """Produce `create` mutations for the current or supplied card state.

        Args:
            cards: Cards to serialize; defaults to the live ledger snapshot.

        Returns:
            One `create` mutation per card in input order.
        """
        snapshot = list(cards) if cards is not None else self._inner.snapshot()
        return [
            CardMutation(
                seq=offset + 1,
                ts=utc_now_iso(),
                op="create",
                card_id=card.id,
                kind=str(card.type),
                data=card_to_wire_dict(card),
            )
            for offset, card in enumerate(snapshot)
        ]


__all__ = ["LoopCardLedger"]
