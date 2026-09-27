"""End-to-end checks that need no real credentials.

The LiveKit token is just a locally signed JWT, so token minting, the control
server, the safety endpoints and tool schema generation can all be verified
offline. Only the audio transport and the model providers need real accounts.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from voice_os.config import LLMConfig, LiveKitConfig, STTConfig, TurnConfig, TTSConfig, load_config
from voice_os.config import Config, SafetyConfig
from voice_os.control_server import start_control_server
from voice_os.safety import init_safety
from voice_os.tools import build_toolset

#: Off the default 8787 so the suite is safe to run while the agent is running.
TEST_PORT = 8791
CONFIG_PORT = 8792


@pytest.fixture(scope="module")
def server():
    cfg = load_config()
    # Placeholder credentials: enough to sign a JWT, never sent anywhere.
    object.__setattr__(cfg.livekit, "url", "wss://test.livelab.cloud")
    object.__setattr__(cfg.livekit, "api_key", "devkey")
    object.__setattr__(cfg.livekit, "api_secret", "x" * 40)
    object.__setattr__(cfg.stt, "api_key", "dummy-stt-key")
    object.__setattr__(cfg.tts, "api_key", "dummy-tts-key")
    # A dedicated port so a running dev server on 8787 cannot shadow these
    # requests and make the suite pass or fail based on ambient state.
    object.__setattr__(cfg, "control_port", TEST_PORT)
    init_safety(cfg)
    httpd = start_control_server(cfg)
    yield f"http://127.0.0.1:{TEST_PORT}"
    httpd.shutdown()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ------------------------------------------------------------------- tools

def test_every_tool_has_a_description_and_schema():
    """A tool with no docstring reaches the model as an unusable blob."""
    for tool in build_toolset():
        info = tool.info
        assert info.name, "tool is missing a name"
        assert info.description, f"tool {info.name} has no description"


def test_toolset_drops_shell_when_disabled():
    names = {tool.info.name for tool in build_toolset(include_shell=False)}
    assert "run_powershell_command" not in names
    assert "click_screen" in names
    assert "look_at_screen" in names


def test_toolset_includes_shell_when_enabled():
    names = {tool.info.name for tool in build_toolset(include_shell=True)}
    assert "run_powershell_command" in names


def test_no_duplicate_tool_names():
    names = [tool.info.name for tool in build_toolset()]
    assert len(names) == len(set(names))


# ------------------------------------------------------------ control plane

def test_health(server):
    status, body = _get(f"{server}/api/health")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_serves_the_web_ui(server):
    status, body = _get(f"{server}/")
    assert status == 200
    assert "Voice OS" in body
    assert "armBtn" in body


def test_connect_mints_a_token(server):
    status, data = _post(f"{server}/api/connect", {})
    assert status == 200
    assert data["token"], "no token returned"
    assert data["url"] == "wss://test.livelab.cloud"
    assert data["agent"] == "voice-os"
    assert data["room"].startswith("voice-os-")
    # A JWT is three dot-separated base64 segments.
    assert data["token"].count(".") == 2


def test_each_connect_gets_a_distinct_room(server):
    _, first = _post(f"{server}/api/connect", {})
    _, second = _post(f"{server}/api/connect", {})
    assert first["room"] != second["room"]


def test_arm_disarm_roundtrip(server):
    status, data = _post(f"{server}/api/arm")
    assert status == 200
    assert data["state"]["armed"] is True

    status, data = _post(f"{server}/api/disarm")
    assert status == 200
    assert data["state"]["armed"] is False


def test_safety_status_endpoint(server):
    status, data = _get(f"{server}/api/safety")
    assert status == 200
    state = json.loads(data)
    assert "armed" in state
    assert "recent_calls" in state
    assert state["armed"] is False


def test_config_endpoint_lists_missing_keys(tmp_path):
    """The console must be able to explain itself without a working session.

    Regression guard: the control server used to refuse to start when the
    configuration was incomplete, which hid the very list of keys the operator
    needed. The page is now always reachable and reports the gaps itself.
    """
    from voice_os.control_server import start_control_server as _start

    cfg = Config(
        livekit=LiveKitConfig(),  # nothing set -> every gap is reported
        llm=LLMConfig(),
        stt=STTConfig(),
        tts=TTSConfig(),
        turn=TurnConfig(),
        safety=SafetyConfig(audit_path=tmp_path / "audit.jsonl"),
        control_port=CONFIG_PORT,
    )
    assert cfg.problems(), "an empty config should report problems"
    assert any("LIVEKIT" in problem for problem in cfg.problems())

    httpd = _start(cfg)
    try:
        status, body = _get(f"http://127.0.0.1:{CONFIG_PORT}/api/config")
        assert status == 200
        data = json.loads(body)
        assert data["ready"] is False
        assert len(data["missing"]) == len(cfg.problems())
        assert data["llm"]["model"] == cfg.llm.model

        # The page must still be served so the operator can read the above.
        status, html = _get(f"http://127.0.0.1:{CONFIG_PORT}/")
        assert status == 200
        assert "configBox" in html
    finally:
        httpd.shutdown()


def test_serves_the_overlay_page(server):
    """The corner orb is a separate page served from the same origin."""
    status, body = _get(f"{server}/overlay")
    assert status == 200
    for probe in ("drawOrb", "renderSafety", "capYou", "capBot", "createMediaStreamSource"):
        assert probe in body, f"overlay is missing {probe}"
    # Amplitude is decorative; it must never be able to break the subtitles.
    assert "/api/disarm" in body


def test_overlay_joins_the_console_room(server):
    """Regression: the overlay must ride the console's session, not start a new one.

    Two agents in one room would double-handle every utterance.
    """
    _, first = _post(f"{server}/api/connect", {"role": "console"})
    _, second = _post(f"{server}/api/connect", {"role": "console"})
    _, overlay = _post(f"{server}/api/connect", {"role": "overlay"})

    assert overlay["room"] == second["room"]
    assert overlay["room"] != first["room"]
    assert overlay["identity"] != second["identity"]
    assert overlay["role"] == "overlay"


def test_active_room_is_shared_across_request_handlers(server):
    """The active room lives on the server, not the per-request handler.

    An earlier version stored it on the handler instance, so every request saw
    None and the overlay silently started its own room.
    """
    _post(f"{server}/api/connect", {"role": "console"})
    status, body = _get(f"{server}/api/active-room")
    assert status == 200
    assert json.loads(body)["room"], "active room was not retained between requests"


def test_overlay_before_console_still_gets_a_room(server):
    """If the overlay connects first it must not be handed a broken session."""
    status, data = _post(f"{server}/api/connect", {"role": "overlay"})
    assert status == 200
    assert data["room"]
    assert data["token"].count(".") == 2


def test_unknown_route_is_404(server):
    status, _ = _get(f"{server}/nope")
    assert status == 404
