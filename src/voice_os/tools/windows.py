"""Window management via pywinauto / Win32.

Listing and inspecting windows is ``safe`` and always available. Focus, minimise
and maximise are ``low``. Closing a window is ``high`` because it can destroy
unsaved work.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import function_tool

from ._common import gate, lazy, ok, refusal, trim

#: Top-level window classes that are shell noise rather than real apps.
_NOISE = {"Progman", "Shell_TrayWnd", "WorkerW", "ApplicationFrameWindow"}


def _visible_windows() -> list[dict[str, Any]]:
    win32gui = lazy("win32gui")
    found: list[dict[str, Any]] = []

    def callback(hwnd: int, _extra: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or title.strip() in _NOISE:
            return
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        found.append(
            {
                "title": title,
                "handle": hwnd,
                "class": win32gui.GetClassName(hwnd),
                "left": rect[0],
                "top": rect[1],
                "width": rect[2] - rect[0],
                "height": rect[3] - rect[1],
            }
        )

    win32gui.EnumWindows(callback, None)
    return found


def _match(title_query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve a title query to a window, preferring exact then prefix matches."""
    windows = _visible_windows()
    needle = (title_query or "").strip().lower()
    if not needle:
        return None, windows
    for window in windows:
        if window["title"].strip().lower() == needle:
            return window, windows
    for window in windows:
        if window["title"].strip().lower().startswith(needle):
            return window, windows
    matches = [w for w in windows if needle in w["title"].lower()]
    if len(matches) == 1:
        return matches[0], windows
    return None, windows


@function_tool
async def list_windows() -> str:
    """List the open application windows on the desktop.

    Read-only, so it works even when the agent is disarmed. Use it to find the
    exact window title before focusing, typing into or closing something.
    """
    decision = gate("list_windows", "safe", {})
    if not decision.allowed:
        return refusal(decision)
    try:
        windows = _visible_windows()
    except Exception as exc:
        return f"ERROR. Could not enumerate windows: {exc}"
    if not windows:
        return "OK. No application windows are currently open."
    lines = [f"OK. {len(windows)} open window(s):"]
    for window in windows[:25]:
        lines.append(
            f'  - "{window["title"]}" ({window["width"]}x{window["height"]}, '
            f'handle {window["handle"]}, class {window["class"]})'
        )
    if len(windows) > 25:
        lines.append(f"  ...and {len(windows) - 25} more.")
    return trim("\n".join(lines))


@function_tool
async def describe_active_window() -> str:
    """Describe the window that currently has keyboard focus."""
    decision = gate("describe_active_window", "safe", {})
    if not decision.allowed:
        return refusal(decision)
    try:
        win32gui = lazy("win32gui")
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return "OK. The focused window has no title (it may be the desktop or a menu)."
        rect = win32gui.GetWindowRect(hwnd)
        return (
            f'OK. Focused window: "{title}" (class {win32gui.GetClassName(hwnd)}, '
            f"position {rect[0]},{rect[1]} size {rect[2] - rect[0]}x{rect[3] - rect[1]})."
        )
    except Exception as exc:
        return f"ERROR. Could not read the focused window: {exc}"


@function_tool
async def focus_window(title: str) -> str:
    """Bring a window to the front by title, so typing and keys land in it.

    title is matched case-insensitively against open window titles. Low risk.
    """
    decision = gate("focus_window", "low", {"title": title})
    if not decision.allowed:
        return refusal(decision)
    try:
        win32gui = lazy("win32gui")
        window, windows = _match(title)
        if window is None:
            matches = [w["title"] for w in windows if title.lower() in w["title"].lower()]
            hint = f" Close matches: {matches[:5]}." if matches else ""
            return f'NOT FOUND. No open window matches "{title}".{hint}'
        if win32gui.IsIconic(window["handle"]):
            win32gui.ShowWindow(window["handle"], win32gui.SW_RESTORE)
        win32gui.SetForegroundWindow(window["handle"])
    except Exception as exc:
        return f"ERROR. Could not focus window {title!r}: {exc}"
    return ok(f'Focused window "{window["title"]}". It is now the active window.')


@function_tool
async def set_window_state(title: str, state: str) -> str:
    """Minimise, maximise or restore a window by title.

    state must be "minimize", "maximize" or "restore". Low risk.
    """
    if state not in {"minimize", "maximize", "restore"}:
        return 'ERROR. state must be "minimize", "maximize" or "restore".'
    decision = gate("set_window_state", "low", {"title": title, "state": state})
    if not decision.allowed:
        return refusal(decision)
    try:
        win32gui = lazy("win32gui")
        window, _ = _match(title)
        if window is None:
            return f'NOT FOUND. No open window matches "{title}".'
        constants = {
            "minimize": win32gui.SW_MINIMIZE,
            "maximize": win32gui.SW_MAXIMIZE,
            "restore": win32gui.SW_RESTORE,
        }
        win32gui.ShowWindow(window["handle"], constants[state])
    except Exception as exc:
        return f"ERROR. Could not {state} window {title!r}: {exc}"
    return ok(f'{state.capitalize()}d window "{window["title"]}".')


@function_tool
async def close_window(title: str, confirmation_id: str | None = None) -> str:
    """Close an application window by title.

    High risk: the app may have unsaved work and will usually prompt. Always
    confirm with the user first, and mention what will be closed.
    """
    decision = gate("close_window", "high", {"title": title}, confirmation_id=confirmation_id)
    if not decision.allowed:
        return refusal(decision)
    try:
        win32gui = lazy("win32gui")
        window, _ = _match(title)
        if window is None:
            return f'NOT FOUND. No open window matches "{title}".'
        win32gui.PostMessage(window["handle"], 0x0010, 0, 0)  # WM_CLOSE
    except Exception as exc:
        return f"ERROR. Could not close window {title!r}: {exc}"
    return ok(
        f'Asked "{window["title"]}" to close. If it has unsaved work it will show a '
        "save prompt, so check with the user before confirming that."
    )


@function_tool
async def move_resize_window(
    title: str, x: int, y: int, width: int, height: int
) -> str:
    """Move and/or resize a window to exact screen coordinates."""
    decision = gate(
        "move_resize_window",
        "low",
        {"title": title, "x": int(x), "y": int(y), "w": int(width), "h": int(height)},
    )
    if not decision.allowed:
        return refusal(decision)
    try:
        win32gui = lazy("win32gui")
        window, _ = _match(title)
        if window is None:
            return f'NOT FOUND. No open window matches "{title}".'
        win32gui.MoveWindow(
            window["handle"], int(x), int(y), int(width), int(height), True
        )
    except Exception as exc:
        return f"ERROR. Could not resize window {title!r}: {exc}"
    return ok(
        f'Moved "{window["title"]}" to ({int(x)}, {int(y)}) at {int(width)}x{int(height)}.'
    )


@function_tool
async def read_window_text(title: str) -> str:
    """Read the visible text content of a window (buttons, labels, fields).

    Read-only, so it works while disarmed. Useful for verifying what a window
    actually says instead of guessing from a screenshot.
    """
    decision = gate("read_window_text", "safe", {"title": title})
    if not decision.allowed:
        return refusal(decision)
    try:
        pywinauto = lazy("pywinauto")
        win32gui = lazy("win32gui")
    except Exception as exc:
        return f"ERROR. Window inspection is unavailable: {exc}"

    try:
        window, _ = _match(title)
        if window is None:
            return f'NOT FOUND. No open window matches "{title}".'
        app = pywinauto.Application(backend="win32").connect(handle=window["handle"])
        wrapper = app.window(handle=window["handle"])
        try:
            text = wrapper.window_text()
        except Exception:
            text = win32gui.GetWindowText(window["handle"])
        return trim(
            f'OK. Window "{window["title"]}" text: {text.strip() or "(empty)"}\n'
            "If you need button labels or control names, use look_at_screen instead, "
            "since this reads only the frame text."
        )
    except Exception as exc:
        return f"ERROR. Could not read window text: {exc}"
