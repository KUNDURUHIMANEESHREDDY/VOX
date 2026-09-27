"""Read-only screen inspection tools.

Everything in this module is ``safe`` risk: it only looks. These stay available
even while the agent is disarmed, so the user can still ask "what's on my
screen?" before deciding to arm it.
"""

from __future__ import annotations

import base64
import io
import time
from typing import Any

from livekit.agents import RunContext, function_tool

from ..safety import get_safety
from ._common import gate, lazy, ok, refusal, trim


def _capture(region: tuple[int, int, int, int] | None):
    """Grab the screen (or a region) as a PIL image, downscaled for the model."""
    pyautogui = lazy("pyautogui")
    box = region
    if box is None:
        width, height = pyautogui.size()
        box = (0, 0, width, height)
    image = pyautogui.screenshot(region=box)
    return image


def _to_data_url(image, max_width: int = 1024) -> str:
    """Downscale and JPEG-encode so a 4K screenshot does not blow up the context."""
    pil = lazy("PIL.Image")
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, height))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=70, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


@function_tool
async def look_at_screen(
    ctx: RunContext[Any],
    question: str = "Describe what is on screen right now.",
) -> str:
    """Look at the user's screen and actually see it.

    Use this when the user asks what is on screen, what a window says, or where
    something is. Returns a real image to the model plus the pixel coordinates
    of the screen, so follow-up clicks can be aimed accurately.
    """
    safety = get_safety()
    decision = gate("look_at_screen", "safe", {"question": question})
    if not decision.allowed:
        return refusal(decision)

    try:
        image = _capture()
    except Exception as exc:
        return f"ERROR. Could not capture the screen: {exc}"

    width, height = image.width, image.height
    data_url = _to_data_url(image)

    # Hand the pixels to the model rather than just describing the file path.
    from livekit.agents.llm import ImageContent

    ctx.context.add_message(
        role="user",
        content=[
            {"type": "text", "text": f"Screenshot of the user's screen: {question}"},
            ImageContent(image=data_url),
        ],
    )

    # Also persist it so the operator can review exactly what the agent saw.
    saved = ""
    try:
        safety.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = safety.screenshot_dir / f"look-{int(time.time() * 1000)}.jpg"
        image.convert("RGB").save(path, format="JPEG", quality=80)
        saved = f" Saved a copy to {path}."
    except OSError:
        pass

    return trim(
        f"OK. Attached a live screenshot ({width}x{height}px) to the conversation. "
        f"Answer the question from the image. Screen coordinates run from (0,0) at the "
        f"top-left to ({width},{height}) at the bottom-right.{saved}"
    )


@function_tool
async def get_screen_layout() -> str:
    """Get the screen resolution, cursor position and current mouse button state."""
    decision = gate("get_screen_layout", "safe", {})
    if not decision.allowed:
        return refusal(decision)
    try:
        pyautogui = lazy("pyautogui")
        width, height = pyautogui.size()
        x, y = pyautogui.position()
    except Exception as exc:
        return f"ERROR. Could not read screen layout: {exc}"
    return trim(
        f"OK. Screen is {width}x{height}. Cursor is at ({x}, {y}). "
        f"Primary display origin is (0,0); if the user has multiple monitors, "
        f"negative coordinates are possible on screens left of / above the primary."
    )


@function_tool
async def save_screenshot(label: str = "screenshot") -> str:
    """Save a full screenshot to disk and tell the user the path."""
    decision = gate("save_screenshot", "safe", {"label": label})
    if not decision.allowed:
        return refusal(decision)
    safety = get_safety()
    try:
        image = _capture()
        safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40] or "shot"
        safety.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = safety.screenshot_dir / f"{safe_label}-{int(time.time() * 1000)}.png"
        image.save(path)
    except Exception as exc:
        return f"ERROR. Could not save screenshot: {exc}"
    return ok(f"Screenshot saved to {path}.")
