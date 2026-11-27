"""Per-loop outbound stream replay buffer (IG-DAEM-004 follow-up).

Keeps a bounded, in-memory ring of recent client-visible frames (post
seq-stamping) per loop so a reconnecting client can resume the stream from its
last seen `seq` via the `stream_resume` RPC instead of falling back to the
card-ledger reattach replay.

Semantics:
- The buffer is process-local: a daemon restart clears it (the restart case is
  recovered by auto-resume + card replay; reconnect *within* one daemon
  lifetime is the case this buffer closes).
- `gap=True` in the resume result means frames between the client's `last_seq`
  and the oldest buffered frame were evicted — the consumer must fall back to
  the card-ledger reattach replay for the missing span.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from typing import Any

logger = logging.getLogger(__name__)

# Bounded per-loop frame ring and loop-entry count (LRU) so a long-lived
# daemon's replay memory stays flat regardless of loop churn.
STREAM_REPLAY_BUFFER_MAX_FRAMES = 512
STREAM_REPLAY_BUFFER_MAX_LOOPS = 64


class StreamReplayBuffer:
    """Bounded per-loop ring of seq-stamped outbound frames."""

    def __init__(
        self,
        *,
        max_frames: int = STREAM_REPLAY_BUFFER_MAX_FRAMES,
        max_loops: int = STREAM_REPLAY_BUFFER_MAX_LOOPS,
    ) -> None:
        self._max_frames = max_frames
        self._max_loops = max_loops
        # loop_id → deque of frames, insertion-ordered LRU by loop.
        self._buffers: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()

    def record(self, loop_id: str, frame: dict[str, Any]) -> None:
        """Record one client-visible frame (must carry an int `seq`)."""
        seq = frame.get("seq") if isinstance(frame, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool):
            return
        lid = str(loop_id or "").strip()
        if not lid:
            return
        buf = self._buffers.get(lid)
        if buf is None:
            while len(self._buffers) >= self._max_loops:
                self._buffers.popitem(last=False)
            buf = deque(maxlen=self._max_frames)
            self._buffers[lid] = buf
        else:
            self._buffers.move_to_end(lid)
        buf.append(frame)

    def resume(self, loop_id: str, last_seq: int) -> dict[str, Any]:
        """Return buffered frames after `last_seq` plus gap/coverage metadata."""
        lid = str(loop_id or "").strip()
        buf = self._buffers.get(lid) if lid else None
        if buf is None:
            return {
                "loop_id": lid,
                "last_seq": last_seq,
                "frames": [],
                "oldest_seq": None,
                "newest_seq": None,
                "gap": False,
            }
        seqs = [f["seq"] for f in buf if isinstance(f.get("seq"), int)]
        oldest = min(seqs) if seqs else None
        newest = max(seqs) if seqs else None
        frames = [f for f in buf if isinstance(f.get("seq"), int) and f["seq"] > last_seq]
        # Client is current when it has seen everything up to `newest`. Frames
        # older than the buffer window were evicted → resumable span starts at
        # `oldest`; anything before that is a gap only if the client needs it
        # (i.e. it is behind `newest` but older than `oldest - 1`).
        gap = (
            newest is not None
            and last_seq < newest
            and oldest is not None
            and last_seq < oldest - 1
        )
        return {
            "loop_id": lid,
            "last_seq": last_seq,
            "frames": list(frames),
            "oldest_seq": oldest,
            "newest_seq": newest,
            "gap": gap,
        }

    def drop_loop(self, loop_id: str) -> None:
        """Remove the buffer for a deleted loop."""
        self._buffers.pop(str(loop_id or "").strip(), None)


__all__ = [
    "STREAM_REPLAY_BUFFER_MAX_FRAMES",
    "STREAM_REPLAY_BUFFER_MAX_LOOPS",
    "StreamReplayBuffer",
]
