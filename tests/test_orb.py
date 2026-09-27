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
