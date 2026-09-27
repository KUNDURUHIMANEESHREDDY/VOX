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
#:
#: The page is a single circle now -- no captions, no plate -- so the window is
#: the orb plus its padding and nothing grows. Fixed on all three axes on
#: purpose: a window that resizes mid-drag makes the orb slide out from under
#: the cursor.
OVERLAY_WIDTH = 116
OVERLAY_HEIGHT = 116

#: Bounds for the dynamic height, in CSS pixels. The max keeps a runaway
#: transcript from eating the corner; beyond it the text stack scrolls.
OVERLAY_MIN_HEIGHT = 116
OVERLAY_MAX_HEIGHT = 116

#: Gap from the screen edge, in CSS pixels.
MARGIN = 16

#: The overlay is made see-through by shaping the *window*, not by alpha.
#:
#: What has been tried on this machine, all with WebView2 1.8-era pywebview:
#:
#: 1. ``transparent=True`` (pywebview's own). It sets WebView2's
#:    ``DefaultBackgroundColor`` to transparent but never touches the WinForms
#:    form behind it, so the page goes transparent and reveals a *white* form.
#:    Measured: an opaque white box over the desktop.
#: 2. Colour key (``WS_EX_LAYERED`` + ``LWA_COLORKEY``) on the top-level
#:    window. The WebView2 control is a child HWND painting its own surface, so
#:    the key never reaches the pixels. Measured: still white.
#: 3. The same colour key applied to the child windows as well.
#:    ``Chrome_RenderWidgetHostHWND`` is a DirectComposition surface; forcing
#:    it layered renders the window solid black. Measured: black.
#: 4. ``SetWindowRgn`` on the top-level and every child. Regions do work on
#:    this window tree (a rectangular region clips correctly), which is the
#:    remaining route, but the elliptical/annular region has not been made to
#:    stick yet.
#:
#: So the overlay currently hides itself rather than sit on the desktop in
#: white. A floating widget that covers the screen is worse than no widget, and
#: the console in the browser still works and still arms and disarms.
#:
#: The route that will work is a native GDI orb: a layered window drawn with
#: ``UpdateLayeredWindow`` and per-pixel alpha, with no WebView2 involved.
#: WebView2's renderer is a DirectComposition surface and cannot composite
#: alpha into a desktop window on this machine at all.
RGN_DIFF = 3  # outer minus inner, i.e. an annulus


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


#: The JS bridge, so the placement thread can hand it the native handle once the
#: window exists. The page then reshapes the window itself as the level changes.
_api_singleton = None


def _set_hwnd(hwnd: int) -> None:
    if _api_singleton is not None:
        _api_singleton._hwnd = hwnd


def _set_region(hwnd: int, inner_frac: float, outer_frac: float) -> bool:
    """Clip the window to the orb: an annulus, or a disc when there is no hole.

    ``inner_frac`` and ``outer_frac`` are fractions of the window's half-width.
    An ``inner_frac`` at or above ``outer_frac`` means a filled disc, which is
    the armed state. The page drives both from its own geometry, so what the
    window clips to always matches what the page painted.
    """
    import ctypes

    import win32gui

    # The region functions live in gdi32, not pywin32 -- win32gui has no
    # CreateEllipticRgn -- so they are called through ctypes. The argtypes are
    # not optional: without them ctypes truncates the region handles and
    # CombineRgn comes back NULL.
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
    gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
    gdi32.CombineRgn.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int
    ]
    gdi32.CombineRgn.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowRgn.restype = ctypes.c_int

    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w = max(1, right - left)
        h = max(1, bottom - top)
        half_w, half_h = w / 2.0, h / 2.0
        cx, cy = int(round(half_w)), int(round(half_h))

        def ellipse(frac: float):
            rx = max(1, int(round(frac * half_w)))
            ry = max(1, int(round(frac * half_h)))
            return gdi32.CreateEllipticRgn(cx - rx, cy - ry, cx + rx, cy + ry)

        outer_f = max(0.02, min(0.98, float(outer_frac)))
        inner_f = max(0.0, min(1.0, float(inner_frac)))
        outer = ellipse(outer_f)
        if not outer:
            return False
        if 0.02 < inner_f < outer_f:
            inner = ellipse(inner_f)
            # CombineRgn(dest, src1, src2, mode): dest is both destination and
            # the first operand, so src1 must be the outer ellipse as well.
            region = gdi32.CombineRgn(outer, outer, inner, RGN_DIFF)
            gdi32.DeleteObject(ctypes.c_void_p(outer))
            if inner:
                gdi32.DeleteObject(ctypes.c_void_p(inner))
        else:
            region = outer
        if not region:
            return False
        # SetWindowRgn takes ownership of the region on success; deleting it
        # afterwards would be a use-after-free.
        if not user32.SetWindowRgn(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(region), 1
        ):
            gdi32.DeleteObject(ctypes.c_void_p(region))
            logger.warning("SetWindowRgn was refused for the overlay")
            return False
        return True
    except Exception as exc:
        logger.warning("could not shape the overlay window: %s", exc)
        return False


def _place_own_window(corner: Corner, timeout: float = 30.0, window=None) -> None:
    """Size this process's window and pin it to the requested corner.

    Runs on a daemon thread: pywebview owns the main thread and the window does
    not exist until ``webview.start()`` has been called.

    No window region is applied. The page fills itself with :data:`KEY_COLOUR`
    and the window is layered with a colour key on that colour, so only the orb
    paints and the desktop shows through everywhere else. A region would only
    clip the ring the moment it grew.
    """
    try:
        hwnd = _find_own_window(timeout)
        if hwnd is None:
            logger.warning("overlay window never appeared")
            return
        shaped = _set_region(hwnd, inner_frac=0.66, outer_frac=0.80)
        _set_hwnd(hwnd)
        rect = _force_size(hwnd, corner)
        if not shaped:
            # Do not leave a white rectangle sitting on someone's desktop.
            # An overlay that covers the screen in white is worse than no
            # overlay at all, so the window is hidden and the reason logged.
            import win32con
            import win32gui

            logger.error(
                "the overlay could not be made see-through, so it is being hidden. "
                "Leaving it up would cover the desktop in white. See the note on "
                "transparency in KEY_COLOUR's comment above."
            )
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
        logger.info(
            "overlay %dx%d at %s (%d,%d) see_through=%s",
            rect[2] - rect[0], rect[3] - rect[1], corner.name, rect[0], rect[1],
            "yes" if shaped else "NO (hidden)",
        )
    except Exception as exc:
        logger.warning("could not place the overlay window: %s", exc)


class _OverlayApi:
    """JS bridge for the overlay page. ``fit`` is called from ``fitWindow`` in
    web/overlay.html whenever the text stack changes size."""

    def __init__(self) -> None:
        self._window = None
        self._fix_point = None
        #: Native handle, learned by the placement thread once the window exists.
        self._hwnd: int | None = None

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

    def move_to(self, x, y):
        """Move the window to an absolute screen position, for dragging.

        Absolute rather than a delta because a dragged window travels *with* the
        cursor: once the window follows the pointer, the pointer's
        window-relative coordinates stop changing, so any delta computed from
        them collapses to zero and the orb sticks. The page therefore sends the
        absolute target it computed from screen coordinates.

        Clamped to the work area here, so the orb cannot be dragged off-screen
        and lost. Returns the position actually used, which is what the page
        should treat as the new origin once it hits an edge.
        """
        import ctypes

        try:
            want_x = int(float(x))
            want_y = int(float(y))
        except (TypeError, ValueError):
            return None
        window = self._window
        if window is None:
            return None
        try:
            hwnd = window.native.Handle
        except Exception:
            try:
                hwnd = int(window.native)
            except Exception as exc:
                logger.debug("overlay could not resolve a native handle: %s", exc)
                return None

        try:
            left, top, right, bottom = _window_rect(hwnd)
            work = _work_area()
            width = right - left
            height = bottom - top
            # A few pixels of slack absorb the invisible border pywebview adds,
            # which GetWindowRect counts but the user cannot see.
            nx = min(max(want_x, work[0] - 8), max(work[0], work[2] - width + 8))
            ny = min(max(want_y, work[1] - 8), max(work[1], work[3] - height + 8))
            ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                None,
                int(nx),
                int(ny),
                0,
                0,
                0x0004 | 0x0010,  # SWP_NOZORDER | SWP_NOACTIVATE
            )
            return [int(nx), int(ny)]
        except Exception as exc:
            logger.debug("overlay move_to failed: %s", exc)
            return None

    def position(self):
        """Current window position in screen pixels, or None."""
        window = self._window
        if window is None:
            return None
        try:
            left, top, _right, _bottom = _window_rect(window.native.Handle)
            return [int(left), int(top)]
        except Exception:
            return None

    def set_shape(self, inner_frac, outer_frac):
        """Tell the window how to clip itself to the orb.

        The page owns the geometry, so it reports the radii it actually painted
        and the window clips to match. An ``inner_frac`` at or above
        ``outer_frac`` gives a filled disc, which is how the armed state is
        drawn.

        Called on every frame where the level changes, so it is throttled by the
        page: SetWindowRgn is not free and a region per frame at 60fps would
        stall the UI thread.
        """
        hwnd = self._hwnd
        if hwnd is None:
            return False
        try:
            return _set_region(hwnd, float(inner_frac), float(outer_frac))
        except (TypeError, ValueError):
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
        # transparent is deliberately OFF. It sets WebView2's background to
        # transparent, which on this machine exposes the white WinForms form
        # behind the page, so the window covers the desktop in white. The page
        # owns transparency instead: it fills itself with KEY_COLOUR, and
        # _apply_colour_key keys that colour out.
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
        global _api_singleton
        _api_singleton = api
        window = webview.create_window(
            "VOX",
            url,
            width=OVERLAY_WIDTH,
            height=OVERLAY_HEIGHT,
            x=x,
            y=y,
            min_size=(OVERLAY_WIDTH, OVERLAY_MIN_HEIGHT),
            frameless=True,
            on_top=True,
            shadow=False,
            # transparency comes from the colour key, not from pywebview
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
    threading.Thread(
    target=_place_own_window, args=(corner, 30.0, window), daemon=True
).start()
    webview.start()
    return 0


def main(cfg: Config) -> int:  # pragma: no cover - thin process wrapper
    return run(cfg)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
