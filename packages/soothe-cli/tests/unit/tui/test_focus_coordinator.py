"""Tests for the unified focus coordinator (soothe_cli.tui.focus)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import TextArea

from soothe_cli.tui.focus import FocusCoordinator, FocusPriority


class _CoordApp(App[None]):
    """Harness binding a real coordinator like SootheApp does."""

    def __init__(self) -> None:
        super().__init__()
        self._focus_coordinator = FocusCoordinator(self)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield TextArea(id="chat-input")


def _docked_area(widget_id: str) -> TextArea:
    """A mounted-but-disabled area (focusable yet not focusable *yet*).

    Textual's ``Screen.set_focus`` accepts any ``focusable`` widget — even one
    outside the DOM — so pending convergence is simulated with ``disabled``,
    the one state where ``set_focus`` refuses the widget.
    """
    area = TextArea(id=widget_id)
    area.disabled = True
    return area


@pytest.mark.asyncio
async def test_request_focus_focuses_mounted_target_immediately() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        target = app.query_one("#chat-input", TextArea)

        assert coordinator.request_focus(target, FocusPriority.CHAT_INPUT, reason="test")
        await pilot.pause()
        assert app.focused is target


@pytest.mark.asyncio
async def test_request_on_focused_target_short_circuits() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        target = app.query_one("#chat-input", TextArea)
        target.focus()
        await pilot.pause()

        assert coordinator.request_focus(target, FocusPriority.APPROVAL_HEAD, reason="test")


@pytest.mark.asyncio
async def test_request_converges_when_target_becomes_focusable() -> None:
    """The core fix: a request that cannot land yet is not lost.

    The old fire-once autofocus silently failed when the target could not
    take focus at call time; the coordinator keeps retrying within its
    bounded window until focus ownership is confirmed.
    """
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        late = _docked_area("late-card")
        await app.mount(late)
        await pilot.pause()

        assert coordinator.request_focus(late, FocusPriority.APPROVAL_HEAD, reason="test")
        assert app.focused is not late  # disabled — request still pending

        late.disabled = False
        for _ in range(6):
            await pilot.pause(0.05)
        assert app.focused is late


@pytest.mark.asyncio
async def test_pending_strong_request_blocks_weaker_requests() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        pending_strong = _docked_area("strong-card")
        await app.mount(pending_strong)
        await pilot.pause()
        # Blur the startup-focused chat input so nothing holds focus yet.
        app.set_focus(None)
        await pilot.pause()
        assert coordinator.request_focus(pending_strong, FocusPriority.APPROVAL_HEAD, reason="test")
        assert coordinator._pending is not None

        mounted_weak = app.query_one("#chat-input", TextArea)
        assert not coordinator.request_focus(mounted_weak, FocusPriority.CHAT_INPUT, reason="test")
        await pilot.pause()
        assert app.focused is not mounted_weak


@pytest.mark.asyncio
async def test_later_weaker_request_cannot_override_pending_stronger_one() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        strong = _docked_area("strong-card")
        await app.mount(strong)
        await pilot.pause()
        # Blur the startup-focused chat input so the weak request below is a
        # genuine arbitration (not a short-circuit on an already-focused target).
        app.set_focus(None)
        await pilot.pause()
        assert coordinator.request_focus(strong, FocusPriority.CLARIFICATION_INPUT, reason="test")

        weak = app.query_one("#chat-input", TextArea)
        assert not coordinator.request_focus(weak, FocusPriority.CHAT_INPUT, reason="test")


@pytest.mark.asyncio
async def test_later_stronger_request_replaces_pending_weaker_one() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        weak = _docked_area("weak-card")
        await app.mount(weak)
        await pilot.pause()
        assert coordinator.request_focus(weak, FocusPriority.CHAT_INPUT, reason="test")

        strong = _docked_area("strong-card")
        await app.mount(strong)
        await pilot.pause()
        assert coordinator.request_focus(strong, FocusPriority.APPROVAL_HEAD, reason="test")

        strong.disabled = False
        for _ in range(6):
            await pilot.pause(0.05)
        assert app.focused is strong


@pytest.mark.asyncio
async def test_claimed_region_blocks_weaker_requests() -> None:
    """Protected region: an active manual-approval claim drops chat requests."""
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        card = app.query_one("#chat-input", TextArea)
        coordinator.claim_region(card, FocusPriority.APPROVAL_HEAD, reason="test")
        assert coordinator.request_focus(card, FocusPriority.APPROVAL_HEAD, reason="test")
        await pilot.pause()
        assert app.focused is card

        chat = TextArea(id="chat")
        await app.mount(chat)
        await pilot.pause()
        assert not coordinator.request_focus(chat, FocusPriority.CHAT_INPUT, reason="test")
        await pilot.pause()
        assert app.focused is card


@pytest.mark.asyncio
async def test_release_claim_allows_weaker_requests_again() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        card = app.query_one("#chat-input", TextArea)
        coordinator.claim_region(card, FocusPriority.APPROVAL_HEAD, reason="test")
        assert coordinator.request_focus(card, FocusPriority.APPROVAL_HEAD, reason="test")
        await pilot.pause()

        coordinator.release_region(card)
        chat = TextArea(id="chat")
        await app.mount(chat)
        await pilot.pause()
        assert coordinator.request_focus(chat, FocusPriority.CHAT_INPUT, reason="test")
        for _ in range(3):
            await pilot.pause(0.05)
        assert app.focused is chat


@pytest.mark.asyncio
async def test_claim_containing_target_does_not_block_it() -> None:
    """A request for a widget inside the claimed subtree is not dropped."""
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        card = app.query_one("#chat-input", TextArea)
        coordinator.claim_region(card, FocusPriority.APPROVAL_HEAD, reason="test")

        assert coordinator.request_focus(card, FocusPriority.APPROVAL_HEAD, reason="test")


@pytest.mark.asyncio
async def test_cancel_pending_stops_convergence() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        late = _docked_area("late-card")
        await app.mount(late)
        await pilot.pause()
        assert coordinator.request_focus(late, FocusPriority.APPROVAL_HEAD, reason="test")

        coordinator.cancel_pending(reason="user took over")
        late.disabled = False
        for _ in range(6):
            await pilot.pause(0.05)
        assert app.focused is not late


@pytest.mark.asyncio
async def test_modal_screen_drops_requests_and_cancels_pending() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        late = _docked_area("late-card")
        await app.mount(late)
        await pilot.pause()
        assert coordinator.request_focus(late, FocusPriority.APPROVAL_HEAD, reason="test")

        await app.push_screen(ModalScreen())
        await pilot.pause()
        target = app.query_one("#chat-input", TextArea)
        assert not coordinator.request_focus(target, FocusPriority.CHAT_INPUT, reason="test")

        late.disabled = False
        for _ in range(4):
            await pilot.pause(0.05)
        assert app.focused is not late  # pending was cancelled by the modal


@pytest.mark.asyncio
async def test_unmounting_target_cancels_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """A removed target stops the retry loop (no dangling timers)."""
    import soothe_cli.tui.focus as focus_mod

    monkeypatch.setattr(focus_mod, "_MAX_CONVERGE_SECONDS", 0.15)
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        doomed = _docked_area("doomed")
        await app.mount(doomed)
        await pilot.pause()
        assert coordinator.request_focus(doomed, FocusPriority.APPROVAL_HEAD, reason="test")

        await doomed.remove()
        for _ in range(6):
            await pilot.pause(0.05)
        assert coordinator._pending is None


@pytest.mark.asyncio
async def test_convergence_deadline_gives_up_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target that never becomes focusable expires instead of retrying forever."""
    import soothe_cli.tui.focus as focus_mod

    monkeypatch.setattr(focus_mod, "_MAX_CONVERGE_SECONDS", 0.15)
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        ghost = _docked_area("ghost")
        await app.mount(ghost)
        await pilot.pause()
        assert coordinator.request_focus(ghost, FocusPriority.APPROVAL_HEAD, reason="test")

        for _ in range(8):
            await pilot.pause(0.05)
        assert coordinator._pending is None
        assert app.focused is not ghost


@pytest.mark.asyncio
async def test_same_widget_pending_request_is_not_duplicated() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator
        late = _docked_area("late-card")
        await app.mount(late)
        await pilot.pause()
        assert coordinator.request_focus(late, FocusPriority.APPROVAL_HEAD, reason="a")
        assert coordinator.request_focus(late, FocusPriority.APPROVAL_HEAD, reason="b")
        assert coordinator._pending is not None
        assert coordinator._pending.widget is late


@pytest.mark.asyncio
async def test_non_focusable_or_none_target_is_dropped() -> None:
    async with _CoordApp().run_test() as pilot:
        app = pilot.app
        assert isinstance(app, _CoordApp)
        coordinator = app._focus_coordinator

        assert not coordinator.request_focus(None, FocusPriority.CHAT_INPUT, reason="test")

        class _NotFocusable(TextArea):
            can_focus = False

        inert = _NotFocusable(id="inert")
        await app.mount(inert)
        await pilot.pause()
        assert not coordinator.request_focus(inert, FocusPriority.CHAT_INPUT, reason="test")
        assert app.focused is not inert
