"""Tests for the local OpenAI-compatible speech shim.

Deepgram and Cartesia were rejected upstream of these endpoints, yet nothing
pinned the shim's behaviour, so swapping it for an OpenAI-shaped server would
fail silently. These tests exercise the real FastAPI app in-process through
TestClient: a WAV synthesized by /v1/audio/speech must be transcribable by
/v1/audio/transcriptions, proving STT and TTS agree on shapes and that both
paths run real models.
"""

from __future__ import annotations

import io
import struct
import sys
import wave
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

local_speech = pytest.importorskip(
    "local_speech",
    reason="scripts/local_speech.py needs the [speech] extra (fastapi, uvicorn, "
    "faster-whisper, piper-tts, python-multipart)",
)

from fastapi.testclient import TestClient  # noqa: E402

PIPER_WAV_RATE = 22050  # en_US-lessac-medium's native rate


@pytest.fixture(scope="module")
def client():
    return TestClient(local_speech.app)


def _synth(client, text: str) -> bytes:
    resp = client.post(
        "/v1/audio/speech",
        json={"model": "piper", "voice": "en_US-lessac-medium", "input": text,
              "response_format": "wav"},
    )
    assert resp.status_code == 200, resp.text
    return resp.content


def test_models_lists_the_two_local_models(client):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()["data"]}
    assert {"base.en", "piper"} <= ids


def test_speech_returns_a_real_wav(client):
    audio = _synth(client, "The local voice server is alive.")
    # RIFF/WAVE header, non-trivial payload: a few tens of KB, not silence.
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert wav.getnchannels() in (1, 2)
        assert wav.getsampwidth() == 2
        frames = wav.readframes(wav.getnframes())
    assert len(frames) > 10_000
    # Not pure silence: a synthesised utterance must have real signal.
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    assert max(abs(min(samples)), abs(max(samples))) > 500


def test_transcriptions_round_trip_through_piper(client):
    audio = _synth(client, "Hello from the local voice server.")
    resp = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", audio, "audio/wav")},
        data={"model": "base.en", "language": "en"},
    )
    assert resp.status_code == 200, resp.text
    text = resp.json()["text"].lower()
    assert "hello" in text
    assert "local" in text and "voice" in text and "server" in text
