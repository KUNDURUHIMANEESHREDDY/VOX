"""Overlay window tests.

The overlay is a separate process that owns a native always-on-top window, so
the parts worth testing are the geometry maths and the failure modes. Nothing
here opens a real window.
"""

from __future__ import annotations

import pytest

from voice_os.config import Config, LiveKitConfig, SafetyConfig, TurnConfig
from voice_os.overlay import (
    CORNERS,
    MARGIN,
    OVERLAY_HEIGHT,
    OVERLAY_WIDTH,
    compute_position,
    control_url,
    wait_for_control_server,
)


def _cfg(port: int = 8787) -> Config:
    return Config(
        livekit=LiveKitConfig(),
        llm=__import__("voice_os.config", fromlist=["LLMConfig"]).LLMConfig(),
        stt=__import__("voice_os.config", fromlist=["STTConfig"]).STTConfig(),
        tts=__import__("voice_os.config", fromlist=["TTSConfig"]).TTSConfig(),
        turn=TurnConfig(),
        safety=SafetyConfig(),
        control_port=port,
    )


# ---------------------------------------------------------------- geometry

def test_all_four_corners_are_defined():
    assert set(CORNERS) == {"bottom-right", "bottom-left", "top-right", "top-left"}


@pytest.mark.parametrize("corner", sorted(CORNERS))
def test_position_stays_inside_the_screen(monkeypatch, corner):
    """Whatever the corner, the window must not hang off the edge.

    Regression risk: a window that lands partially offscreen looks like the app
    failed to start at all.
    """
    import voice_os.overlay as overlay

    screen_w, screen_h = 1920, 1080
    monkeypatch.setattr(overlay, "_screen_box", lambda: (0, 0, screen_w, screen_h))

    x, y = compute_position(_cfg(), CORNERS[corner])

    assert 0 <= x, f"{corner} x is offscreen: {x}"
    assert 0 <= y, f"{corner} y is offscreen: {y}"
    assert x + OVERLAY_WIDTH <= screen_w, f"{corner} overflows the right edge"
    assert y + OVERLAY_HEIGHT <= screen_h, f"{corner} overflows the bottom edge"
    assert x >= MARGIN or x == 0
    assert y >= MARGIN or y == 0


def test_corners_are_actually_different(monkeypatch):
    import voice_os.overlay as overlay

    monkeypatch.setattr(overlay, "_screen_box", lambda: (0, 0, 1920, 1080))
    positions = {c: compute_position(_cfg(), CORNERS[c]) for c in CORNERS}
    assert len(set(positions.values())) == 4, positions


def test_position_respects_a_virtual_desktop_offset(monkeypatch):
    """Multi-monitor: the work area can start at a negative or offset origin."""
    import voice_os.overlay as overlay

    monkeypatch.setattr(overlay, "_screen_box", lambda: (1920, 0, 2560, 1440))
    x, y = compute_position(_cfg(), CORNERS["bottom-right"])
    assert x >= 1920
    assert x + OVERLAY_WIDTH <= 1920 + 2560
    assert y >= 0


def test_position_clamps_when_the_screen_is_tiny(monkeypatch):
    """A screen smaller than the window must still produce a sane origin.

    The window will necessarily overflow, but it must never be positioned at a
    negative coordinate, which would make the window unreachable.
    """
    import voice_os.overlay as overlay

    monkeypatch.setattr(overlay, "_screen_box", lambda: (0, 0, 300, 200))
    for corner in CORNERS.values():
        x, y = compute_position(_cfg(), corner)
        assert x >= 0, f"{corner.name} went off the left edge: {x}"
        assert y >= 0, f"{corner.name} went off the top edge: {y}"


def test_top_left_corner_keeps_the_margin(monkeypatch):
    import voice_os.overlay as overlay

    monkeypatch.setattr(overlay, "_screen_box", lambda: (0, 0, 1920, 1080))
    x, y = compute_position(_cfg(), CORNERS["top-left"])
    assert (x, y) == (MARGIN, MARGIN)


# -------------------------------------------------------------------- urls

def test_control_url_points_at_the_overlay_page():
    assert control_url(_cfg(9000), "/overlay") == "http://127.0.0.1:9000/overlay"


def test_wait_for_control_server_gives_up_quickly_when_nothing_is_listening():
    # Port 1 is privileged and unbound, so this fails fast and deterministically.
    assert wait_for_control_server(_cfg(1), timeout=1.5) is False
