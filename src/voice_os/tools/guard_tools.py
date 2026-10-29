"""Tools the model uses to manage its own permissions.

These exist so arming and consent stay inside the same audited path whether the
user clicks the web UI or just says "arm".
"""

from __future__ import annotations

from livekit.agents import function_tool

from ..safety import get_safety
from ._common import ok


@function_tool
async def safety_status() -> str:
    """Report whether desktop control is armed, and what is still pending.

    Call this when the user asks what the agent is allowed to do, or at the start
    of a session if you are unsure.
    """
    status = get_safety().status()
    if status["armed"]:
        state = f"ARMED for {status['armed_for_seconds']:.0f}s"
        if status["auto_disarm_minutes"]:
            state += f", auto-disarms after {status['auto_disarm_minutes']:.0f}m idle"
    else:
        state = "DISARMED — cannot change anything on the machine"
    pending = status["pending_confirmations"]
    lines = [f"OK. Desktop control is {state}."]
    if pending:
        lines.append(
            "Awaiting user confirmation for: "
            + "; ".join(f"{p['tool']} -> {p['detail']}" for p in pending)
        )
    lines.append(
        f"Read-only tools (look_at_screen, list_windows, get_clipboard, recall_memory, "
        f"find_skills) always work. "
        f"Shell commands are {'enabled' if status['allow_shell'] else 'disabled'}."
    )
    return "\n".join(lines)


@function_tool
async def arm_agent() -> str:
    """Arm the agent so it can change things on the computer.

    Only call this when the user explicitly asks you to arm, or says something
    that clearly means it: "arm", "go ahead", "you can take control". Never arm
    yourself, and never arm as a side effect of another request. Once armed,
    confirm high-risk actions with the user before performing them.
    """
    result = get_safety().arm(source="voice")
    return ok(
        result["message"]
        + " You can now focus windows, launch apps, type into windows the user names, "
        "and open URLs. Clicks, drags, keystrokes, closing windows and shell commands "
        "still need explicit confirmation each time."
    )


@function_tool
async def disarm_agent() -> str:
    """Disarm the agent immediately.

    Call this whenever the user says stop, halt, disarm, or asks you to wait.
    Also call it if the user sounds confused or worried.
    """
    result = get_safety().disarm(source="voice", reason="requested by user")
    return ok(result["message"] + " I can still answer questions and look at the screen.")
