"""A native always-on-top orb, drawn with GDI and per-pixel alpha.

Why this exists instead of a WebView2 window
--------------------------------------------
The obvious way to build a desktop HUD is a small frameless browser window.
That does not work here, and it was measured rather than assumed:

* pywebview's ``transparent=True`` makes WebView2's background transparent but
  never touches the WinForms form behind it, so the page goes transparent and
  reveals a *white* form. The window covers the desktop in white.
* A colour key (``WS_EX_LAYERED`` + ``LWA_COLORKEY``) on the top-level window
  does nothing, because the WebView2 control is a child HWND painting its own
  surface.
* Applying the key to the child windows as well renders the window solid
  black: ``Chrome_RenderWidgetHostHWND`` is a DirectComposition surface and
  cannot be layered.

WebView2's renderer cannot composite alpha into a desktop window on this
machine. So the orb drops the browser engine entirely: a plain Win32 layered
window, a 32-bit top-down DIB, and ``UpdateLayeredWindow`` with real per-pixel
alpha. Nothing to composite, so nothing to fail.

What it draws
-------------
A bare circle, nothing attached. Two states told apart by shape rather than
hue, because shape survives greyscale and peripheral vision:

* **disarmed** - a ring with a hole in it. The desktop shows through the middle.
* **armed** - a filled amber disc.

Amplitude is derived from the control server's own activity feed rather than
from audio, so the orb needs no audio path of its own. It reacts when the agent
gates a tool call or is waiting on a confirmation.

Cost
----
The orb is static when nothing is happening: it polls four times a second and
does not redraw at all unless the armed state changed or the activity level
moved enough to be visible. Idle cost is a handful of HTTP requests.
"""

from __future__ import annotations

import ctypes
import json
import logging
import struct
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger("voice_os.orb")

# --------------------------------------------------------------------- win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wintypes.ATOM
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.SetCapture.restype = wintypes.HWND
user32.SetCapture.argtypes = [wintypes.HWND]
user32.ReleaseCapture.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.HDC, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]
user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT
]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
    wintypes.HANDLE, wintypes.DWORD,
]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_PAINT = 0x000F
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_RBUTTONUP = 0x0205
WM_NCHITTEST = 0x0084
WM_ERASEBKGND = 0x0014

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
SW_SHOW = 5
SW_HIDE = 0
HTCLIENT = 1
IDC_ARROW = 32512
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
DI_NORMAL = 0x0000

#: Physical size of the orb, and the plate the DIB covers. Slightly larger than
#: the disc so the soft edge is not clipped.
DISC = 104
PLATE = DISC + 24


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", wintypes.BYTE),
        ("BlendFlags", wintypes.BYTE),
        ("SourceConstantAlpha", wintypes.BYTE),
        ("AlphaFormat", wintypes.BYTE),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


# ------------------------------------------------------------------ geometry


@dataclass
class _Pixel:
    """Precomputed per-pixel data.

    The distance field and the lighting term never change, so they are computed
    once. Per frame then costs one comparison and a colour pick per pixel, which
    is what keeps a pure-Python orb viable at 116x116.
    """

    dist: float  # 0.0 at the centre, 1.0 at the disc edge
    light: float  # 0.0 at the bottom, 1.0 at the top
    index: int  # byte offset into the DIB


def _build_pixels() -> list[_Pixel]:
    c = (PLATE - 1) / 2.0
    r = DISC / 2.0
    out: list[_Pixel] = []
    for y in range(PLATE):
        for x in range(PLATE):
            dx = (x - c) / r
            dy = (y - c) / r
            d = (dx * dx + dy * dy) ** 0.5
            if d > 1.25:
                continue  # fully outside; leave alpha 0 and never touch it again
            out.append(_Pixel(dist=d, light=(-dy + 1.0) / 2.0, index=(y * PLATE + x) * 4))
    return out


PIXELS = _build_pixels()

#: Colours, as (r, g, b) at full alpha. Amber for armed, cool graphite for the
#: disarmed ring: one accent, desaturated, and only ever in the armed state.
ARM_CORE = (232, 176, 108)
ARM_EDGE = (138, 84, 34)
RING_LIGHT = (237, 240, 244)
RING_MID = (201, 205, 212)
RING_DARK = (42, 46, 53)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


class OrbRenderer:
    """Paints the orb into a 32-bit DIB with premultiplied BGRA.

    ``UpdateLayeredWindow`` requires premultiplied alpha, which is the one easy
    thing to get wrong: writing straight RGBA with a soft edge produces a dark
    halo, because the colour channels have to be scaled by the alpha too.
    """

    def __init__(self) -> None:
        self.size = PLATE
        self.hdc_screen = user32.GetDC(None)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = PLATE
        info.bmiHeader.biHeight = -PLATE  # negative => top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB

        self.bits = ctypes.c_void_p()
        self.hbm = gdi32.CreateDIBSection(
            self.hdc_mem, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(self.bits), None, 0
        )
        if not self.hbm:
            raise OSError("CreateDIBSection failed", ctypes.get_last_error())
        self.old = gdi32.SelectObject(self.hdc_mem, self.hbm)
        self.buffer = (ctypes.c_ubyte * (PLATE * PLATE * 4)).from_address(self.bits.value)
        # Start fully transparent rather than zeroed-with-alpha-undefined.
        self.clear()

    def clear(self) -> None:
        ctypes.memset(self.bits, 0, PLATE * PLATE * 4)

    def draw(self, armed: bool, level: float) -> None:
        """Repaint the disc.

        ``level`` is 0..1 activity. In the ring state it closes the hole; in the
        filled state it only brightens, so an armed orb never looks disarmed.
        """
        buf = self.buffer
        if armed:
            hole = 0.0
            ring_lo = 0.0
        else:
            # A hairline at rest that thickens and closes as level rises, so
            # speech reads as the circle filling in. Kept thin deliberately: a
            # fat ring looks like a donut sitting on the wallpaper.
            ring_lo = max(0.0, 0.84 - 0.10 * level)
            hole = max(0.0, ring_lo - 0.30 - 0.22 * level)
        ring_hi = 1.0
        # Antialias width, in normalised distance (~1.5px at DISC=104).
        soft = 1.6 / (DISC / 2.0)
        inner_soft = 0.05

        for p in PIXELS:
            i = p.index
            d = p.dist
            if d > ring_hi:
                buf[i + 3] = 0
                continue

            if armed:
                colour = _mix(ARM_EDGE, ARM_CORE, max(0.0, 1.0 - d * d))
                a = 255 if d <= ring_hi - soft else int(255 * max(0.0, (ring_hi - d) / soft))
            else:
                if d < hole:
                    buf[i + 3] = 0
                    continue
                # Light top, dark bottom: the physical lip that keeps the ring
                # legible on a white wallpaper as well as a dark one.
                if p.light > 0.55:
                    colour = _mix(RING_MID, RING_LIGHT, (p.light - 0.55) / 0.45)
                else:
                    colour = _mix(RING_DARK, RING_MID, p.light / 0.55)
                # Both edges feathered, so the ring has no jagged steps.
                a = 255
                if d < ring_lo + inner_soft:
                    a = int(a * max(0.0, (d - ring_lo) / inner_soft))
                if d > ring_hi - soft:
                    a = min(a, int(255 * max(0.0, (ring_hi - d) / soft)))

            if a <= 0:
                buf[i + 3] = 0
                continue
            if a > 255:
                a = 255
            # Premultiply: UpdateLayeredWindow composites as if over a
            # transparent surface, so the colour must already carry the alpha.
            buf[i + 0] = colour[2] * a // 255  # B
            buf[i + 1] = colour[1] * a // 255  # G
            buf[i + 2] = colour[0] * a // 255  # R
            buf[i + 3] = a

    def present(self, hwnd: int, at: tuple[int, int]) -> bool:
        """Push the DIB to the window as a layered, alpha-composited update.

        ``at`` is the orb's screen position, and it is not optional decoration:
        for a layered window ``UpdateLayeredWindow`` *moves* the window to
        ``pptDst``. Passing (0, 0) -- which looks like "no offset" -- pins the orb
        to the top-left corner of the screen, and every repaint drags it back
        there. That is exactly what happened before this was passed in.

        The DC coordinate space is the virtual screen's, so a negative x/y for a
        monitor left of or above the primary one still works.
        """
        dst = POINT(int(at[0]), int(at[1]))
        size = SIZE(PLATE, PLATE)
        src = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, 1)  # AC_SRC_ALPHA
        ok = user32.UpdateLayeredWindow(
            wintypes.HWND(hwnd), self.hdc_screen, ctypes.byref(dst), ctypes.byref(size),
            self.hdc_mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA,
        )
        if not ok:
            logger.warning(
                "UpdateLayeredWindow failed: %s", ctypes.get_last_error()
            )
        return bool(ok)

    def close(self) -> None:
        try:
            gdi32.SelectObject(self.hdc_mem, self.old)
            gdi32.DeleteObject(self.hbm)
            gdi32.DeleteDC(self.hdc_mem)
            user32.ReleaseDC(None, self.hdc_screen)
        except Exception:
            pass


# ------------------------------------------------------------------- control


class Control:
    """Just enough of the control server to drive the orb."""

    def __init__(self, base_url: str, timeout: float = 1.5) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def safety(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/safety", timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def disarm(self) -> dict:
        req = urllib.request.Request(
            f"{self.base}/api/disarm",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def level_from(self, state: dict) -> float:
        """Turn the activity feed into a 0..1 pulse.

        Derived from data the control server already keeps rather than from an
        audio stream, so the orb needs no microphone of its own and cannot
        interfere with the session. A pending confirmation is the strongest
        signal: that is the one moment the user must look at the orb.
        """
        pending = state.get("pending_confirmations") or []
        if pending:
            return 1.0
        calls = state.get("recent_calls") or []
        if not calls:
            return 0.0
        newest = max((c.get("ts", 0.0) for c in calls), default=0.0)
        age = time.time() - newest
        if age < 0 or age > 4.0:
            return 0.0
        # Fast attack, slow release over four seconds.
        return max(0.0, 1.0 - age / 4.0) ** 1.6


# ------------------------------------------------------------------ the orb


class NativeOrb:
    """A frameless, click-through-until-touched, always-on-top circle."""

    #: Movement in pixels before a press becomes a drag rather than a tap.
    TAP_SLOP = 4
    POLL_MS = 250

    def __init__(self, cfg, base_url: str) -> None:
        self._make_dpi_aware()
        self.cfg = cfg
        self.control = Control(base_url)
        self.hwnd: int = 0
        self.renderer = OrbRenderer()
        self.armed = False
        self.level = 0.0
        self._dragging = False
        self._press: tuple[int, int] | None = None
        self._drawn: tuple[bool, float] | None = None
        self._cursor: int | None = None

    @staticmethod
    def _make_dpi_aware() -> None:
        """Opt into per-monitor DPI *before* any window is created.

        Without this the process is DPI-unaware, Windows virtualises the
        coordinates, and a 128px orb becomes 160px on a 125% display while the
        corner arithmetic is done against a virtualised work area -- which is
        how the orb ended up at 0,0 instead of in the corner. PER_MONITOR_AWARE_V2
        is -4; it is not exported by ctypes.wintypes on every Python build, so
        the value is inlined and failure is non-fatal.
        """
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("could not set DPI awareness: %s", exc)

    # -- placement ---------------------------------------------------------

    def _work_area(self) -> tuple[int, int, int, int]:
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        r = RECT()
        # SPI_GETWORKAREA = 0x0030
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        if r.r <= r.l or r.b <= r.t:
            return 0, 0, 1920, 1080
        return r.l, r.t, r.r, r.b

    def _place(self) -> tuple[int, int]:
        left, top, right, bottom = self._work_area()
        margin = 24
        corner = (getattr(self.cfg, "overlay_corner", "") or "bottom-right").lower()
        w = h = PLATE
        logger.info("work area=(%d,%d,%d,%d) corner=%s", left, top, right, bottom, corner)
        if corner == "top-left":
            return left + margin, top + margin
        if corner == "bottom-left":
            return left + margin, bottom - h - margin
        if corner == "top-right":
            return right - w - margin, top + margin
        if corner == "bottom-right":
            return right - w - margin, bottom - h - margin
        logger.warning("unknown OVERLAY_CORNER %r; using bottom-right", corner)
        return right - w - margin, bottom - h - margin

    def _move(self, x: int, y: int) -> tuple[int, int]:
        """Move the window, clamped so the orb cannot be lost off-screen."""
        left, top, right, bottom = self._work_area()
        w = h = PLATE
        x = min(max(x, left - PLATE // 2), right - w + PLATE // 2)
        y = min(max(y, top - PLATE // 2), bottom - h + PLATE // 2)
        # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd), None, int(x), int(y), 0, 0, 0x0040 | 0x0010 | 0x0001
        )
        # Present immediately, at the new position. Deferring it to the next poll
        # would let a stale frame show at the old spot, and for a layered window
        # the position is set by the present call itself.
        if self.renderer is not None:
            self.renderer.draw(self.armed, self.level)
            self.renderer.present(self.hwnd, (int(x), int(y)))
        return int(x), int(y)

    def _where(self) -> tuple[int, int]:
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        r = RECT()
        user32.GetWindowRect(wintypes.HWND(self.hwnd), ctypes.byref(r))
        return int(r.l), int(r.t)

    # -- painting ----------------------------------------------------------

    def _redraw(self, force: bool = False) -> None:
        key = (self.armed, round(self.level, 2))
        if not force and self._drawn == key:
            return  # nothing moved; do not spend a composite
        self._drawn = key
        self.renderer.draw(self.armed, self.level)
        # Present at the orb's real position: UpdateLayeredWindow moves the
        # window to whatever it is handed.
        self.renderer.present(self.hwnd, self._where())

    def _poll(self) -> None:
        try:
            state = self.control.safety()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.debug("orb poll failed: %s", exc)
            return
        self.armed = bool(state.get("armed"))
        self.level = self.control.level_from(state)
        self._redraw()

    # -- window plumbing ---------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            self._poll()
            return 0
        if msg == WM_ERASEBKGND:
            return 1  # layered window: never paint a background
        if msg == WM_NCHITTEST:
            return HTCLIENT
        if msg == WM_LBUTTONDOWN:
            self._press = (lparam & 0xFFFF, (lparam >> 16) & 0xFFFF)
            self._dragging = False
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE:
            if self._press is not None:
                x = lparam & 0xFFFF
                y = (lparam >> 16) & 0xFFFF
                px, py = self._press
                if not self._dragging and abs(x - px) + abs(y - py) > self.TAP_SLOP:
                    self._dragging = True
                if self._dragging:
                    # Track from the window's real position rather than a
                    # remembered one, so the orb cannot outrun the cursor once
                    # it has been clamped against a screen edge.
                    ox, oy = self._where()
                    self._move(ox + (x - px), oy + (y - py))
                    self._press = (x, y)
            return 0
        if msg == WM_LBUTTONUP:
            user32.ReleaseCapture()
            tapped = not self._dragging
            self._dragging = False
            self._press = None
            if tapped:
                # The panic button. Deliberately the only thing a tap does:
                # arming is never reachable from a floating widget, because an
                # accidental tap must never grant control.
                try:
                    self.control.disarm()
                except (urllib.error.URLError, OSError) as exc:
                    logger.debug("orb disarm failed: %s", exc)
            return 0
        if msg == WM_RBUTTONUP:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> int:
        hinst = kernel32.GetModuleHandleW(None)
        hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))
        hcursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))

        def proc(hwnd, msg, wparam, lparam):
            # A window procedure must not raise into the message loop: ctypes
            # would print a traceback to stderr on every subsequent message and
            # the window would keep running in a half-broken state.
            try:
                return self._wndproc(hwnd, msg, wparam, lparam)
            except Exception as exc:
                logger.debug("orb wndproc(%s) failed: %s", msg, exc)
                return 0

        wndproc = WNDPROC(proc)  # keep a reference: the callback must outlive us

        cls = WNDCLASSW()
        cls.style = CS_HREDRAW | CS_VREDRAW
        cls.lpfnWndProc = wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "VoxOrb"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != 1410:  # already registered
                logger.error("RegisterClassW failed: %s", err)
                return 1

        x, y = self._place()
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "VoxOrb", "VOX", WS_POPUP,
            0, 0, PLATE, PLATE, None, None, hinst, None,
        )
        if not hwnd:
            logger.error("CreateWindowExW failed: %s", ctypes.get_last_error())
            return 1
        self.hwnd = int(hwnd)
        # Created at the origin, then moved explicitly. Passing x/y to
        # CreateWindowExW did not stick on this machine -- the window kept
        # landing at 0,0 -- so the placement is done with SetWindowPos where it
        # can be verified.
        placed = self._move(x, y)
        self._cursor = placed
        logger.info(
            "orb wanted %s, asked for %s, actually at %s", (x, y), placed, self._where()
        )

        user32.ShowWindow(hwnd, SW_SHOW)
        self._redraw(force=True)
        user32.SetTimer(hwnd, 1, self.POLL_MS, None)
        self._poll()

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self.renderer.close()
        return 0


def run(cfg, base_url: str) -> int:
    """Open the native orb and block until it closes (right-click quits)."""
    try:
        orb = NativeOrb(cfg, base_url)
    except OSError as exc:
        logger.error("could not create the orb surface: %s", exc)
        return 1
    logger.info("orb up: click to disarm, drag to move, right-click to quit")
    return orb.run()
