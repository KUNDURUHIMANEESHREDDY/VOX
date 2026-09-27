"""Runtime guardrails for tools that can affect the real desktop.

The agent is driven by untrusted-ish input (speech, which is easy for a
mis-hearing to corrupt) and acts on the real machine. Prompting the model to
"be careful" is not a control. This module is the control: every tool that
touches the machine asks :meth:`SafetyController.gate` for permission, and the
permission is bound to the exact arguments that were requested.

Three risk tiers:

``safe``
    Read-only. Always allowed.
``low``
    Changes state in a way that is annoying but reversible (open a window,
    focus a window, type into whatever has focus). Requires the agent to be
    armed.
``high``
    Hard to undo, or can hit the wrong target (clicks, drags, shell commands,
    closing windows, sending arbitrary keystrokes). Requires the agent to be
    armed *and* an explicit confirmation that is bound to the exact call, so a
    stale "yes" cannot be replayed onto a different action.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Risk = Literal["safe", "low", "high"]

RISK_ORDER: dict[str, int] = {"safe": 0, "low": 1, "high": 2}


@dataclass(frozen=True)
class Decision:
    """Outcome of a :meth:`SafetyController.gate` call."""

    allowed: bool
    reason: str = ""
    #: Set when a high-risk action needs consent; the tool must ask the user,
    #: then re-call the tool passing this id back as ``confirmation_id``.
    confirmation_id: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.confirmation_id is not None


@dataclass
class _Pending:
    """A high-risk action awaiting a spoken 'yes'."""

    id: str
    tool: str
    signature: str
    detail: dict[str, Any]
    created_at: float

    def expired(self, ttl: float) -> bool:
        return (time.time() - self.created_at) > ttl


class SafetyController:
    """Single source of truth for arm state, consent and the audit trail.

    One process-wide instance is exported as :data:`SAFETY`. Localhost control
    endpoints and the voice tools both mutate it, so the web UI and the spoken
    commands cannot disagree about whether the agent is live.
    """

    def __init__(
        self,
        *,
        audit_path: Path,
        confirmation_ttl: float = 90.0,
        auto_disarm_minutes: float = 15.0,
        allow_shell: bool = False,
        shell_denylist: list[str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._audit_path = audit_path
        self._confirmation_ttl = confirmation_ttl
        self._auto_disarm_seconds = auto_disarm_minutes * 60.0
        self._allow_shell = allow_shell
        self._shell_denylist = [d.lower() for d in (shell_denylist or [])]
        self._armed = False
        self._armed_at: float | None = None
        self._last_activity = time.time()
        self._pending: dict[str, _Pending] = {}
        #: Populated by the agent so the UI can render a live activity feed.
        self.listeners: list[Any] = []
        self.history: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- state

    @property
    def armed(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._armed

    def status(self) -> dict[str, Any]:
        """Snapshot for the UI, the ``doctor`` command and the agent's own tools."""
        with self._lock:
            self._expire_locked()
            return {
                "armed": self._armed,
                "armed_for_seconds": (
                    round(time.time() - self._armed_at, 1) if self._armed_at else 0.0
                ),
                "auto_disarm_minutes": self._auto_disarm_seconds / 60.0,
                "allow_shell": self._allow_shell,
                "confirmation_ttl_seconds": self._confirmation_ttl,
                "pending_confirmations": [
                    {
                        "id": p.id,
                        "tool": p.tool,
                        "detail": p.detail,
                        "age_seconds": round(time.time() - p.created_at, 1),
                    }
                    for p in self._pending.values()
                ],
                "recent_calls": self.history[-12:][::-1],
            }

    def arm(self, *, source: str = "voice") -> dict[str, Any]:
        with self._lock:
            self._armed = True
            self._armed_at = time.time()
            self._last_activity = time.time()
        record = {"event": "arm", "source": source}
        self._audit(record)
        return {"armed": True, "message": f"Agent armed via {source}."}

    def disarm(self, *, source: str = "voice", reason: str = "") -> dict[str, Any]:
        with self._lock:
            self._armed = False
            self._armed_at = None
            dropped = len(self._pending)
            self._pending.clear()
        record = {"event": "disarm", "source": source, "reason": reason, "dropped": dropped}
        self._audit(record)
        return {
            "armed": False,
            "message": f"Agent disarmed via {source}.{' ' + reason if reason else ''}",
        }

    # ---------------------------------------------------------------- gating

    @staticmethod
    def signature(tool: str, args: dict[str, Any]) -> str:
        """Deterministic identity of a specific call, used to bind consent to it.

        Must be stable across calls, because the gate re-computes it when the
        tool is retried with the confirmation id and compares it to what was
        recorded when the user gave their "yes".
        """
        payload = json.dumps(
            {"tool": tool, "args": args}, sort_keys=True, default=repr, separators=(",", ":")
        )
        return f"{tool}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    def shell_allowed(self, command: str) -> tuple[bool, str]:
        """Check a shell command against the denylist (case-insensitive)."""
        if not self._allow_shell:
            return False, "Shell command execution is disabled. Set ALLOW_SHELL=1 to enable it."
        lowered = command.lower()
        for needle in self._shell_denylist:
            if needle in lowered:
                return False, (
                    f"Blocked by the shell denylist (matched {needle!r}). "
                    "Tell the user this command is not permitted."
                )
        return True, ""

    def gate(
        self,
        tool: str,
        risk: Risk,
        args: dict[str, Any],
        *,
        confirmation_id: str | None = None,
        force_risk: Risk | None = None,
    ) -> Decision:
        """Decide whether ``tool`` may run with exactly ``args``.

        ``force_risk`` lets a tool escalate itself, e.g. a click is ``high`` but
        a click at the current cursor position in a text field is ``low``.
        """
        with self._lock:
            self._expire_locked()
            effective = force_risk or risk
            self._last_activity = time.time()

            if effective == "safe":
                self._record(tool, risk, args, "allowed", "read-only tool")
                return Decision(True, "read-only tool")

            if not self._armed:
                decision = Decision(
                    False,
                    "The agent is disarmed, so it cannot change anything on this machine. "
                    "Ask the user to say 'arm' or press the Arm button, then repeat the request.",
                )
                self._record(tool, risk, args, "refused", "disarmed")
                return decision

            if effective == "low":
                self._record(tool, risk, args, "allowed", "armed")
                return Decision(True, "armed")

            # effective == "high"
            if not confirmation_id:
                pending = self._create_pending_locked(tool, args)
                self._record(tool, risk, args, "needs_confirmation", pending.id)
                return Decision(
                    False,
                    "This action is high risk and needs explicit confirmation from the user. "
                    f"Say exactly what you are about to do and ask 'shall I proceed?'. "
                    f"If they agree, call confirm_action('{pending.id}') and then repeat this "
                    f"tool call with confirmation_id='{pending.id}'.",
                    confirmation_id=pending.id,
                )

            pending = self._pending.get(confirmation_id)
            if pending is None or pending.expired(self._confirmation_ttl):
                self._pending.pop(confirmation_id, None)
                self._record(tool, risk, args, "refused", "confirmation missing or expired")
                return Decision(
                    False,
                    "That confirmation is no longer valid. Ask the user to confirm again, then "
                    "use the new confirmation id.",
                )

            # Bind consent to this exact call. A mismatched signature means the
            # model is trying to reuse a 'yes' for something the user never saw.
            if self.signature(tool, args) != pending.signature:
                self._record(tool, risk, args, "refused", "confirmation bound to other args")
                return Decision(
                    False,
                    "The confirmation the user gave was for different arguments. Do not reuse it. "
                    "Ask the user to confirm this specific action.",
                )

            del self._pending[confirmation_id]
            self._record(tool, risk, args, "allowed", f"confirmed {confirmation_id}")
            return Decision(True, f"confirmed {confirmation_id}")

    # ------------------------------------------------------------- confirm API

    def _create_pending_locked(self, tool: str, args: dict[str, Any]) -> _Pending:
        # Bound the number of outstanding requests so a confused model cannot
        # accumulate a backlog of stale consents.
        if len(self._pending) >= 8:
            oldest = min(self._pending.values(), key=lambda p: p.created_at)
            self._pending.pop(oldest.id, None)
        pending = _Pending(
            id=secrets.token_hex(6),
            tool=tool,
            signature=self.signature(tool, args),
            detail=dict(args),
            created_at=time.time(),
        )
        self._pending[pending.id] = pending
        return pending

    def peek_pending(self, confirmation_id: str) -> dict[str, Any] | None:
        with self._lock:
            pending = self._pending.get(confirmation_id)
            if pending is None:
                return None
            return {
                "id": pending.id,
                "tool": pending.tool,
                "detail": pending.detail,
                "age_seconds": round(time.time() - pending.created_at, 1),
            }

    def confirm(self, confirmation_id: str) -> dict[str, Any]:
        """Mark a pending request as verbally approved by the user.

        The gate still re-validates the fingerprint on the follow-up call, so a
        successful ``confirm`` cannot by itself authorise anything.
        """
        pending = self.peek_pending(confirmation_id)
        if pending is None:
            return {
                "ok": False,
                "message": "Unknown or expired confirmation id. Ask the user to confirm again.",
            }
        if pending["age_seconds"] > self._confirmation_ttl:
            with self._lock:
                self._pending.pop(confirmation_id, None)
            return {
                "ok": False,
                "message": "That confirmation expired. Ask the user to confirm again.",
            }
        self._record(pending["tool"], "high", pending["detail"], "user_confirmed", confirmation_id)
        return {
            "ok": True,
            "message": (
                "User confirmed. Now call the original tool again, passing "
                f"confirmation_id='{confirmation_id}' and the identical arguments."
            ),
            "confirmation_id": confirmation_id,
            "tool": pending["tool"],
            "detail": pending["detail"],
        }

    # ----------------------------------------------------------------- audit

    def _record(
        self,
        tool: str,
        risk: str,
        args: dict[str, Any],
        outcome: str,
        note: str = "",
    ) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "tool": tool,
            "risk": risk,
            "outcome": outcome,
            "note": note,
            "args": _redact(args),
        }
        self.history.append(entry)
        del self.history[:-200]
        self._audit(entry)

    def _audit(self, entry: dict[str, Any]) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=repr) + "\n")
        except OSError:
            # Auditing must never take the agent down.
            pass

    # ------------------------------------------------------------- internals

    def _expire_locked(self) -> None:
        now = time.time()
        for key in [k for k, p in self._pending.items() if p.expired(self._confirmation_ttl)]:
            del self._pending[key]
        if (
            self._armed
            and self._auto_disarm_seconds > 0
            and (now - self._last_activity) > self._auto_disarm_seconds
        ):
            self._armed = False
            self._armed_at = None
            self._audit(
                {
                    "event": "auto_disarm",
                    "idle_seconds": round(now - self._last_activity, 1),
                }
            )


_SECRET_HINTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "auth")


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that looks like a credential before it hits the audit log."""
    clean: dict[str, Any] = {}
    for key, value in args.items():
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            clean[key] = "***redacted***"
        elif isinstance(value, str) and len(value) > 500:
            clean[key] = value[:500] + f"...[{len(value) - 500} chars truncated]"
        else:
            clean[key] = value
    return clean


#: Process-wide controller. The web control server and the voice tools share it.
SAFETY: SafetyController | None = None


def get_safety() -> SafetyController:
    global SAFETY
    if SAFETY is None:  # pragma: no cover - defensive
        raise RuntimeError("SafetyController has not been initialised; call init_safety() first.")
    return SAFETY


def init_safety(cfg) -> SafetyController:
    global SAFETY
    SAFETY = SafetyController(
        audit_path=cfg.safety.audit_path,
        confirmation_ttl=cfg.safety.confirmation_ttl,
        auto_disarm_minutes=cfg.safety.auto_disarm_minutes,
        allow_shell=cfg.safety.allow_shell,
        shell_denylist=cfg.safety.shell_denylist,
    )
    return SAFETY
