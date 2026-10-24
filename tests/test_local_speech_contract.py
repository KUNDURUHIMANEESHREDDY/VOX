"""Fast, model-free contract tests for the local speech HTTP shim."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

local_speech = pytest.importorskip(
    "local_speech",
    reason="install FastAPI and python-multipart to run local speech contract tests",
)

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    return TestClient(local_speech.app)


def test_request_size_middleware_rejects_declared_oversize_before_route():
    async def scenario():
        called = False
        events = []

        async def downstream(scope, receive, send):
            nonlocal called
            called = True

        async def receive():
            raise AssertionError("oversized declared bodies must be rejected before reading")

        async def send(message):
            events.append(message)

        middleware = local_speech.BodySizeLimitMiddleware(downstream, max_bytes=8)
        await middleware(
            {"type": "http", "headers": [(b"content-length", b"9")]},
            receive,
            send,
        )
        assert called is False
        assert events[0]["status"] == 413

    asyncio.run(scenario())


def test_request_size_middleware_rejects_streaming_body_over_limit():
    async def scenario():
        events = []

        async def downstream(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def receive():
            return {"type": "http.request", "body": b"123456789", "more_body": False}

        async def send(message):
            events.append(message)

        middleware = local_speech.BodySizeLimitMiddleware(downstream, max_bytes=8)
        await middleware({"type": "http", "headers": []}, receive, send)
        assert events[0]["status"] == 413

    asyncio.run(scenario())


def test_request_size_middleware_rejects_ambiguous_framing():
    async def scenario():
        events = []

        async def downstream(scope, receive, send):
            raise AssertionError("ambiguous framing must be rejected before route handling")

        async def receive():
            raise AssertionError("ambiguous framing must be rejected before reading")

        async def send(message):
            events.append(message)

        middleware = local_speech.BodySizeLimitMiddleware(downstream, max_bytes=8)
        await middleware(
            {
                "type": "http",
                "headers": [
                    (b"content-length", b"1"),
                    (b"content-length", b"1"),
                ],
            },
            receive,
            send,
        )
        assert events[0]["status"] == 400

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "unknown"},
        {"voice": "unknown"},
        {"response_format": "mp3"},
        {"speed": 1.5},
        {"input": " "},
        {"input": "x" * (local_speech.MAX_TTS_INPUT_CHARACTERS + 1)},
    ],
)
def test_speech_rejects_unsupported_or_unbounded_request_values(client, overrides):
    payload = {
        "model": "piper",
        "voice": "en_US-lessac-medium",
        "input": "hello",
        "response_format": "wav",
        "speed": 1.0,
    }
    payload.update(overrides)
    response = client.post("/v1/audio/speech", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "form",
    [
        {"model": "large-v3"},
        {"language": "fr"},
        {"response_format": "srt"},
    ],
)
def test_transcription_rejects_unsupported_contract_values(client, form):
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("audio.wav", b"audio", "audio/wav")},
        data=form,
    )
    assert response.status_code == 422


def test_transcription_rejects_empty_audio_before_model_loading(client):
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert response.status_code == 400


def test_transcription_rejects_oversized_audio_before_model_loading(client, monkeypatch):
    monkeypatch.setattr(local_speech, "MAX_AUDIO_UPLOAD_BYTES", 8)
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("large.wav", b"123456789", "audio/wav")},
    )
    assert response.status_code == 413


def test_transcription_rejects_unexpected_content_type_before_model_loading(client):
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("audio.txt", b"not audio", "text/plain")},
    )
    assert response.status_code == 415
