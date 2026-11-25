"""Docked approval bar — pinned multi-approval UI (IG-CLNT-005).

Hosts pending clarification/approval cards at the bottom of the message area,
directly above the queued-goals bar. Unlike the transcript, the dock is never
flushed by new messages; on a decision the submitted card relocates into the
transcript as the record.

Ordering is head-first: only the head card is interactive; queued cards render
as one-line waiting rows until the resume turn for the previous answer
completes (`promote_next`). This matches the relay inbox's FIFO answer pairing
without any wire changes.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from textual.containers import Vertical
from textual.widgets import Static

__all__ = ["ApprovalDockBar"]


class _WaitingRow(Static):
    """One-line placeholder for a queued (non-head) approval card."""

    DEFAULT_CSS = """
    _WaitingRow {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """


def _card_label(widget: Any) -> str:
    """Short label for a waiting row (tool name for approvals, else question)."""
    origin = str(getattr(widget, "_origin_node", "") or "")
    questions = list(getattr(widget, "_questions", []) or [])
    if not questions:
        return "approval"
    q0 = questions[0]
    header = q0.get("header", "") if isinstance(q0, dict) else str(q0)
    if origin == "tool_approval":
        from soothe_cli.tui.widgets.messages.structured_ask_user import (
            tool_label_from_header,
        )

        return tool_label_from_header(header)
    question = q0.get("question", "") if isinstance(q0, dict) else str(q0)
    question = question.strip() or header.strip() or "approval"
    return question if len(question) <= 60 else question[:57] + "…"


class ApprovalDockBar(Vertical):
    """Pinned approval dock: head card + queued waiting rows.

    Hidden while empty. Cards are `StructuredAskUserWidget` instances created
    by `_mount_manual_clarification_input`; the dock owns their DOM placement
    but not their lifecycle decisions (submit/abandon bubble to the app).
    """

    DEFAULT_CSS = """
    ApprovalDockBar {
        height: auto;
        max-height: 40vh;
        display: none;
        border-top: solid $border-blurred;
        background: $surface;
    }
    ApprovalDockBar #approval-dock-header {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(**kwargs)
        self._cards: list[Any] = []
        """Pending cards in arrival order (head first)."""
        self._rows: dict[int, _WaitingRow] = {}
        """Waiting row per queued (non-head) card, keyed by id(card)."""

    # ── State ────────────────────────────────────────────────────────

    def compose(self):  # noqa: ANN201
        """Header row; cards and waiting rows mount after it."""
        yield Static("", id="approval-dock-header")

    @property
    def pending_count(self) -> int:
        """Number of pending approval cards (head + waiting)."""
        return len(self._cards)

    @property
    def has_pending(self) -> bool:
        """True while at least one approval awaits a decision."""
        return bool(self._cards)

    @property
    def head_widget(self) -> Any | None:
        """The interactive head card, or None when the dock is empty."""
        return self._cards[0] if self._cards else None

    # ── Mutations ────────────────────────────────────────────────────

    async def push_card(self, widget: Any) -> bool:
        """Add a pending card. First card becomes the interactive head.

        Returns:
        False when the card is already docked (duplicate push); True otherwise.
        """
        if any(card is widget for card in self._cards):
            return False
        self._cards.append(widget)
        if len(self._cards) == 1:
            await self.mount(widget)
        else:
            row = _WaitingRow(
                f"· {_card_label(widget)} — waiting (answer #1 first)",
                classes="approval-dock-waiting",
            )
            self._rows[id(widget)] = row
            await self.mount(row)
        self._sync()
        return True

    async def pop_card(self, widget: Any) -> bool:
        """Remove a card (or its waiting row). Promotes the next waiting card.

        Returns:
        True when the card was docked and has been removed.
        """
        for i, card in enumerate(self._cards):
            if card is widget:
                del self._cards[i]
                row = self._rows.pop(id(widget), None)
                if row is not None:
                    await row.remove()
                elif card.is_mounted:
                    await card.remove()
                await self.promote_next()
                self._sync()
                return True
        return False

    async def promote_next(self) -> bool:
        """Promote the first waiting card to interactive head (if any).

        Called at turn end so the next queued approval only becomes
        answerable once the previous answer's resume turn has completed.
        """
        if not self._cards or self._cards[0].is_mounted:
            return False
        head = self._cards[0]
        row = self._rows.pop(id(head), None)
        if row is not None:
            await row.remove()
        # Insert directly after the header so the interactive card renders
        # above the remaining waiting rows regardless of mount order.
        with suppress(Exception):
            header = self.query_one("#approval-dock-header")
            await self.mount(head, before=header)
            self._sync()
            return True
        await self.mount(head)
        self._sync()
        return True

    def clear_pending(self) -> None:
        """Remove all cards and waiting rows (/clear, loop switch).

        Sync entry point (called from `clear_live_session_ui`); the DOM
        removal is scheduled on the app's message loop.
        """
        self._cards.clear()
        self._rows.clear()
        with suppress(Exception):
            self.call_next(self._clear_dom)

    async def _clear_dom(self) -> None:
        """Remove docked cards/rows from the DOM (async tail of clear)."""
        with suppress(Exception):
            for child in list(self.children):
                if child.id != "approval-dock-header":
                    await child.remove()
        self._sync()

    # ── Internals ────────────────────────────────────────────────────

    def _sync(self) -> None:
        """Refresh the header label and empty-state visibility."""
        with suppress(Exception):
            if not self._cards:
                self.display = False
                return
            self.display = True
            header = self.query_one("#approval-dock-header", Static)
            n = len(self._cards)
            header.update(
                f"⏸ Tool approvals ({n} pending)" if n > 1 else "⏸ Tool approval (1 pending)"
            )
