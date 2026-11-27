"""Fold live card lists and repair goal segments against boundary markers."""

from __future__ import annotations

import uuid

from soothe.display.transcript_types import MessageData, MessageType


def _last_card_per_id(cards: list[MessageData]) -> list[MessageData]:
    """Keep the last occurrence of each card id while preserving order."""
    index_by_id: dict[str, int] = {}
    ordered: list[MessageData] = []
    for card in cards:
        if card.id in index_by_id:
            ordered[index_by_id[card.id]] = card
        else:
            index_by_id[card.id] = len(ordered)
            ordered.append(card)
    return ordered


def fold_display_cards(cards: list[MessageData]) -> list[MessageData]:
    """Collapse streaming duplicates; keep terminal card state per id."""
    if not cards:
        return []
    from soothe.display.card_binder import merge_consecutive_assistant_cards

    return merge_consecutive_assistant_cards(_last_card_per_id(cards))


def repair_goal_segment_cards(
    cards: list[MessageData],
    *,
    goal_text: str,
    goal_completion: str,
    status: str,
    report_card_id: str,
) -> list[MessageData]:
    """Read-repair one folded goal segment against its boundary marker.

    Guarantees:

    * the segment opens with a user card carrying the goal text (synthesized
      when the bound projection lacks one);
    * a completed segment with a non-empty completion body ends on the full
      report card — truncated streaming report cards are replaced by the
      marker text, not merely supplemented.

    Cancelled segments (``status != "completed"`` or empty completion body)
    keep their folded cards as-is.
    """
    cards = _ensure_user_card(cards, goal_text)
    text = goal_completion.strip()
    if not text or status != "completed":
        return cards
    last = cards[-1] if cards else None
    if (
        last is not None
        and last.type == MessageType.ASSISTANT
        and last.loop_output_phase == "goal_completion"
        and (last.content or "").strip() == text
    ):
        return cards
    kept = [
        card
        for card in cards
        if not (card.type == MessageType.ASSISTANT and card.loop_output_phase == "goal_completion")
    ]
    report = MessageData(
        type=MessageType.ASSISTANT,
        content=text,
        id=report_card_id,
        is_streaming=False,
        loop_output_phase="goal_completion",
    )
    return [*kept, report]


def _ensure_user_card(cards: list[MessageData], goal_text: str) -> list[MessageData]:
    text = goal_text.strip()
    if not text:
        return cards
    for card in cards:
        if card.type == MessageType.USER and card.content.strip():
            return cards
    user = MessageData(type=MessageType.USER, content=text, id=f"msg-{uuid.uuid4().hex[:8]}")
    return [user, *cards]


__all__ = [
    "fold_display_cards",
    "repair_goal_segment_cards",
]
