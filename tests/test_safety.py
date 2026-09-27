"""Safety-layer tests.

The permission model is the part of this system that must not silently
regress, so it is tested directly rather than through the agent.
"""

from __future__ import annotations

import time

import pytest

from voice_os.config import SafetyConfig
from voice_os.safety import SafetyController


@pytest.fixture()
def safety(tmp_path):
    # Use the real production denylist so these tests exercise the config that
    # actually ships, not a convenient stub.
    return SafetyController(
        audit_path=tmp_path / "audit.jsonl",
        confirmation_ttl=90.0,
        auto_disarm_minutes=15.0,
        allow_shell=False,
        shell_denylist=SafetyConfig().shell_denylist,
    )


# --------------------------------------------------------------- read-only

def test_safe_tools_work_while_disarmed(safety):
    decision = safety.gate("look_at_screen", "safe", {"question": "what is this?"})
    assert decision.allowed
    assert decision.confirmation_id is None


def test_state_changing_tools_are_refused_while_disarmed(safety):
    decision = safety.gate("focus_window", "low", {"title": "Notepad"})
    assert not decision.allowed
    assert "disarmed" in decision.reason.lower()


# ----------------------------------------------------------------- arming

def test_arming_permits_low_risk(safety):
    safety.arm()
    assert safety.armed
    assert safety.gate("focus_window", "low", {"title": "Notepad"}).allowed


def test_disarm_revokes_low_risk(safety):
    safety.arm()
    safety.disarm()
    assert not safety.armed
    assert not safety.gate("focus_window", "low", {"title": "Notepad"}).allowed


def test_idle_auto_disarm(safety):
    safety.arm()
    safety._auto_disarm_seconds = 0.01
    time.sleep(0.05)
    assert not safety.armed


# --------------------------------------------------------- high risk + consent

def test_high_risk_needs_confirmation_then_proceeds(safety):
    safety.arm()
    args = {"x": 100, "y": 200}

    blocked = safety.gate("click_screen", "high", args)
    assert not blocked.allowed
    assert blocked.needs_confirmation
    cid = blocked.confirmation_id

    confirmed = safety.confirm(cid)
    assert confirmed["ok"]

    allowed = safety.gate("click_screen", "high", args, confirmation_id=cid)
    assert allowed.allowed, allowed.reason


def test_confirmation_is_single_use(safety):
    safety.arm()
    args = {"x": 1, "y": 2}
    cid = safety.gate("click_screen", "high", args).confirmation_id
    safety.confirm(cid)
    assert safety.gate("click_screen", "high", args, confirmation_id=cid).allowed
    # A second click needs a fresh confirmation.
    assert not safety.gate("click_screen", "high", args, confirmation_id=cid).allowed


def test_confirmation_cannot_be_replayed_onto_different_arguments(safety):
    """The whole point of fingerprinting: a 'yes' is for one exact action."""
    safety.arm()
    cid = safety.gate("click_screen", "high", {"x": 10, "y": 10}).confirmation_id
    safety.confirm(cid)

    # Same tool, same confirmation id, different target.
    replay = safety.gate("click_screen", "high", {"x": 999, "y": 999}, confirmation_id=cid)
    assert not replay.allowed
    assert "different arguments" in replay.reason


def test_confirmation_on_a_different_tool_is_rejected(safety):
    safety.arm()
    cid = safety.gate("click_screen", "high", {"x": 1, "y": 1}).confirmation_id
    safety.confirm(cid)
    other = safety.gate("press_key", "high", {"key": "enter"}, confirmation_id=cid)
    assert not other.allowed


def test_confirmation_expires(safety):
    safety.arm()
    args = {"x": 5, "y": 5}
    cid = safety.gate("click_screen", "high", args).confirmation_id
    safety.confirm(cid)
    safety._confirmation_ttl = -1.0  # force expiry
    assert not safety.gate("click_screen", "high", args, confirmation_id=cid).allowed


def test_disarm_drops_pending_confirmations(safety):
    safety.arm()
    cid = safety.gate("click_screen", "high", {"x": 1, "y": 1}).confirmation_id
    safety.disarm()
    safety.arm()
    assert safety.gate("click_screen", "high", {"x": 1, "y": 1}, confirmation_id=cid).allowed is False


def test_pending_queue_is_bounded(safety):
    safety.arm()
    for i in range(20):
        safety.gate("click_screen", "high", {"x": i, "y": i})
    assert len(safety.status()["pending_confirmations"]) <= 8


# ------------------------------------------------------------------ shell

def test_shell_disabled_by_default(safety):
    allowed, why = safety.shell_allowed("git status")
    assert not allowed
    assert "ALLOW_SHELL" in why


def test_shell_denylist_blocks_destructive(safety):
    safety._allow_shell = True
    allowed, why = safety.shell_allowed("Remove-Item -Recurse C:\\stuff")
    assert not allowed
    assert "denylist" in why

    ok, _ = safety.shell_allowed("git status")
    assert ok


def test_shell_denylist_is_case_insensitive(safety):
    safety._allow_shell = True
    allowed, _ = safety.shell_allowed("FORMAT C:")
    assert not allowed


# ------------------------------------------------------------------ audit

def test_audit_log_records_the_decision(safety, tmp_path):
    safety.gate("look_at_screen", "safe", {"question": "hi"})
    # Refused while disarmed, then armed, then high risk asks for consent.
    safety.gate("click_screen", "high", {"x": 1, "y": 2})
    safety.arm()
    safety.gate("click_screen", "high", {"x": 1, "y": 2})
    safety.gate("focus_window", "low", {"title": "Notepad"})

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5  # safe, refused, arm, needs_confirmation, allowed
    assert '"outcome": "refused"' in lines[1]
    assert '"event": "arm"' in lines[2]
    assert '"outcome": "needs_confirmation"' in lines[3]
    assert '"outcome": "allowed"' in lines[4]


def test_audit_redacts_secrets(safety, tmp_path):
    safety.gate("authenticate", "safe", {"api_key": "sk-super-secret", "user": "me"})
    log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-super-secret" not in log
    assert "redacted" in log


def test_audit_truncates_huge_arguments(safety, tmp_path):
    safety.gate("type_text", "safe", {"text": "x" * 5000})
    log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "chars truncated" in log


# ----------------------------------------------------------------- status

def test_status_reports_armed_and_pending(safety):
    safety.arm()
    safety.gate("click_screen", "high", {"x": 3, "y": 3})
    status = safety.status()
    assert status["armed"] is True
    assert len(status["pending_confirmations"]) == 1
    assert status["pending_confirmations"][0]["tool"] == "click_screen"
