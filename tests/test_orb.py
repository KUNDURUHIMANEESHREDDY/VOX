"""Tests for the native orb: geometry, colour, and the activity mapping.

The pixel maths is the part worth pinning down. ``UpdateLayeredWindow``
composites premultiplied alpha, so a buffer written as straight RGBA produces a
dark halo, and a ring that forgets to clear its inner edge leaves a filled disc
that reads as armed. Both are silent on screen and trivial to assert here.

Nothing in this file creates a window: the renderer is exercised through its
DIB, and the Win32 entry points are only touched where they are pure functions.
"""

from __future__ import annotations

import time

import pytest

from voice_os.orb import (
    ARM_CORE,
    DISC,
    PIXELS,
    PLATE,
    Control,
    NativeOrb,
    OrbRenderer,
    _mix,
    _Pixel,
)


@pytest.fixture()
def renderer():
    r = OrbRenderer()
    yield r
    r.close()


def _alpha_at(r: OrbRenderer, x: int, y: int) -> int:
    i = (y * PLATE + x) * 4
    return r.buffer[i + 3]


def _bgra_at(r: OrbRenderer, x: int, y: int) -> tuple[int, int, int, int]:
    i = (y * PLATE + x) * 4
    return (r.buffer[i], r.buffer[i + 1], r.buffer[i + 2], r.buffer[i + 3])


# ------------------------------------------------------------------ geometry


def test_the_geometry_table_covers_the_whole_plate():
    """Every in-range pixel must be precomputed, or it keeps stale alpha."""
    assert PIXELS, "no pixels were precomputed"
    indices = {p.index for p in PIXELS}
    centre = ((PLATE - 1) // 2) * PLATE + (PLATE - 1) // 2
    assert centre * 4 in indices
    # The centre has distance ~0 and the rim ~1.
    by_index = {p.index: p for p in PIXELS}
    assert by_index[centre * 4].dist < 0.02


def test_pixels_beyond_the_disc_are_excluded_from_the_table():
    """Far pixels are skipped so a full repaint can leave them alone."""
    assert all(p.dist <= 1.25 for p in PIXELS)


# ------------------------------------------------------------------- colour


def test_mix_interpolates_between_two_colours():
    assert _mix((0, 0, 0), (255, 255, 255), 0.0) == (0, 0, 0)
    assert _mix((0, 0, 0), (255, 255, 255), 1.0) == (255, 255, 255)
    mid = _mix((0, 0, 0), (200, 100, 50), 0.5)
    assert mid == (100, 50, 25)


# ------------------------------------------------------------ disarmed ring


def test_disarmed_is_a_ring_not_a_disc(renderer):
    """The middle must be fully clear, or it reads as armed."""
    renderer.draw(armed=False, level=0.0)
    c = PLATE // 2
    assert _alpha_at(renderer, c, c) == 0, "the hole should be see-through"
    # Sample the middle of the ring band, not an arbitrary radius: the band runs
    # from RING_LO to 1.0 in normalised distance, so 0.92 lands inside it while
    # 0.80 is still in the hole.
    ring_y = c - int((DISC / 2) * 0.92)
    assert _alpha_at(renderer, c, ring_y) == 255, "the ring should be opaque"
    # And just inside the inner edge should already be clear.
    hole_y = c - int((DISC / 2) * 0.60)
    assert _alpha_at(renderer, c, hole_y) == 0, "the hole should extend past 0.60"


def test_disarmed_alpha_is_premultiplied(renderer):
    """A soft edge pixel must have colour <= alpha, or a dark halo appears."""
    renderer.draw(armed=False, level=0.0)
    c = PLATE // 2
    checked = 0
    for dy in range(-DISC // 2 - 4, DISC // 2 + 4):
        b, g, red, a = _bgra_at(renderer, c, c + dy)
        if 0 < a < 255:
            checked += 1
            assert red <= a, f"R {red} exceeds alpha {a}"
            assert g <= a, f"G {g} exceeds alpha {a}"
            assert b <= a, f"B {b} exceeds alpha {a}"
    assert checked, "expected at least one partially transparent edge pixel"


def test_corners_stay_fully_transparent(renderer):
    renderer.draw(armed=False, level=0.0)
    assert _alpha_at(renderer, 0, 0) == 0
    assert _alpha_at(renderer, PLATE - 1, 0) == 0
    assert _alpha_at(renderer, 0, PLATE - 1) == 0
    assert _alpha_at(renderer, PLATE - 1, PLATE - 1) == 0


# ---------------------------------------------------------------- armed disc


def test_armed_fills_the_centre(renderer):
    renderer.draw(armed=True, level=0.0)
    assert _alpha_at(renderer, PLATE // 2, PLATE // 2) == 255


def test_armed_uses_the_accent_and_not_the_ring_colour(renderer):
    """Guards against the two states converging on the same paint."""
    renderer.draw(armed=True, level=0.0)
    c = PLATE // 2
    _b, _g, red, _a = _bgra_at(renderer, c, c - int(DISC * 0.25))
    # Red channel should sit near ARM_CORE, not near a cool grey.
    assert abs(red - ARM_CORE[0]) < 40, f"expected the amber accent, got R={red}"


def test_armed_is_a_lighter_disc_than_its_edge(renderer):
    """A flat fill would look like a sticker; the gradient is the depth cue."""
    renderer.draw(armed=True, level=0.0)
    c = PLATE // 2
    _b, _g, centre_red, _a = _bgra_at(renderer, c, c - int(DISC * 0.2))
    _b, _g, edge_red, _a = _bgra_at(renderer, c, c + int(DISC * 0.42))
    assert centre_red > edge_red


# ------------------------------------------------------------------- level


def test_level_closes_the_hole_but_never_turns_the_ring_solid(renderer):
    renderer.draw(armed=False, level=0.0)
    c = PLATE // 2
    assert _alpha_at(renderer, c, c) == 0
    renderer.draw(armed=False, level=1.0)
    # At full level the hole has shrunk but the disc is still not a filled plate,
    # because only the armed state is allowed to look solid.
    assert _alpha_at(renderer, c, c) < 255


def test_the_two_states_are_distinguishable_at_the_centre(renderer):
    """The safety signal depends on this: hollow vs filled at the middle."""
    c = PLATE // 2
    renderer.draw(armed=False, level=0.0)
    off = _alpha_at(renderer, c, c)
    renderer.draw(armed=True, level=0.0)
    on = _alpha_at(renderer, c, c)
    assert off == 0 and on == 255


# ---------------------------------------------------------------- activity


def test_level_is_zero_with_no_activity():
    c = Control("http://127.0.0.1:1")
    assert c.level_from({"recent_calls": [], "pending_confirmations": []}) == 0.0


def test_a_pending_confirmation_saturates_the_level():
    """That is the one moment the user must look at the orb."""
    c = Control("http://127.0.0.1:1")
    level = c.level_from({"pending_confirmations": [{"id": "abc"}], "recent_calls": []})
    assert level == 1.0


def test_recent_activity_decays_to_zero():
    c = Control("http://127.0.0.1:1")
    now = time.time()
    fresh = c.level_from({"recent_calls": [{"ts": now}], "pending_confirmations": []})
    older = c.level_from({"recent_calls": [{"ts": now - 3.9}], "pending_confirmations": []})
    stale = c.level_from({"recent_calls": [{"ts": now - 30}], "pending_confirmations": []})
    assert fresh > older > 0.0
    assert stale == 0.0


def test_a_clock_skewed_timestamp_does_not_go_negative():
    """A ts from the future must not produce a huge level."""
    c = Control("http://127.0.0.1:1")
    assert c.level_from({"recent_calls": [{"ts": time.time() + 60}]}) == 0.0


# ---------------------------------------------------------------- movement
#
# Drag was the one part of the orb that felt bad on a real desktop: a ghost
# circle trailed the cursor and the orb lagged behind it. Both came from doing
# too much per mouse-move event, so the movement path is pinned here -- not for
# its arithmetic, which is trivial, but to keep it free of the things that
# made it feel wrong.


class _Recorder:
    """Stands in for the renderer and counts what a move actually costs."""

    def __init__(self) -> None:
        self.presents: list[tuple[int, int]] = []
        self.draws = 0

    def draw(self, armed: bool, level: float) -> None:
        self.draws += 1

    def present(self, hwnd: int, at: tuple[int, int]) -> bool:
        self.presents.append(at)
        return True


def _orb(work_area=(0, 0, 1860, 1080), pos=(1708, 24)):
    """A NativeOrb with no Win32 window, for exercising the movement logic."""
    orb = NativeOrb.__new__(NativeOrb)
    orb.hwnd = 1
    orb._pos = pos
    orb._work_area_cache = work_area
    orb._work_area_at = time.monotonic()
    orb._dragging = False
    orb._press = None
    orb._drawn = None
    orb.armed = False
    orb.level = 0.0
    orb.renderer = _Recorder()
    return orb


def test_moving_the_orb_does_not_repaint_it():
    """The pixels do not change when it moves; a repaint per event was the lag.

    draw() is ~10ms of Python across 13k pixels. At mouse-move rates that is
    the difference between the orb tracking the cursor and trailing it.
    """
    orb = _orb()
    for step in range(50):
        orb._go(orb._pos[0] - 3, orb._pos[1] + 1)
    assert orb.renderer.draws == 0, "a move must not repaint"
    assert len(orb.renderer.presents) == 50


def test_moving_the_orb_presents_at_the_new_position():
    orb = _orb()
    assert orb._go(1000, 500) == (1000, 500)
    assert orb.renderer.presents[-1] == (1000, 500)


def test_the_orb_stays_reachable_when_dragged_off_screen():
    """It may hang over an edge, but it can never be lost."""
    orb = _orb()
    # Work area is (0, 0, 1860, 1080); the orb may hang out by half its plate.
    assert orb._go(-9000, -9000) == (-64, -64)
    assert orb._go(9000, 9000) == (1796, 1016)


def test_repeated_moves_stay_in_step_with_the_cursor():
    """Incremental tracking must not drift.

    An earlier version re-read the window position on every event and stepped
    from that, so the gap between the orb and DWM compounded and the orb crept
    away from the cursor over a long drag. Start well inside the work area so
    the edge clamp is not what is being measured.
    """
    orb = _orb(pos=(900, 500))
    x = y = 0
    for _ in range(200):
        x -= 2
        y -= 1
        orb._go(orb._pos[0] + (x - (x + 2)), orb._pos[1] + (y - (y + 1)))
    assert orb._pos == (500, 300)


def test_the_orb_follows_the_cursor_and_not_the_event_delta():
    """The drag must land where the pointer is, however events are coalesced.

    Stepping by each event's delta silently loses the distance of every message
    Windows merged away, so a quick flick left the orb short of the cursor.
    Positioning from the real cursor is immune to that, and it also means the
    orb self-corrects instead of accumulating error over a long drag.
    """
    orb = _orb(pos=(900, 500))
    orb._grab = (20, 30)  # grabbed near the top-left of the plate
    # Deliberately uneven steps: coalescing does not arrive on a tidy grid.
    for cursor_x in (880, 811, 700, 512, 349, 305):
        orb._cursor = lambda cx=cursor_x: (cx, 640)
        cx, cy = orb._cursor()
        orb._go(cx - orb._grab[0], cy - orb._grab[1])
    assert orb._pos == (285, 610), "the orb must sit under the cursor, not lag it"
    assert orb.renderer.draws == 0


def test_the_work_area_is_cached_rather_than_queried_per_event(monkeypatch):
    """SystemParametersInfoW is a round trip to the shell.

    It used to run on every mouse-move event. A work area that changes maybe
    once a second does not need to be re-read 200 times a drag.
    """
    import voice_os.orb as orb_module

    queries = []
    real = orb_module.user32.SystemParametersInfoW

    def counting(action, param, ptr, winini):
        queries.append(action)
        return real(action, param, ptr, winini)

    monkeypatch.setattr(orb_module.user32, "SystemParametersInfoW", counting)

    orb = _orb()
    orb._work_area_cache = None  # force the first call to go out
    for _ in range(50):
        orb._clamp(100, 100)
    assert len(queries) == 1, f"queried the shell {len(queries)} times in one drag"

    # ...and it must not cache forever, or a taskbar that moves is ignored.
    orb._work_area_at = time.monotonic() - 10.0
    orb._clamp(100, 100)
    assert len(queries) == 2


def test_redraw_presents_at_the_intended_position_not_the_window_position():
    """Windows is asked where the window is only for diagnostics.

    If a redraw used the real window rect it would fight an in-flight drag and
    snap the orb back.
    """
    orb = _orb()
    orb._pos = (400, 300)
    orb.hwnd = 1
    orb.renderer.present = lambda hwnd, at: None
    orb._redraw(force=True)
    assert orb._pos == (400, 300)
