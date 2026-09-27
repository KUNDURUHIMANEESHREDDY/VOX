"""Mouse and keyboard control.

This is the most dangerous surface in the agent: speech recognition is fallible
and a misheard coordinate or a stray keystroke lands on the real desktop. Every
tool here is ``high`` risk and therefore bound to an explicit confirmation --
with one deliberate exception. ``type_text`` becomes ``low`` risk when the user
names a destination window, because then the user has already specified *where*
the text should go. Typing blind into whatever happens to be focused stays
``high``, since there is no way to verify the landing site.
"""

from __future__ import annotations

import time
from typing import Any

from livekit.agents import function_tool

from ..safety import get_safety
from ._common import gate, lazy, ok, refusal, trim

#: Coarse keyboard button names mapped to pyautogui spellings.
BUTTONS = {"left", "right", "middle"}

KEY_ALIASES = {
    "esc": "escape",
    "escape": "escape",
    "enter": "enter",
    "return": "enter",
    "space": "space",
    "spacebar": "space",
    "tab": "tab",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "win",
    "windows": "win",
    "cmd": "win",
    "super": "win",
    "menu": "apps",
    "printscreen": "printscreen",
    "capslock": "capslock",
}

def _resolve_keys(keys: list[str]) -> list[str]:
    resolved = []
    for key in keys:
        token = key.strip().lower()
        if not token:
            continue
        resolved.append(KEY_ALIASES.get(token, token))
    return resolved


@function_tool
async def move_mouse(x: int, y: int, duration: float = 0.3) -> str:
    """Move the mouse cursor to pixel coordinates without clicking.

    Low risk: it changes nothing. Use it to highlight or point at something.
    Get coordinates from look_at_screen first.
    """
    decision = gate("move_mouse", "low", {"x": int(x), "y": int(y)})
    if not decision.allowed:
        return refusal(decision)
    try:
        pyautogui = lazy("pyautogui")
        pyautogui.moveTo(int(x), int(y), duration=max(0.0, min(float(duration), 3.0)))
    except Exception as exc:
        return f"ERROR. Could not move the mouse: {exc}"
    return ok(f"Cursor moved to ({int(x)}, {int(y)}). Nothing was clicked.")


@function_tool
async def click_screen(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    confirmation_id: str | None = None,
) -> str:
    """Click the mouse. High risk: always confirm with the user first.

    Call look_at_screen to see the screen and get accurate coordinates before
    clicking. Omit x and y to click wherever the cursor already is.
    Set clicks=2 for a double click.
    """
    if button not in BUTTONS:
        return f"ERROR. button must be one of {sorted(BUTTONS)}, got {button!r}."
    clicks = max(1, min(int(clicks), 3))
    target: dict[str, Any] = {"x": int(x) if x is not None else None, "y": int(y) if y is not None else None}

    decision = gate("click_screen", "high", target, confirmation_id=confirmation_id)
    if not decision.allowed:
        return refusal(decision)

    try:
        pyautogui = lazy("pyautogui")
        if target["x"] is not None and target["y"] is not None:
            pyautogui.click(
                target["x"], target["y"], clicks=clicks, button=button, interval=0.12
            )
            where = f"({target['x']}, {target['y']})"
        else:
            pyautogui.click(clicks=clicks, button=button, interval=0.12)
            pos = pyautogui.position()
            where = f"the current cursor position ({pos[0]}, {pos[1]})"
    except Exception as exc:
        return f"ERROR. Click failed: {exc}"
    suffix = "double-clicked" if clicks == 2 else "clicked"
    if clicks == 3:
        suffix = "triple-clicked"
    return ok(f"{button.capitalize()}-button {suffix} at {where}.")


@function_tool
async def drag_mouse(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration: float = 0.6,
    button: str = "left",
    confirmation_id: str | None = None,
) -> str:
    """Press, drag and release the mouse — used to move, resize or drop things.

    High risk: this can move files, resize windows or drag items onto buttons
    that delete them. Always confirm with the user first.
    """
    if button not in BUTTONS:
        return f"ERROR. button must be one of {sorted(BUTTONS)}, got {button!r}."
    decision = gate(
        "drag_mouse",
        "high",
        {"from": [int(from_x), int(from_y)], "to": [int(to_x), int(to_y)], "button": button},
        confirmation_id=confirmation_id,
    )
    if not decision.allowed:
        return refusal(decision)
    try:
        pyautogui = lazy("pyautogui")
        pyautogui.moveTo(int(from_x), int(from_y))
        time.sleep(0.1)
        pyautogui.dragTo(
            int(to_x), int(to_y), duration=max(0.1, min(float(duration), 5.0)), button=button
        )
    except Exception as exc:
        return f"ERROR. Drag failed: {exc}"
    return ok(f"Dragged the {button} button from ({int(from_x)}, {int(from_y)}) to ({int(to_x)}, {int(to_y)}).")


@function_tool
async def scroll_screen(amount: int, x: int | None = None, y: int | None = None) -> str:
    """Scroll the mouse wheel. Positive scrolls up, negative scrolls down."""
    decision = gate("scroll_screen", "low", {"amount": int(amount), "x": x, "y": y})
    if not decision.allowed:
        return refusal(decision)
    try:
        pyautogui = lazy("pyautogui")
        amount = max(-40, min(int(amount), 40))
        if x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y))
        pyautogui.scroll(amount)
    except Exception as exc:
        return f"ERROR. Scroll failed: {exc}"
    direction = "up" if amount > 0 else "down"
    return ok(f"Scrolled {abs(amount)} clicks {direction}.")


@function_tool
async def type_text(
    text: str,
    into_window: str = "",
    confirmation_id: str | None = None,
) -> str:
    """Type text on the keyboard.

    If the user names a target window (for example into_window="Notepad"), the
    window is focused first and this is treated as a low-risk action.

    If no window is named, the text goes into whatever currently has focus,
    which is high risk because the landing site cannot be verified. In that case
    ask the user to confirm, and tell them exactly which window has focus.
    """
    if not text:
        return "ERROR. text must not be empty."
    text = str(text)[:4000]
    into_window = (into_window or "").strip()

    if into_window:
        from .windows import focus_window  # local import avoids a cycle

        focused = await focus_window(into_window)
        if focused.startswith(("ERROR", "NOT FOUND")):
            return f"ERROR. Could not focus window {into_window!r}: {focused}"
        decision = gate(
            "type_text", "low", {"text": text, "into_window": into_window},
            confirmation_id=confirmation_id,
        )
        where = f"window {into_window!r}"
    else:
        from .windows import describe_active_window

        active = await describe_active_window()
        decision = gate(
            "type_text", "high", {"text": text, "into_window": None},
            confirmation_id=confirmation_id,
        )
        where = f"whatever is focused right now ({trim(active, 200)})"

    if not decision.allowed:
        return refusal(decision)

    try:
        pyautogui = lazy("pyautogui")
        pyautogui.typewrite(text, interval=0.01)
    except Exception as exc:
        return f"ERROR. Typing failed: {exc}"
    return ok(f"Typed {len(text)} characters into {where}.")


@function_tool
async def press_key(key: str, confirmation_id: str | None = None) -> str:
    """Press a single key such as enter, escape, tab, f5 or up.

    High risk: keys like enter or f4 can commit or destroy things. Always confirm
    with the user first and say which key you are about to press.
    """
    resolved = _resolve_keys([key])
    if not resolved:
        return f"ERROR. Unknown key {key!r}."
    name = resolved[0]
    decision = gate("press_key", "high", {"key": name}, confirmation_id=confirmation_id)
    if not decision.allowed:
        return refusal(decision)
    try:
        pyautogui = lazy("pyautogui")
        pyautogui.press(name)
    except Exception as exc:
        return f"ERROR. Could not press {name!r}: {exc}"
    return ok(f"Pressed the {name} key.")


@function_tool
async def press_hotkey(
    keys: str, confirmation_id: str | None = None
) -> str:
    """Press a keyboard shortcut, for example "ctrl+c", "alt+tab" or "win+d".

    High risk: shortcuts can close unsaved work, delete, or capture the machine.
    Always confirm with the user first, and never guess a shortcut — if unsure
    what a shortcut does, say so instead of pressing it.
    """
    raw = [part for part in str(keys).replace("-", "+").split("+") if part.strip()]
    resolved = _resolve_keys(raw)
    if not resolved:
        return f"ERROR. Could not parse shortcut {keys!r}."
    decision = gate("press_hotkey", "high", {"keys": resolved}, confirmation_id=confirmation_id)
    if not decision.allowed:
        return refusal(decision)
    try:
        pyautogui = lazy("pyautogui")
        pyautogui.hotkey(*resolved)
    except Exception as exc:
        return f"ERROR. Could not press shortcut {'+'.join(resolved)}: {exc}"
    return ok(f"Pressed {'+'.join(resolved)}.")


@function_tool
async def emergency_stop() -> str:
    """Stop all agent activity immediately and disarm it.

    Tell the user this is always available, and use it if they sound worried or
    ask you to stop.
    """
    result = get_safety().disarm(source="emergency_stop", reason="requested by user")
    return ok("All desktop control is stopped and the agent is disarmed. " + result["message"])
