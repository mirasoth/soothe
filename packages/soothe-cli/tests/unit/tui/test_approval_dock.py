"""Tests for ApprovalDockBar (IG-CLNT-005) and the submit-relocation wiring."""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical

from soothe_cli.tui.widgets.approval_dock import ApprovalDockBar
from soothe_cli.tui.widgets.messages.structured_ask_user import StructuredAskUserWidget


def _card(name: str, idx: int = 0) -> StructuredAskUserWidget:
    return StructuredAskUserWidget(
        step_id=f"step-{idx}",
        questions=[
            {
                "question": f"Approve {name} (arg=1)?",
                "header": f"Approve {name} (arg=1)",
                "options": [{"label": "Approve"}, {"label": "Reject"}],
            }
        ],
        widget_id=f"card-{idx}",
        id=f"card-{idx}",
        origin_node="tool_approval",
        allow_custom=False,
    )


class _DockApp(App[None]):
    """Minimal harness hosting the approval dock."""

    def __init__(self) -> None:
        super().__init__()
        self.dock = ApprovalDockBar(id="approval-dock")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield self.dock


# ---------------------------------------------------------------------------
# Dock lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_first_card_is_head_and_visible() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        assert dock.pending_count == 0
        assert dock.display is False

        card = _card("run_command", 0)
        assert await dock.push_card(card) is True
        await pilot.pause()

        assert dock.pending_count == 1
        assert dock.head_widget is card
        assert dock.display is True
        assert card.is_mounted
        header = dock.query_one("#approval-dock-header")
        assert "1 pending" in str(header.render())


@pytest.mark.asyncio
async def test_push_second_card_renders_waiting_row_head_first() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        card_a = _card("run_command", 0)
        card_b = _card("write_file", 1)
        assert await dock.push_card(card_a) is True
        assert await dock.push_card(card_b) is True
        await pilot.pause()

        assert dock.pending_count == 2
        assert dock.head_widget is card_a
        # Only the head card is mounted; the queued one is a waiting row.
        assert card_a.is_mounted
        assert not card_b.is_mounted
        rows = list(dock.query(".approval-dock-waiting"))
        assert len(rows) == 1
        # Waiting rows use the short tool label, not the full header.
        assert "write_file" in str(rows[0].render())
        assert "arg=1" not in str(rows[0].render())
        header = dock.query_one("#approval-dock-header")
        assert "2 pending" in str(header.render())


@pytest.mark.asyncio
async def test_push_same_card_twice_is_deduped() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        card = _card("run_command", 0)
        assert await dock.push_card(card) is True
        assert await dock.push_card(card) is False
        await pilot.pause()
        assert dock.pending_count == 1


@pytest.mark.asyncio
async def test_pop_head_removes_card_and_promotes_next() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        card_a = _card("run_command", 0)
        card_b = _card("write_file", 1)
        card_c = _card("read_file", 2)
        await dock.push_card(card_a)
        await dock.push_card(card_b)
        await dock.push_card(card_c)
        await pilot.pause()

        assert await dock.pop_card(card_a) is True
        await pilot.pause()

        # card_b promoted to head, its waiting row gone; card_c still queued.
        assert dock.pending_count == 2
        assert dock.head_widget is card_b
        assert card_b.is_mounted
        assert not card_c.is_mounted
        rows = list(dock.query(".approval-dock-waiting"))
        assert len(rows) == 1
        # The promoted card renders before the remaining waiting row.
        children = [c.id or type(c).__name__ for c in dock.children]
        assert children.index("card-1") < children.index("_WaitingRow")
        assert await dock.pop_card(card_a) is False


@pytest.mark.asyncio
async def test_promote_next_is_noop_without_waiting_cards() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        card = _card("run_command", 0)
        await dock.push_card(card)
        await pilot.pause()
        assert await dock.promote_next() is False
        # Empty dock: also a no-op.
        await dock.pop_card(card)
        assert await dock.promote_next() is False


@pytest.mark.asyncio
async def test_clear_pending_empties_the_dock() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        await dock.push_card(_card("run_command", 0))
        await dock.push_card(_card("write_file", 1))
        await pilot.pause()
        assert dock.pending_count == 2

        dock.clear_pending()
        await pilot.pause()

        assert dock.pending_count == 0
        assert dock.head_widget is None
        assert dock.display is False
        assert not list(dock.query(".approval-dock-waiting"))


@pytest.mark.asyncio
async def test_dock_never_steals_focus_on_push() -> None:
    app = _DockApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.dock
        await dock.push_card(_card("run_command", 0))
        await dock.push_card(_card("write_file", 1))
        await pilot.pause()
        assert app.focused is not dock.head_widget


# ---------------------------------------------------------------------------
# Submit handler wiring: decided card relocates dock → transcript
# ---------------------------------------------------------------------------


class _FakeDock:
    """Records pop_card calls from the submit handler."""

    def __init__(self) -> None:
        self.popped: list[Any] = []

    async def pop_card(self, widget: Any) -> bool:  # noqa: ANN401
        self.popped.append(widget)
        return True

    async def promote_next(self) -> bool:
        return False


class _AdapterWithDock:
    """Fake adapter with a bound dock and a recording transcript mount."""

    def __init__(self) -> None:
        self._current_step_messages: dict = {}
        self._clarification_input_by_step: dict = {}
        self._clarification_answers_pending: list[str] | None = None
        self._clarification_pending = False
        self._approval_dock = _FakeDock()
        self.mounted_to_transcript: list[Any] = []

    async def _mount_message(self, widget: Any) -> None:  # noqa: ANN401
        self.mounted_to_transcript.append(widget)


class _RelocationHarness:
    """Minimal stand-in exercising _handle_clarification_submitted with a dock."""

    def __init__(self) -> None:
        self._composer_mode = "ask"
        self._status_bar = None
        self._ui_adapter = _AdapterWithDock()
        self.sent: list[str] = []

    async def _set_spinner(self, status: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        pass

    async def _resolve_default_clarification_mode(self) -> str:
        return "auto"

    async def _send_to_agent(self, message: str, **_kwargs: Any) -> None:  # noqa: ANN401
        self.sent.append(message)


@pytest.mark.asyncio
async def test_submit_relocates_card_from_dock_to_transcript() -> None:
    from soothe_cli.tui.app._execution import _ExecutionMixin

    class _Harness(_RelocationHarness, _ExecutionMixin):
        pass

    app = _Harness()
    adapter = app._ui_adapter
    card = _card("run_command", 0)
    adapter._clarification_input_by_step["step-0"] = card

    event = StructuredAskUserWidget.Submitted(
        step_id="step-0",
        questions=card._questions,
        answers=["Approve"],
        widget_id="card-0",
        origin_node="tool_approval",
    )
    await app.on_structured_ask_user_widget_submitted(event)

    # Decided card left the dock and became the transcript record.
    assert adapter._approval_dock.popped == [card]
    assert adapter.mounted_to_transcript == [card]
    # Answer still forwarded to the daemon.
    assert len(app.sent) == 1
    assert adapter._clarification_answers_pending == ["Approve"]


@pytest.mark.asyncio
async def test_abandon_relocates_card_without_sending() -> None:
    from soothe_cli.tui.app._execution import _ExecutionMixin

    class _Harness(_RelocationHarness, _ExecutionMixin):
        pass

    app = _Harness()
    adapter = app._ui_adapter
    card = _card("run_command", 0)
    adapter._clarification_input_by_step["step-0"] = card

    event = StructuredAskUserWidget.Submitted(
        step_id="step-0",
        questions=card._questions,
        answers=[],  # abandon posts nothing
        widget_id="card-0",
        origin_node="tool_approval",
    )
    await app.on_structured_ask_user_widget_submitted(event)

    # Dismissed card still lands in the transcript (no-send semantics kept).
    assert adapter._approval_dock.popped == [card]
    assert adapter.mounted_to_transcript == [card]
    assert app.sent == []
    assert adapter._clarification_pending is False
