"""Launching applications, opening URLs and running shell commands.

Opening things is ``low`` risk. Running a shell command is ``high`` risk, off by
default (``ALLOW_SHELL``), and additionally filtered through a denylist that
blocks the commands people actually regret running.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import function_tool

from ..safety import get_safety
from ._common import gate, lazy, ok, refusal, trim

#: Friendly names -> launch targets, so the model does not have to invent paths.
APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "powershell": "powershell.exe",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
    "browser": "",  # resolved dynamically below
    "edge": "msedge.exe",
    "chrome": "chrome.exe",
    "firefox": "firefox.exe",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "spotify": "spotify.exe",
    "discord": "discord.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe",
    "snipping tool": "snippingtool.exe",
}

_URL_PREFIXES = ("http://", "https://", "file:///", "ftp://")


@function_tool
async def open_url(url: str) -> str:
    """Open a URL in the default browser.

    Accepts a full URL. If the user says "search for X" use search_the_web
    instead. Low risk.
    """
    url = (url or "").strip()
    decision = gate("open_url", "low", {"url": url})
    if not decision.allowed:
        return refusal(decision)
    if not url.startswith(_URL_PREFIXES):
        if "." not in url or " " in url:
            return (
                f"ERROR. {url!r} is not a full URL. Add https:// or use search_the_web "
                "to run a search."
            )
        url = f"https://{url}"
    try:
        webbrowser = lazy("webbrowser")
        webbrowser.open(url)
    except Exception as exc:
        return f"ERROR. Could not open {url}: {exc}"
    return ok(f"Opened {url} in the default browser.")


@function_tool
async def search_the_web(query: str) -> str:
    """Search the web for a query using the default browser."""
    query = (query or "").strip()
    if not query:
        return "ERROR. query must not be empty."
    decision = gate("search_the_web", "low", {"query": query})
    if not decision.allowed:
        return refusal(decision)
    from urllib.parse import quote_plus

    url = f"https://duckduckgo.com/?q={quote_plus(query)}"
    try:
        webbrowser = lazy("webbrowser")
        webbrowser.open(url)
    except Exception as exc:
        return f"ERROR. Could not open a search: {exc}"
    return ok(f'Searched the web for "{query}".')


@function_tool
async def launch_app(name: str) -> str:
    """Launch a desktop application by common name.

    Knows Notepad, Calculator, Paint, File Explorer, Terminal, PowerShell, Task
    Manager, Settings, browsers, VS Code, Spotify, Discord and Office apps.
    Low risk.
    """
    name = (name or "").strip()
    decision = gate("launch_app", "low", {"name": name})
    if not decision.allowed:
        return refusal(decision)
    key = name.lower().strip()
    target = APP_ALIASES.get(key, "")
    if not target and key == "browser":
        target = _default_browser()
    if not target:
        return (
            f'ERROR. "{name}" is not in the known app list. Known apps: '
            f'{", ".join(sorted(set(APP_ALIASES)))}. You can also use run_powershell_command '
            "if shell execution is enabled."
        )
    try:
        subprocess = lazy("subprocess")
        subprocess.Popen(
            target, shell=target.endswith(":") or ":" in target
        )
    except Exception as exc:
        return f"ERROR. Could not launch {name}: {exc}"
    return ok(f"Launched {name} ({target}).")


def _default_browser() -> str:
    """Best-effort default browser launch target."""
    try:
        webbrowser = lazy("webbrowser")
        return webbrowser.get().name or "https://www.google.com"
    except Exception:
        return "https://www.google.com"


@function_tool
async def list_running_apps() -> str:
    """List the processes currently running, with memory usage.

    Read-only, so it works while disarmed. Useful to find the right process or
    check whether something the user mentioned is actually running.
    """
    decision = gate("list_running_apps", "safe", {})
    if not decision.allowed:
        return refusal(decision)
    try:
        psutil = lazy("psutil")
    except Exception as exc:
        return f"ERROR. Process listing is unavailable: {exc}"
    try:
        rows = []
        for proc in psutil.process_iter(["name", "memory_info", "pid"]):
            try:
                info = proc.info
                if not info.get("name"):
                    continue
                mem = info.get("memory_info")
                rows.append(
                    (info["name"], info.get("pid", 0), (mem.rss // 1048576) if mem else 0)
                )
            except Exception:
                continue
    except Exception as exc:
        return f"ERROR. Could not list processes: {exc}"
    if not rows:
        return "OK. No readable processes found."
    rows.sort(key=lambda r: r[2], reverse=True)
    lines = [f"OK. {len(rows)} processes running. Largest 25 by memory:"]
    for name, pid, mem in rows[:25]:
        lines.append(f"  - {name} (pid {pid}, {mem} MB)")
    return trim("\n".join(lines))


@function_tool
async def run_powershell_command(
    command: str, confirmation_id: str | None = None
) -> str:
    """Run a single read-oriented PowerShell command and return its output.

    High risk, and disabled unless the operator set ALLOW_SHELL=1. Even then a
    denylist blocks destructive commands. Prefer the dedicated tools whenever
    one fits: this escape hatch is for things like listing git status or
    checking the weather, not for changing the system.

    Always confirm with the user first and read the command back to them.
    """
    command = (command or "").strip()
    if not command:
        return "ERROR. command must not be empty."
    if "\n" in command or ";" in command:
        return (
            "ERROR. Run exactly one command at a time. Chaining with ';' or newlines "
            "is not allowed."
        )

    safety = get_safety()
    allowed, why = safety.shell_allowed(command)
    if not allowed:
        return f"REFUSED. {why}"

    decision = gate(
        "run_powershell_command", "high", {"command": command}, confirmation_id=confirmation_id
    )
    if not decision.allowed:
        return refusal(decision)

    try:
        subprocess = lazy("subprocess")
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return f"ERROR. Command failed to run: {exc}"
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    parts = [f"OK. Exit code {completed.returncode}."]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    return trim("\n".join(parts))


@function_tool
async def set_clipboard(text: str) -> str:
    """Put text on the system clipboard."""
    decision = gate("set_clipboard", "low", {"text": str(text)[:4000]})
    if not decision.allowed:
        return refusal(decision)
    try:
        pyperclip = lazy("pyperclip")
        pyperclip.copy(str(text))
    except Exception as exc:
        return f"ERROR. Could not set the clipboard: {exc}"
    return ok(f"Copied {len(str(text))} characters to the clipboard.")


@function_tool
async def get_clipboard() -> str:
    """Read the current text contents of the system clipboard."""
    decision = gate("get_clipboard", "safe", {})
    if not decision.allowed:
        return refusal(decision)
    try:
        pyperclip = lazy("pyperclip")
        value = pyperclip.paste()
    except Exception as exc:
        return f"ERROR. Could not read the clipboard: {exc}"
    if not value:
        return "OK. The clipboard is empty."
    return trim(f"OK. Clipboard contents ({len(value)} chars): {value}")


@function_tool
async def set_system_volume(action: str, steps: int = 5) -> str:
    """Control the system volume using the media keys.

    action must be "up", "down", "mute" or "unmute". Uses real media keys, so it
    affects whichever app has focus. Low risk.
    """
    action = (action or "").strip().lower()
    if action not in {"up", "down", "mute", "unmute"}:
        return 'ERROR. action must be "up", "down", "mute" or "unmute".'
    decision = gate("set_system_volume", "low", {"action": action, "steps": int(steps)})
    if not decision.allowed:
        return refusal(decision)
    key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute", "unmute": "volumemute"}[action]
    times = 1 if action in {"mute", "unmute"} else max(1, min(int(steps), 25))
    try:
        pyautogui = lazy("pyautogui")
        for _ in range(times):
            pyautogui.press(key)
    except Exception as exc:
        return f"ERROR. Could not change volume: {exc}"
    if action == "mute":
        return ok("Toggled mute. Note this toggles, so call it again to unmute.")
    if action == "unmute":
        return ok("Toggled mute, which should now unmute. Note this is a toggle.")
    return ok(f"Sent {times} volume-{action} media keypress(es).")
