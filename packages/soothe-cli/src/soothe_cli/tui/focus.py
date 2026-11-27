"""Unified focus arbitration for the TUI.

Every automatic "who should hold keyboard focus" decision routes through
:class:`FocusCoordinator` instead of each widget scheduling its own delayed
focus attempt. Two primitives replace the scattered fixed-delay schemes
(immediate fire / ``call_after_refresh`` / retry timers):

- :meth:`FocusCoordinator.request_focus` — a prioritized focus request.
  Pending requests are arbitrated by :class:`FocusPriority`: a converging
  request blocks weaker ones, and later requests replace weaker or equal
  pending ones. Each request converges instead of firing once: the target is
  focused, focus ownership is verified, and short bounded retries cover the
  uncertain mount → compose → layout window (any fixed delay is too early on
  slow terminals and redundant on fast ones).
- :meth:`FocusCoordinator.claim_region` / :meth:`FocusCoordinator.release_region`
  — a protected region (e.g. the manual-mode tool-approval head card,
  IG-CLNT-005). While a stronger claim is active, weaker requests are dropped
  outright: turn teardown must not reclaim focus for the chat input while a
  manual approval awaits a decision.

User-driven focus moves (click, Tab, Ctrl+R, intra-card navigation via
explicit ``focus()``) bypass the coordinator; the coordinator only arbitrates
*automatic* focus, so a user's explicit move always wins over a pending
request.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from enum import IntEnum
from functools import partial
from typing import Any

__all__ = ["FocusCoordinator", "FocusPriority"]

logger = logging.getLogger(__name__)

_RETRY_INTERVAL_SECONDS = 0.05
"""Interval between convergence retries once the call_after_refresh fast
path is exhausted."""

_CALL_LATER_ATTEMPTS = 3
"""Number of call_after_refresh attempts before falling back to timed retries.

A refresh-triggered attempt has zero extra latency when layout already
settled; timers guarantee progress even when refreshes stop arriving."""

_MAX_CONVERGE_SECONDS = 1.0
"""Wall-clock ceiling for a pending request. Layout may be slow; never
unbounded."""


class FocusPriority(IntEnum):
    """Focus-request authority; lower numeric value = stronger claim.

    The ordering encodes the IG-CLNT-005 contract: a manual-mode tool-approval
    head card outranks every automatic focus mover; chat-input convenience is
    the weakest request and loses to any pending stronger request or active
    stronger claim.
    """

    MODAL = 0
    """Reserved for modal screens, which manage their own focus."""

    APPROVAL_HEAD = 10
    """Manual-mode tool-approval head card (the operator is the decider)."""

    CLARIFICATION_INPUT = 20
    """Active clarification / plan-review answer field."""

    PRIMARY_INPUT = 30
    """A lone non-chat input (e.g. a modal filter box)."""

    CHAT_INPUT = 40
    """The bottom chat prompt — default convenience, weakest authority."""


class _PendingRequest:
    """A focus request awaiting confirmed ownership of keyboard focus."""

    __slots__ = ("attempts", "deadline", "priority", "reason", "saw_mounted", "widget")

    def __init__(self, widget: Any, priority: FocusPriority, reason: str) -> None:
        self.widget = widget
        self.priority = priority
        self.reason = reason
        self.deadline = time.monotonic() + _MAX_CONVERGE_SECONDS
        self.saw_mounted = False
        self.attempts = 0


class FocusCoordinator:
    """Single arbiter for automatic keyboard focus within one app instance."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self._app = app
        self._pending: _PendingRequest | None = None
        self._deadline_timer: Any | None = None
        self._claims: dict[int, tuple[Any, FocusPriority]] = {}
        """Protected regions keyed by ``id(widget)``; pruned lazily."""

    # ── Public API ────────────────────────────────────────────────────

    def request_focus(self, widget: Any, priority: FocusPriority, *, reason: str = "") -> bool:
        """Request keyboard focus for ``widget`` with ``priority`` authority.

        Returns True when the target holds focus (immediately, or the request
        was accepted and is converging) and False when the request was dropped
        — either a stronger pending request is converging or an active stronger
        protected region blocks it.

        A widget that already holds focus (directly or via a descendant)
        short-circuits to True; repeated requests are free.
        """
        if widget is None or not getattr(widget, "can_focus", False):
            return False
        if self._modal_open():
            self._clear_pending("modal opened")
            return False
        if self._holds_focus(widget):
            self._clear_pending("target already focused")
            return True
        if self._blocked_by_stronger_claim(widget, priority):
            logger.debug(
                "focus request dropped by stronger claim: %s (%s)",
                self._describe(widget),
                reason or priority.name,
            )
            return False
        pending = self._pending
        if pending is not None and pending.widget is not widget and pending.priority < priority:
            logger.debug(
                "focus request superseded by pending %s request: %s (%s)",
                pending.priority.name,
                self._describe(widget),
                reason or priority.name,
            )
            return False
        if pending is not None and pending.widget is widget:
            # Same target: keep the existing (possibly stronger) convergence.
            return True
        self._clear_pending("superseded by newer request")
        request = _PendingRequest(widget, priority, reason or priority.name)
        self._pending = request
        self._arm_deadline_timer(request)
        logger.debug(
            "focus requested: %s priority=%s (%s)",
            self._describe(widget),
            priority.name,
            request.reason,
        )
        self._tick(request)
        return self._holds_focus(widget) or self._pending is request

    def claim_region(self, widget: Any, priority: FocusPriority, *, reason: str = "") -> None:
        """Protect ``widget``'s region: weaker requests are dropped while the
        claim is active (widget mounted). Stronger or equal requests to the
        claimed subtree are unaffected."""
        if widget is None:
            return
        self._claims[id(widget)] = (widget, priority)
        logger.debug(
            "focus region claimed: %s priority=%s (%s)",
            self._describe(widget),
            priority.name,
            reason or "unspecified",
        )

    def release_region(self, widget: Any) -> None:
        """Drop the protection for ``widget`` (card answered / cleared)."""
        if widget is None:
            return
        if self._claims.pop(id(widget), None) is not None:
            logger.debug("focus region released: %s", self._describe(widget))

    def cancel_pending(self, *, reason: str = "") -> None:
        """Cancel the pending convergence (user took over, e.g. Ctrl+R)."""
        self._clear_pending(reason or "pending cancelled")

    # ── Arbitration internals ─────────────────────────────────────────

    def _modal_open(self) -> bool:
        with suppress(Exception):
            return bool(self._app.screen.is_modal)
        return False

    def _holds_focus(self, widget: Any) -> bool:
        focused = getattr(self._app, "focused", None)
        if focused is None:
            return False
        if focused is widget:
            return True
        with suppress(Exception):
            return focused in widget.walk_children(with_self=True)
        return False

    def _active_claims(self) -> list[tuple[Any, FocusPriority]]:
        active: list[tuple[Any, FocusPriority]] = []
        stale: list[int] = []
        for key, (widget, priority) in self._claims.items():
            if widget is None or not widget.is_mounted:
                stale.append(key)
                continue
            active.append((widget, priority))
        for key in stale:
            self._claims.pop(key, None)
        return active

    def _blocked_by_stronger_claim(self, widget: Any, priority: FocusPriority) -> bool:
        for claimed, claim_priority in self._active_claims():
            if claim_priority >= priority:
                continue
            with suppress(Exception):
                if widget is claimed or widget in claimed.walk_children(with_self=True):
                    continue
            return True
        return False

    # ── Convergence loop ──────────────────────────────────────────────

    def _tick(self, request: _PendingRequest) -> None:
        """One convergence attempt; reschedules until confirmed or expired."""
        if self._pending is not request:
            return
        request.attempts += 1
        if self._modal_open():
            self._clear_pending("modal opened")
            return
        widget = request.widget
        mounted = getattr(widget, "is_mounted", False)
        if mounted:
            request.saw_mounted = True
        elif request.saw_mounted:
            self._clear_pending("target removed")
            return
        if self._blocked_by_stronger_claim(widget, request.priority):
            self._clear_pending("stronger claim took focus")
            return
        if self._attempt(widget):
            self._clear_pending("focus confirmed")
            return
        if time.monotonic() >= request.deadline:
            logger.debug(
                "focus request expired: %s (%s) after %s attempts",
                self._describe(widget),
                request.reason,
                request.attempts,
            )
            self._clear_pending("convergence deadline reached")
            return
        if request.attempts <= _CALL_LATER_ATTEMPTS:
            self._schedule_via_call_after_refresh(request)
        else:
            self._schedule_via_timer(request)

    def _attempt(self, widget: Any) -> bool:
        """Focus ``widget`` and verify ownership landed on it (or its subtree)."""
        try:
            self._app.set_focus(widget, scroll_visible=False)
        except Exception:  # noqa: BLE001
            return False
        return self._holds_focus(widget)

    def _schedule_via_call_after_refresh(self, request: _PendingRequest) -> None:
        with suppress(Exception):
            self._app.call_after_refresh(self._tick, request)
            return
        self._schedule_via_timer(request)

    def _schedule_via_timer(self, request: _PendingRequest) -> None:
        # App.set_timer takes a zero-arg callback — bind the request via partial.
        with suppress(Exception):
            self._app.set_timer(_RETRY_INTERVAL_SECONDS, partial(self._tick, request))

    def _arm_deadline_timer(self, request: _PendingRequest) -> None:
        self._cancel_deadline_timer()
        with suppress(Exception):
            self._deadline_timer = self._app.set_timer(
                _MAX_CONVERGE_SECONDS, partial(self._expire, request)
            )

    def _expire(self, request: _PendingRequest) -> None:
        if self._pending is not request:
            return
        if self._attempt(request.widget):
            self._clear_pending("focus confirmed")
            return
        logger.debug(
            "focus request expired: %s (%s) after %s attempts",
            self._describe(request.widget),
            request.reason,
            request.attempts,
        )
        self._clear_pending("convergence deadline reached")

    def _cancel_deadline_timer(self) -> None:
        timer = self._deadline_timer
        self._deadline_timer = None
        if timer is not None:
            with suppress(Exception):
                timer.stop()

    # ── Bookkeeping ───────────────────────────────────────────────────

    def _clear_pending(self, why: str) -> None:
        if self._pending is not None:
            logger.debug(
                "focus pending cleared (%s): %s (%s)",
                why,
                self._describe(self._pending.widget),
                self._pending.reason,
            )
        self._pending = None
        self._cancel_deadline_timer()

    @staticmethod
    def _describe(widget: Any) -> str:
        try:
            return f"{type(widget).__name__}#{getattr(widget, 'id', None) or '-'}"
        except Exception:  # noqa: BLE001
            return repr(widget)
