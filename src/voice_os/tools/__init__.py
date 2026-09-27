"""Toolset assembly.

:func:`build_toolset` returns the list of tools handed to ``AgentSession``. The
read-only tools come first and are always available; the state-changing and
high-risk tools follow. Ordering matters only for readability of the schema the
model sees, not for execution.
"""

from __future__ import annotations

from livekit.agents.llm import Tool

from .apps import (
    get_clipboard,
    launch_app,
    list_running_apps,
    open_url,
    run_powershell_command,
    search_the_web,
    set_clipboard,
    set_system_volume,
)
from .brain import build_brain_toolset
from .guard_tools import arm_agent, confirm_action, disarm_agent, safety_status
from .input import (
    click_screen,
    drag_mouse,
    emergency_stop,
    move_mouse,
    press_hotkey,
    press_key,
    scroll_screen,
    type_text,
)
from .screen import get_screen_layout, look_at_screen, save_screenshot
from .windows import (
    close_window,
    describe_active_window,
    focus_window,
    list_windows,
    move_resize_window,
    read_window_text,
    set_window_state,
)

#: Read-only tools, usable while the agent is disarmed.
READ_ONLY: list[Tool] = [
    look_at_screen,
    get_screen_layout,
    save_screenshot,
    list_windows,
    describe_active_window,
    read_window_text,
    list_running_apps,
    get_clipboard,
    safety_status,
]

#: State-changing tools. Require an armed agent.
STATE_CHANGING: list[Tool] = [
    focus_window,
    set_window_state,
    move_resize_window,
    open_url,
    search_the_web,
    launch_app,
    set_clipboard,
    set_system_volume,
    move_mouse,
    scroll_screen,
    type_text,
    run_powershell_command,
]

#: High-risk tools. Require an armed agent plus per-action confirmation.
HIGH_RISK: list[Tool] = [
    click_screen,
    drag_mouse,
    press_key,
    press_hotkey,
    close_window,
]

#: Permission-management tools. Always available so the model can obey an
#: instruction to stop or to arm.
PERMISSION: list[Tool] = [
    arm_agent,
    disarm_agent,
    confirm_action,
    emergency_stop,
]


def build_toolset(*, include_shell: bool = True, include_brain: bool = True) -> list[Tool]:
    """Return every tool, optionally dropping the escape hatches.

    ``include_shell=False`` hides ``run_powershell_command`` from the model
    entirely, which is stronger than refusing it at runtime: an unavailable tool
    cannot be hallucinated into a retry loop. ``include_brain=False`` does the
    same for the whole brain toolset, leaving a pure desktop controller.

    The brain's own tools are tiered separately in :mod:`.brain` and reach the
    same :class:`~voice_os.safety.SafetyController`, so the desktop tools above
    remain the only place machine control is authorised.
    """
    tools = [*READ_ONLY, *STATE_CHANGING, *HIGH_RISK, *PERMISSION]
    if include_brain:
        tools = [*tools, *build_brain_toolset()]
    if not include_shell:
        tools = [tool for tool in tools if getattr(tool, "__name__", "") != "run_powershell_command"]
    return tools


__all__ = [
    "READ_ONLY",
    "STATE_CHANGING",
    "HIGH_RISK",
    "PERMISSION",
    "build_toolset",
]
