"""Goal boundary mutation semantics in the in-memory card ledger fold."""

from __future__ import annotations

from soothe.display.card_ledger import (
    InMemoryCardLedger,
    build_goal_boundary_mutation,
    build_header_mutation,
    card_to_wire_dict,
)
from soothe.display.transcript_types import MessageData, MessageType


def _assistant_card(card_id: str, content: str) -> MessageData:
    return MessageData(
        type=MessageType.ASSISTANT,
        content=content,
        id=card_id,
        loop_output_phase="goal_completion",
    )


def _create(seq: int, card: MessageData) -> object:
    from soothe.display.card_ledger import CardMutation

    return CardMutation(
        seq=seq,
        ts="2026-11-27T00:00:00+00:00",
        op="create",
        card_id=card.id,
        kind=str(card.type),
        data=card_to_wire_dict(card),
    )


def test_build_goal_boundary_mutation_carries_report_text() -> None:
    marker = build_goal_boundary_mutation(
        seq=7,
        goal_index=2,
        goal_id="goal-2",
        goal_text="deploy chirpy",
        status="completed",
        goal_completion_text="I deployed and verified the stack.",
        started_at="2026-11-27T10:00:00+00:00",
        completed_at="2026-11-27T10:05:00+00:00",
        duration_ms=300_000,
        tokens_used=1_234,
    )

    assert marker.op == "goal_boundary"
    assert marker.seq == 7
    assert marker.card_id == "__goal__:2"
    assert marker.kind == "goal_boundary"
    assert marker.data["goal_text"] == "deploy chirpy"
    assert marker.data["status"] == "completed"
    assert marker.data["goal_completion_text"] == "I deployed and verified the stack."
    assert marker.data["duration_ms"] == 300_000
    assert marker.data["tokens_used"] == 1_234


def test_boundary_starts_new_segment_in_fold() -> None:
    ledger = InMemoryCardLedger(loop_id="loop-1")
    ledger.apply(build_header_mutation(loop_id="loop-1", created_by="test"))
    ledger.apply(_create(1, _assistant_card("card-a", "first goal report")))
    ledger.apply(
        build_goal_boundary_mutation(
            seq=2,
            goal_index=0,
            goal_text="goal one",
            status="completed",
            goal_completion_text="first goal report",
        )
    )
    ledger.apply(_create(3, _assistant_card("card-b", "second goal report")))

    # The fold tracks only the live (post-boundary) segment.
    assert [card.content for card in ledger.snapshot()] == ["second goal report"]
    # Boundary records accumulate across segments.
    assert len(ledger.boundaries) == 1
    assert ledger.boundaries[0].data["goal_text"] == "goal one"
    # next_seq continues from the boundary-marked log.
    assert ledger.next_seq == 4


def test_repeated_card_ids_across_segments_do_not_collapse() -> None:
    """Stable binder ids repeat per goal; segment folds stay independent."""
    results: list[list[MessageData]] = []
    for goal in range(2):
        fold = InMemoryCardLedger(loop_id="loop-1")
        fold.apply(build_header_mutation(loop_id="loop-1", created_by="test"))
        fold.apply(_create(1, _assistant_card("card-same", f"report {goal}")))
        results.append(fold.snapshot())

    assert [card.content for card in results[0]] == ["report 0"]
    assert [card.content for card in results[1]] == ["report 1"]


def test_boundary_requires_card_id_like_other_records() -> None:
    from soothe.display.card_ledger import CardMutation

    ledger = InMemoryCardLedger(loop_id="loop-1")
    marker = CardMutation(
        seq=1,
        ts="2026-11-27T00:00:00+00:00",
        op="goal_boundary",
        card_id="",
        kind="goal_boundary",
        data={"goal_text": "x"},
    )
    try:
        ledger.apply(marker)
    except ValueError as exc:
        assert "card_id" in str(exc)
    else:
        raise AssertionError("boundary without card_id must be rejected")
