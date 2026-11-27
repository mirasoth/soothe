"""Pixel-slime thinking-row animation: sprites, hop logic, and widget wiring."""

from __future__ import annotations

import pytest

from soothe_cli.tui.widgets.loading import LoadingWidget, TipRow
from soothe_cli.tui.widgets.slime import (
    _BRAILLE_BASE,
    _DOT_BITS,
    _SLIME_JUMP,
    _SLIME_LAND,
    _SLIME_SLEEP,
    RUN_FRAMES,
    SLEEP_SPRITE,
    SLIME_SPRITE_WIDTH,
    SlimeRunner,
    SlimeState,
    _braille_sprite,
    run_slime_segment,
    sleeping_slime_segment,
)

_GRIDS = (_SLIME_JUMP, _SLIME_LAND, _SLIME_SLEEP)


def _plain(renderable: object) -> str:
    """Extract plain text from a Content or plain-string renderable."""
    if hasattr(renderable, "plain"):
        return str(renderable.plain)  # type: ignore[attr-defined]
    return str(renderable)


# ---------------------------------------------------------------------------
# Sprite authoring invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grid", _GRIDS)
def test_pixel_grids_are_four_rows_of_equal_even_width(grid: tuple[str, ...]) -> None:
    """Pixel grids must be 4 rows of equal even width so braille cells tile exactly."""
    assert len(grid) == 4
    widths = {len(row) for row in grid}
    assert len(widths) == 1
    width = widths.pop()
    assert width % 2 == 0
    assert any("#" in row for row in grid)


def test_braille_sprite_converts_known_grid() -> None:
    """A 2x4 grid maps to one cell with the expected dot bits."""
    sprite = _braille_sprite(("#.", "..", ".#", ".."))
    assert len(sprite) == 1
    # dot1 (row0 left) + dot6 (row2 right)
    assert sprite == chr(0x2800 + 0x01 + 0x20)


@pytest.mark.parametrize("grid", _GRIDS)
def test_braille_sprite_round_trips_pixel_grids(grid: tuple[str, ...]) -> None:
    """Decoding an authored sprite back to pixels must reproduce the grid."""
    sprite = _braille_sprite(grid)
    width = len(grid[0])
    for cy in range(4):
        for px in range(width):
            bit = _DOT_BITS[cy][px % 2]
            on = (ord(sprite[px // 2]) - _BRAILLE_BASE) & bit != 0
            assert on == (grid[cy][px] == "#")


def test_sprite_widths() -> None:
    """Geometry math needs sane sprite widths (hop 4 cells, sleep 7 cells)."""
    assert SLIME_SPRITE_WIDTH == 4
    assert len(RUN_FRAMES) == 2
    assert len(SLEEP_SPRITE) == 7


# ---------------------------------------------------------------------------
# SlimeRunner hop logic
# ---------------------------------------------------------------------------


def test_runner_places_slime_at_left_edge_on_first_advance() -> None:
    """First advance parks the slime right after the status hint, hopping right."""
    runner = SlimeRunner()
    runner.advance(min_x=30, max_x=60)
    assert runner.placed
    assert runner.x == 30
    assert runner.facing == 1


def test_runner_bounces_between_edges() -> None:
    """The slime hops right, flips at the right edge, and returns to the left edge."""
    runner = SlimeRunner()
    min_x, max_x = 10, 13
    runner.advance(min_x, max_x)
    assert runner.x == 10
    runner.advance(min_x, max_x)
    assert runner.x == 11
    runner.advance(min_x, max_x)
    runner.advance(min_x, max_x)
    runner.advance(min_x, max_x)
    assert runner.facing == -1
    assert runner.x == 12
    runner.advance(min_x, max_x)
    runner.advance(min_x, max_x)
    assert runner.x == 10
    runner.advance(min_x, max_x)
    assert runner.facing == 1


def test_runner_alternates_hop_frames() -> None:
    """Each hop toggles between the stretch and squash sprites."""
    runner = SlimeRunner()
    runner.advance(0, 80)
    assert runner.sprite() == RUN_FRAMES[0]
    runner.advance(0, 80)
    assert runner.sprite() == RUN_FRAMES[1]
    runner.advance(0, 80)
    assert runner.sprite() == RUN_FRAMES[0]


def test_runner_holds_still_when_row_is_too_narrow() -> None:
    """No room means no placement and no movement."""
    runner = SlimeRunner()
    runner.advance(min_x=30, max_x=20)
    assert not runner.placed
    runner.advance(min_x=30, max_x=20)
    assert not runner.placed


def test_runner_restores_position_from_sleep_snapshot() -> None:
    """A woken slime resumes from where it dozed off, hopping right."""
    runner = SlimeRunner(SlimeState(x=42))
    assert runner.placed
    runner.advance(min_x=5, max_x=60)
    assert runner.x == 43
    assert runner.facing == 1


# ---------------------------------------------------------------------------
# Segment builders
# ---------------------------------------------------------------------------


def test_run_segment_pads_sprite_after_prefix() -> None:
    """The segment is padding spaces followed by the sprite."""
    runner = SlimeRunner()
    segment = run_slime_segment(runner, prefix_cells=30, width=80, advance=True)
    assert segment is not None
    assert segment == " " * 2 + runner.sprite()
    assert len(segment) == 2 + SLIME_SPRITE_WIDTH


def test_run_segment_hidden_when_prefix_reaches_right_edge() -> None:
    """A long hint leaves no room and the segment is skipped."""
    width = 50
    runner = SlimeRunner()
    segment = run_slime_segment(
        runner, prefix_cells=width - SLIME_SPRITE_WIDTH, width=width, advance=True
    )
    assert segment is None


def test_sleep_segment_rests_at_saved_column() -> None:
    """The sleeping slime stays at the captured column."""
    segment = sleeping_slime_segment(SlimeState(x=40), prefix_cells=10, width=80)
    assert segment is not None
    assert segment == " " * 30 + SLEEP_SPRITE


def test_sleep_segment_shifts_right_when_tip_grows_over_it() -> None:
    """A longer tip nudges the sleeping slime right instead of overlapping it."""
    segment = sleeping_slime_segment(SlimeState(x=5), prefix_cells=20, width=80)
    assert segment is not None
    # Starts at min_x = prefix + 1, i.e. one gap column after the tip text.
    assert segment == " " + SLEEP_SPRITE


def test_sleep_segment_hidden_when_row_too_narrow() -> None:
    """Tip plus slime must fit or the slime is skipped."""
    segment = sleeping_slime_segment(SlimeState(x=2), prefix_cells=31, width=38)
    assert segment is None


# ---------------------------------------------------------------------------
# Widget wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loading_widget_renders_hopping_slime_after_hint() -> None:
    """The slime hops to the right of the status hint on the thinking row."""
    from textual.app import App, ComposeResult
    from textual.containers import Container

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield Container(id="thinking-status")

    app = _Harness()
    async with app.run_test(size=(100, 10)) as pilot:
        container = app.query_one("#thinking-status", Container)
        widget = LoadingWidget("Thinking")
        await container.mount(widget)
        await pilot.pause(0.3)

        content = widget._build_content().plain
        slime_index = next((content.find(f) for f in RUN_FRAMES if content.find(f) != -1), -1)
        assert slime_index != -1, content
        # Right of " <spinner> Thinking... · <elapsed>s" — never over the hint.
        assert slime_index > len(" Thinking... ")

        dormant = widget.dormant_slime_state()
        assert dormant is not None


@pytest.mark.asyncio
async def test_tip_row_parks_sleeping_slime_and_hands_it_back() -> None:
    """The tip row shows the sleeping slime, and take_slime_state clears it."""
    from textual.app import App, ComposeResult
    from textual.containers import Container

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield Container(id="thinking-status")

    app = _Harness()
    async with app.run_test(size=(100, 10)) as pilot:
        container = app.query_one("#thinking-status", Container)
        tip_row = TipRow()
        await container.mount(tip_row)
        tip_row.set_tip("rotate tips freely")
        await pilot.pause()

        state = SlimeState(x=60)
        tip_row.display_sleeping_slime(state)
        await pilot.pause()
        assert SLEEP_SPRITE in _plain(tip_row.render())

        handed_back = tip_row.take_slime_state()
        assert handed_back == state
        await pilot.pause()
        assert SLEEP_SPRITE not in _plain(tip_row.render())
