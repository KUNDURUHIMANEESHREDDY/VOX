"""Localhost control plane and static web client.

Two jobs, one origin:

* Mint LiveKit access tokens so the browser can join a room and have this agent
  dispatched into it. Without this the user would have to copy tokens by hand.
* Expose arm/disarm, safety status and the audit tail to the page, so the
  operator can see exactly what the agent is allowed to do and has done.

Bound to loopback and unauthenticated by design: it is a local operator console
for a single-user tool. Do not change the bind address without adding auth.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from livekit import api

from .agent import AGENT_NAME
from .config import Config
from .safety import get_safety

logger = logging.getLogger("voice_os.control")

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


def issue_token(cfg: Config, room: str, identity: str) -> str:
    """Create a participant token that auto-dispatches the agent into ``room``."""
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    room_config = api.RoomConfiguration(
        agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)],
        empty_timeout=300,
        departure_timeout=20,
    )
    token = (
        api.AccessToken(cfg.livekit.api_key, cfg.livekit.api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .with_room_config(room_config)
        .with_ttl(timedelta(hours=2))
    )
    return token.to_jwt()


class _Handler(BaseHTTPRequestHandler):
    server_version = "DesktopVoiceControl/1.0"
    cfg: Config

    @property
    def active_room(self) -> str | None:
        """The room the console last joined.

        Stored on the server, not on the handler: a fresh handler is built for
        every request, so instance state would be thrown away each time and the
        overlay would never find the console's room.
        """
        return getattr(self.server, "active_room", None)

    @active_room.setter
    def active_room(self, value: str | None) -> None:
        self.server.active_room = value

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- responses

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    # ----------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._send_file(WEB_ROOT / "index.html")
        elif path in {"/overlay", "/overlay.html"}:
            self._send_file(WEB_ROOT / "overlay.html")
        elif path == "/api/safety":
            self._send_json(200, get_safety().status())
        elif path == "/api/health":
            self._send_json(200, {"ok": True, "agent": AGENT_NAME})
        elif path == "/api/active-room":
            self._send_json(200, {"room": self.active_room})
        elif path == "/api/config":
            # The console serves this so it can tell the operator exactly which
            # keys are missing instead of just failing to connect.
            self._send_json(
                200,
                {
                    "ready": not self.cfg.problems(),
                    "missing": self.cfg.problems(),
                    "llm": {"model": self.cfg.llm.model, "base_url": self.cfg.llm.base_url},
                    "stt": {"provider": self.cfg.stt.provider, "model": self.cfg.stt.model},
                    "tts": {"provider": self.cfg.tts.provider, "model": self.cfg.tts.model},
                    "turn_detection": self.cfg.turn.turn_detection,
                    "allow_shell": self.cfg.safety.allow_shell,
                    "brain": {
                        "enabled": self.cfg.brain.enabled,
                        "skills": self.cfg.brain.skill_search_limit,
                    },
                },
            )
        elif path == "/api/brain":
            # Brain status is richer than a config echo and worth its own
            # endpoint, but it stays on the same loopback-only, unauthenticated
            # terms as everything else this server exposes.
            self._send_json(200, self._brain_status())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_json()

        if path == "/api/connect":
            return self._handle_connect(body)
        if path == "/api/arm":
            result = get_safety().arm(source="web-ui")
            return self._send_json(200, {**result, "state": get_safety().status()})
        if path == "/api/disarm":
            result = get_safety().disarm(source="web-ui", reason="operator button")
            return self._send_json(200, {**result, "state": get_safety().status()})
        if path == "/api/confirm":
            return self._send_json(200, get_safety().confirm(str(body.get("confirmation_id", ""))))
        if path == "/api/pending":
            pending_id = str(body.get("confirmation_id", ""))
            return self._send_json(
                200, {"pending": get_safety().peek_pending(pending_id)}
            )
        return self._send_json(404, {"error": "not found"})

    def _brain_status(self) -> dict:
        """Report what the brain holds, without ever failing the request.

        A brain that will not open its database is a degraded agent, not a dead
        console, so the error is reported in the payload and the status code
        stays 200. This runs on a request thread with no event loop, which is
        why it uses the brain's synchronous status path rather than awaiting
        :meth:`Brain.status`.
        """
        if not self.cfg.brain.enabled:
            return {"enabled": False}
        try:
            from .brain import get_brain

            return {"enabled": True, **get_brain(self.cfg.brain).quick_status()}
        except Exception as exc:
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

    def _handle_connect(self, body: dict) -> None:
        gaps = self.cfg.problems()
        if gaps:
            return self._send_json(
                503,
                {
                    "error": "agent is not configured yet",
                    "missing": gaps,
                    "hint": "copy .env.example to .env, fill it in, then restart",
                },
            )

        role = str(body.get("role") or "console").strip().lower()
        if role == "overlay" and self.active_room:
            # Reuse the console's room so the overlay rides the same session
            # instead of triggering a second agent join.
            room = self.active_room
        else:
            room = (
                str(body.get("room") or "").strip()
                or f"voice-os-{secrets.token_hex(3)}"
            )
            if role != "overlay":
                self.active_room = room

        identity = str(body.get("identity") or "").strip() or f"operator-{secrets.token_hex(2)}"
        if role == "overlay":
            identity = f"overlay-{secrets.token_hex(2)}"

        try:
            token = issue_token(self.cfg, room, identity)
        except Exception as exc:
            logger.exception("could not mint a token")
            return self._send_json(500, {"error": f"could not mint a LiveKit token: {exc}"})
        self._send_json(
            200,
            {
                "token": token,
                "url": self.cfg.livekit.url,
                "room": room,
                "identity": identity,
                "agent": AGENT_NAME,
                "role": role,
            },
        )


def start_control_server(cfg: Config) -> ThreadingHTTPServer:
    """Start the control server on a daemon thread and return the server."""
    handler = type("_BoundHandler", (_Handler,), {"cfg": cfg})
    httpd = ThreadingHTTPServer((cfg.control_host, cfg.control_port), handler)
    httpd.daemon_threads = True
    # Shared across every request handler, so the overlay can find the console's
    # room no matter which thread serves it.
    httpd.active_room = None
    thread = threading.Thread(target=httpd.serve_forever, name="control-server", daemon=True)
    thread.start()
    logger.info(
        "control server on http://%s:%d", cfg.control_host, cfg.control_port
    )
    return httpd
