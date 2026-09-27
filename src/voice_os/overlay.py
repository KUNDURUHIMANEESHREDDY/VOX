"""Always-on-top desktop overlay.

A small frameless transparent window pinned to one corner of the screen showing
the orb plus live subtitles, so the conversation is visible without a chat
window covering the desktop.

Runs as its own process (``python -m voice_os overlay``) rather than a
thread inside the worker: pywebview needs to own the main thread for its native
window, and the worker needs that thread for asyncio. Two processes, no fight.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Config

logger = logging.getLogger("voice_os.overlay")

#: Diameter of the orb itself, in CSS pixels.
#:
#: Set to 50 on request. Note the trade-off: at 50 CSS px (62 physical px) the
#: orb is too small to read amplitude at a glance. The ChatGPT reference in
#: ``refs/chatgpt-voice/reddit-blue-orb.jpg`` measures 527px on a 1080px phone
#: (48.8% of screen width), which would be ~750 CSS px here.
ORB_SIZE = 50

#: Window size in CSS pixels. The height is only the starting point: the page
#: measures its own text stack and asks Python to grow/shrink the window to
#: fit (see ``fit`` in web/overlay.html and ``_OverlayApi`` below), so a long
#: caption or toast never overflows and a short one never leaves dead space.
#:
#: Width stays fixed; only height is dynamic.
OVERLAY_WIDTH = 220
OVERLAY_HEIGHT = 150

#: Bounds for the dynamic height, in CSS pixels. The max keeps a runaway
#: transcript from eating the corner; beyond it the text stack scrolls.
OVERLAY_MIN_HEIGHT = 110
OVERLAY_MAX_HEIGHT = 430

#: Gap from the screen edge, in CSS pixels.
MARGIN = 16


@dataclass(frozen=True)
class Corner:
    name: str
    # (x_fraction, y_fraction) anchoring the window inside the work area.
    fx: float
    fy: float


CORNERS: dict[str, Corner] = {
    "bottom-right": Corner("bottom-right", 1.0, 1.0),
    "bottom-left": Corner("bottom-left", 0.0, 1.0),
    "top-right": Corner("top-right", 1.0, 0.0),
    "top-left": Corner("top-left", 0.0, 0.0),
}


def control_url(cfg: Config, path: str = "/overlay") -> str:
    return f"http://{cfg.control_host}:{cfg.control_port}{path}"


def wait_for_control_server(cfg: Config, timeout: float = 20.0) -> bool:
    """Block until the worker's control server answers /api/health."""
    url = control_url(cfg, "/api/health")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, socket.timeout, OSError):
            time.sleep(0.5)
    return False


def _screen_box() -> tuple[int, int, int, int]:
    """Primary monitor **work area** as (x, y, width, height).

    Win32 is used rather than ``webview.screens`` for two reasons: pywebview's
    ``Screen`` has no ``primary`` attribute, and it does not report the work
    area. The work area is what matters here, because the taskbar overlaps the
    monitor rect and would otherwise sit on top of the overlay.
    """
    try:
        import win32api
        import win32con

        hmon = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        work = win32api.GetMonitorInfo(hmon)["Work"]
        return work[0], work[1], work[2] - work[0], work[3] - work[1]
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("could not read the monitor work area: %s", exc)

    # Last resort only. Preferring a smaller guess would push the window
    # half off a large screen; this is a guess, not a measurement.
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return (
            0,
            0,
            user32.GetSystemMetrics(0),  # SM_CXSCREEN
            user32.GetSystemMetrics(1),  # SM_CYSCREEN
        )
    except Exception:
        return (0, 0, 1920, 1080)


def compute_position(cfg: Config, corner: Corner) -> tuple[int, int]:
    """Place the window in the requested corner of the work area."""
    x0, y0, width, height = _screen_box()
    if corner.fx >= 1.0:
        x = x0 + width - OVERLAY_WIDTH - MARGIN
    else:
        x = x0 + MARGIN
    if corner.fy >= 1.0:
        y = y0 + height - OVERLAY_HEIGHT - MARGIN
    else:
        y = y0 + MARGIN
    return max(x0, x), max(y0, y)


def _find_own_window(timeout: float) -> int | None:
    """Locate this process's native window.

    Matching on the title is unreliable: WebView2 overwrites the window title
    with the page's ``<title>``, so the handle pywebview was given does not
    survive. The window's owning process id never lies.
    """
    import win32gui
    import win32process

    own = os.getpid()
    found: int | None = None

    def callback(hwnd: int, _extra: object) -> None:
        nonlocal found
        if found is not None:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if pid == own and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            # Ignore helper/tooltip windows; we want the real client window.
            if rect[2] - rect[0] > 40 and rect[3] - rect[1] > 40:
                found = hwnd

    deadline = time.time() + timeout
    while time.time() < deadline and found is None:
        win32gui.EnumWindows(callback, None)
        if found is None:
            time.sleep(0.3)
    return found


def _work_area() -> tuple[int, int, int, int]:
    """Work area as (x, y, right, bottom), in this process's coordinate space.

    ``SPI_GETWORKAREA`` is used instead of the monitor rect so the taskbar is
    respected, and because it reports in the caller's DPI context — which is the
    same context the window was created in, so the numbers are directly usable.
    """
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class APPDATA(ctypes.Structure):
        _fields_ = [("rc", RECT), ("f", ctypes.c_uint)]

    SPI_GETWORKAREA = 48
    area = APPDATA()
    if not ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETWORKAREA, ctypes.sizeof(area), ctypes.byref(area), 0
    ):
        return (0, 0, OVERLAY_WIDTH, OVERLAY_HEIGHT)
    return (area.rc.left, area.rc.top, area.rc.right, area.rc.bottom)


def _dpi(hwnd: int) -> int:
    """DPI this window is viewed at, falling back to 96 (100% scaling)."""
    try:
        got = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
        if got:
            return int(got)
    except (AttributeError, OSError):
        pass  # pre-Windows 10.1607
    return 96


def _force_size(hwnd: int, corner: Corner) -> tuple[int, int, int, int]:
    """Resize the window and pin it to the requested corner.

    Two things are corrected here rather than trusting pywebview:

    * It does not honour ``height`` reliably — a 200x200 request produced a
      200x162 window, which squashed a circular clip into an ellipse.
    * The size it does apply is in an ambiguous coordinate space, so a given
      value can land as either logical or physical pixels depending on the
      process's DPI awareness.

    ``GetDpiForWindow`` resolves the second one: it reports the DPI *this*
    window is currently viewed at, which is the same coordinate space
    ``SetWindowPos`` accepts, so ``css * dpi / 96`` is always the intended
    physical size.

    Returns the final rect.
    """
    import win32gui

    left, top, right, bottom = _work_area()
    scale = _dpi(hwnd) / 96
    width = max(1, round(OVERLAY_WIDTH * scale))
    height = max(1, round(OVERLAY_HEIGHT * scale))

    x = right - width - MARGIN if corner.fx >= 1.0 else left + MARGIN
    y = bottom - height - MARGIN if corner.fy >= 1.0 else top + MARGIN

    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    ctypes.windll.user32.SetWindowPos(
        ctypes.c_void_p(hwnd),
        None,
        int(x),
        int(y),
        int(width),
        int(height),
        SWP_NOZORDER | SWP_NOACTIVATE,
    )
    rect = win32gui.GetWindowRect(hwnd)
    logger.debug(
        "work area=(%d,%d,%d,%d) dpi=%.0f target=%dx%d",
        left, top, right, bottom, _dpi(hwnd), width, height,
    )
    return rect


def _place_own_window(corner: Corner, timeout: float = 30.0) -> None:
    """Size this process's window and pin it to the requested corner.

    Runs on a daemon thread: pywebview owns the main thread and the window does
    not exist until ``webview.start()`` has been called.

    No window region is applied. The window is transparent (``transparent=True``
    in :func:`run`, which sets WebView2's ``DefaultBackgroundColor`` to
    transparent), so only the pills and the orb paint — the surround shows the
    desktop. A region would only clip the text the moment it grew.
    """
    try:
        hwnd = _find_own_window(timeout)
        if hwnd is None:
            logger.warning("overlay window never appeared")
            return
        rect = _force_size(hwnd, corner)
        logger.info(
            "overlay %dx%d at %s (%d,%d)",
            rect[2] - rect[0], rect[3] - rect[1], corner.name, rect[0], rect[1],
        )
    except Exception as exc:
        logger.warning("could not place the overlay window: %s", exc)


class _OverlayApi:
    """JS bridge for the overlay page. ``fit`` is called from ``fitWindow`` in
    web/overlay.html whenever the text stack changes size."""

    def __init__(self) -> None:
        self._window = None
        self._fix_point = None

    def attach(self, window, fix_point) -> None:
        self._window = window
        self._fix_point = fix_point

    def fit(self, width, height):
        """Resize the window to the measured content size (CSS pixels)."""
        try:
            w = int(float(width))
            h = int(float(height))
        except (TypeError, ValueError):
            return False
        w = max(80, min(OVERLAY_WIDTH, w))
        h = max(OVERLAY_MIN_HEIGHT, min(OVERLAY_MAX_HEIGHT, h))
        window = self._window
        if window is None:
            return False
        try:
            window.resize(w, h, self._fix_point)
            return True
        except Exception as exc:
            logger.debug("overlay fit(%d, %d) failed: %s", w, h, exc)
            return False


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    import win32gui

    return win32gui.GetWindowRect(hwnd)


def run(cfg: Config) -> int:
    """Open the overlay window. Blocks until the window closes."""
    try:
        import webview
    except ImportError:
        logger.error(
            "pywebview is not installed, so the overlay cannot start. "
            "Install it with: uv pip install pywebview"
        )
        return 1

    if not wait_for_control_server(cfg):
        logger.error(
            "the control server at %s is not responding. Start the agent first "
            "with ./run.ps1, then run: python -m voice_os overlay",
            control_url(cfg, ""),
        )
        return 1

    corner_name = (cfg.overlay_corner or "bottom-right").lower()
    corner = CORNERS.get(corner_name)
    if corner is None:
        logger.warning(
            "unknown OVERLAY_CORNER %r; falling back to bottom-right. Valid: %s",
            corner_name,
            ", ".join(CORNERS),
        )
        corner = CORNERS["bottom-right"]
    x, y = compute_position(cfg, corner)

    url = control_url(cfg, "/overlay") + f"?corner={corner.name}"

    try:
        # Only kwargs pywebview 5.x actually accepts. There is no
        # minimizable/maximizable; passing them raises TypeError.
        #
        # transparent=True works on Windows with the EdgeChromium backend:
        # pywebview sets WebView2's DefaultBackgroundColor to transparent, so
        # only painted elements (pills, orb) show and the surround is the
        # desktop itself. The page must therefore keep html/body transparent
        # and put its backgrounds on the pills — but we move them to
        # fully-transparent so the desktop shows through everywhere.
        from webview.window import FixPoint

        if corner.fx >= 1.0 and corner.fy >= 1.0:
            fix_point = FixPoint.SOUTH | FixPoint.EAST
        elif corner.fx >= 1.0:
            fix_point = FixPoint.NORTH | FixPoint.EAST
        elif corner.fy >= 1.0:
            fix_point = FixPoint.SOUTH | FixPoint.WEST
        else:
            fix_point = FixPoint.NORTH | FixPoint.WEST

        api = _OverlayApi()
        window = webview.create_window(
            "Voice OS",
            url,
            width=OVERLAY_WIDTH,
            height=OVERLAY_HEIGHT,
            x=x,
            y=y,
            min_size=(OVERLAY_WIDTH, OVERLAY_MIN_HEIGHT),
            frameless=True,
            on_top=True,
            shadow=False,
            transparent=True,
            easy_drag=False,
            resizable=False,
            text_select=False,
            js_api=api,
        )
        api.attach(window, fix_point)

    except Exception as exc:
        logger.error("could not create the overlay window: %s", exc)
        return 1

    logger.info(
        "overlay open in the %s corner at http://%s:%d/overlay",
        corner.name,
        cfg.control_host,
        cfg.control_port,
    )
    # pywebview owns the main thread, so the window has to be placed from a
    # helper thread once it actually exists.
    threading.Thread(target=_place_own_window, args=(corner,), daemon=True).start()
    webview.start()
    return 0


def main(cfg: Config) -> int:  # pragma: no cover - thin process wrapper
    return run(cfg)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
