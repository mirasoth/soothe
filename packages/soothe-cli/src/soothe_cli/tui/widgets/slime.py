"""Pixel-art slime bouncing across the thinking row's blank space.

While the loop runs, the slime hops between the end of the status hint
(token count) and the right edge of the thinking row, squashing on each
landing; when the loop ends it is handed to the tip row and dozes off,
flattened, where it stopped.

Sprites are authored as ASCII pixel grids (``#`` / ``.``) and converted to
braille characters at import time — each braille cell encodes a 2x4 pixel
block, so a single text row renders a 4-pixel-tall sprite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

FILLED: Final = "#"
"""Pixel-on marker used inside the ASCII sprite grids."""

_BRAILLE_BASE: Final = 0x2800
"""Unicode base of the braille patterns block."""

# Braille dot bits per pixel row (left-column bit, right-column bit).
_DOT_BITS: Final = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def _braille_sprite(rows: tuple[str, ...]) -> str:
    """Convert an ASCII pixel grid into a single-row braille string.

    Args:
    rows: Pixel rows (top to bottom), each ``#`` or ``.``. The height must
    not exceed 4 (one braille cell is 4 pixels tall).

    Returns:
    One braille character per 2-pixel-wide column of the grid.
    """
    width = len(rows[0])
    cells: list[str] = []
    for cx in range(0, width, 2):
        bits = 0
        for cy, row in enumerate(rows):
            for dx in (0, 1):
                px = cx + dx
                if px < width and row[px] == FILLED:
                    bits |= _DOT_BITS[cy][dx]
        cells.append(chr(_BRAILLE_BASE + bits))
    return "".join(cells)


# Slime mid-hop: stretched tall with eyes, bottom-anchored. 8x4 pixels
# (renders 4 cells wide). The slime is symmetric — no facing variants.
_SLIME_JUMP: Final = (
    "..####..",
    ".#.##.#.",
    "########",
    "########",
)

# Squashed on landing: eyes ride down with the squash.
_SLIME_LAND: Final = (
    "........",
    "..####..",
    "##.##.##",
    "########",
)

# Dozing pose: flattened solid (eyes closed) with a baked-in "z" floating
# above-right. 14x4 pixels (renders 7 cells wide).
_SLIME_SLEEP: Final = (
    "..........###.",
    "..####.....#..",
    "########..###.",
    "########......",
)

RUN_FRAMES: Final = (_braille_sprite(_SLIME_JUMP), _braille_sprite(_SLIME_LAND))
"""Alternating hop frames (stretch, squash)."""

SLEEP_SPRITE: Final = _braille_sprite(_SLIME_SLEEP)

SLIME_SPRITE_WIDTH: Final = len(RUN_FRAMES[0])
"""Display width (in terminal cells) of the hopping slime."""


@dataclass(frozen=True, slots=True)
class SlimeState:
    """Snapshot of the slime's position so it can doze off where it stopped.

    Attributes:
    x: Content column where the sprite starts.
    """

    x: int


class SlimeRunner:
    """Hop-and-frame state machine for the bouncing slime.

    Tracks the sprite's column within the blank area of the thinking row and
    flips direction at both edges.
    """

    def __init__(self, state: SlimeState | None = None) -> None:
        """Initialize from a previous snapshot (e.g. the spot where it dozed off).

        Args:
        state: Restored position, or `None` to start fresh.
        """
        self._x: int = state.x if state is not None else 0
        self._facing: int = 1
        self._frame: int = 0
        self._placed: bool = state is not None

    @property
    def x(self) -> int:
        """Current content column where the sprite starts."""
        return self._x

    @property
    def facing(self) -> int:
        """+1 while hopping right, -1 while hopping left."""
        return self._facing

    @property
    def placed(self) -> bool:
        """Whether the slime has ever had room to appear on the row."""
        return self._placed

    def advance(self, min_x: int, max_x: int) -> None:
        """Move one cell within `[min_x, max_x]`, bouncing at both edges.

        Args:
        min_x: Leftmost sprite start column (just right of the status hint).
        max_x: Rightmost sprite start column (flush with the row edge).
        """
        if max_x < min_x:
            return  # Not enough room on this row — hold still.
        if not self._placed:
            self._x = min_x
            self._facing = 1
            self._placed = True
            return
        self._frame ^= 1
        nxt = self._x + self._facing
        if nxt > max_x:
            self._facing = -1
            nxt = self._x - 1
        elif nxt < min_x:
            self._facing = 1
            nxt = self._x + 1
        self._x = max(min_x, min(max_x, nxt))

    def sprite(self) -> str:
        """Get the braille sprite for the current hop frame."""
        return RUN_FRAMES[self._frame]

    def state(self) -> SlimeState:
        """Snapshot the current position."""
        return SlimeState(x=self._x)


def run_slime_segment(
    runner: SlimeRunner, prefix_cells: int, width: int, *, advance: bool
) -> str | None:
    """Build the padded hopping-slime segment appended after the status hint.

    Args:
    runner: The hop state machine driving the slime.
    prefix_cells: Display width of the rendered status line before the slime.
    width: Display width of the row the slime hops across.
    advance: When `True`, move the slime one cell before rendering.

    Returns:
    Padding spaces plus the sprite, or `None` when the row is too narrow
    (or the prefix already reaches the sprite).
    """
    min_x = prefix_cells + 2
    max_x = width - SLIME_SPRITE_WIDTH
    if max_x < min_x:
        return None
    if advance:
        runner.advance(min_x, max_x)
    pad = runner.x - prefix_cells
    if pad < 1:
        return None
    return " " * pad + runner.sprite()


def sleeping_slime_segment(state: SlimeState, prefix_cells: int, width: int) -> str | None:
    """Build the padded sleeping-slime segment for the tip row.

    The slime rests at the column where it stopped, nudged right when a
    longer tip would overlap it.

    Args:
    state: Resting position captured when the loop ended.
    prefix_cells: Display width of the rendered tip text before the slime.
    width: Display width of the tip row.

    Returns:
    Padding spaces plus the sleeping sprite, or `None` when the row is too
    narrow to fit both the tip and the slime.
    """
    min_x = prefix_cells + 1
    max_x = width - len(SLEEP_SPRITE)
    if max_x < min_x:
        return None
    x = min(max(state.x, min_x), max_x)
    return " " * (x - prefix_cells) + SLEEP_SPRITE
