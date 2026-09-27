"""Shared helpers for tool implementations.

Tools are the only place where the agent can touch the machine, so they all
follow the same shape: check the safety gate first, return a string the model
can act on either way, and never raise past the tool boundary.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

from ..safety import Decision, Risk, get_safety

#: Refusals come back in the same string channel as successes so the model
#: always has something coherent to say out loud.
MAX_RESULT_CHARS = 1500


def refusal(decision: Decision) -> str:
    """Render a denied :class:`Decision` as instructions for the model."""
    prefix = "REFUSED."
    if decision.needs_confirmation:
        return f"{prefix} {decision.reason}"
    return f"{prefix} {decision.reason}"


def gate(
    tool: str,
    risk: Risk,
    args: dict[str, Any],
    *,
    confirmation_id: str | None = None,
    force_risk: Risk | None = None,
) -> Decision:
    return get_safety().gate(
        tool,
        risk,
        args,
        confirmation_id=confirmation_id,
        force_risk=force_risk,
    )


def ok(message: str) -> str:
    return f"OK. {message}"


def trim(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"...[truncated, {len(text)} chars total]"


def lazy(module: str) -> Any:
    """Import an optional desktop dependency on first use.

    pyautogui and pywinauto cost a noticeable fraction of a second to import and
    only matter for a subset of tools, so startup stays fast and a missing
    dependency degrades into a clear message instead of a crash.
    """

    @functools.lru_cache(maxsize=None)
    def _load() -> Any:
        import importlib

        try:
            return importlib.import_module(module)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                f"Optional dependency '{module}' is unavailable: {exc}. "
                f"Install it with: uv pip install {module.replace('_', '-')}"
            ) from exc

    return _load()


def rate_limit(seconds: float) -> Callable:
    """Reject calls that arrive faster than ``seconds`` apart.

    Speech recognition occasionally emits a partial transcript twice, which
    would otherwise fire a click twice. Cheap insurance for anything that moves
    or clicks.
    """

    def decorator(fn: Callable) -> Callable:
        state: dict[str, float] = {"last": 0.0}

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            now = time.time()
            if now - state["last"] < seconds:
                await _noop()
                return "SKIPPED. Duplicate request within the debounce window; ignored."
            state["last"] = now
            return await fn(*args, **kwargs)

        return wrapper

    return decorator


async def _noop() -> None:
    return None
