"""Local OpenAI-compatible speech shim: Whisper STT + Piper TTS.

Replaces the two hosted providers VOX used by default (Deepgram and Cartesia)
with open-source models that run on this machine. The agent's pipeline only
knows how to talk OpenAI-shaped HTTP, so this server speaks just enough of that
shape for both:

    POST /v1/audio/transcriptions   (multipart form, OpenAI STT)
    POST /v1/audio/speech           (JSON body, OpenAI TTS)
    GET  /v1/models                 (probe, lists the two model ids)

No auth, loopback only. Run:

    python scripts/local_speech.py            # default port 8090
    python scripts/local_speech.py --port 8090

Models are downloaded on first use into ~/.cache (faster-whisper) and the
Piper onnx is expected at .data/models/en_US-lessac-medium.onnx.
"""

from __future__ import annotations

import argparse
import io
import json
import wave
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

MAX_REQUEST_BODY_BYTES = 26 * 1024 * 1024
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_TTS_INPUT_CHARACTERS = 10_000


class _RequestBodyTooLarge(Exception):
    """Raised when a streaming request exceeds the configured byte budget."""


class BodySizeLimitMiddleware:
    """Reject oversized HTTP bodies before multipart parsing or model loading."""

    def __init__(self, app, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        transfer_encodings = [
            value for key, value in headers if key.lower() == b"transfer-encoding"
        ]
        if len(lengths) > 1 or len(transfer_encodings) > 1 or (lengths and transfer_encodings):
            await self._reject(send, 400, "Ambiguous request framing.")
            return
        if lengths:
            raw_length = lengths[0]
            if not raw_length or not raw_length.isdigit():
                await self._reject(send, 400, "Invalid Content-Length.")
                return
            # A header with more digits than the configured cap cannot be a
            # valid in-range request; avoid converting unbounded decimal input.
            if len(raw_length) > len(str(self.max_bytes)) or int(raw_length) > self.max_bytes:
                await self._reject(send, 413, "Request body exceeds the configured limit.")
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if not response_started:
                await self._reject(send, 413, "Request body exceeds the configured limit.")


app = FastAPI(title="vox-local-speech", version="0.1.0")
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)

_WHISPER = None
_PIPER = None

WHISPER_MODEL = "base.en"
PIPER_MODEL = ".data/models/en_US-lessac-medium.onnx"


def _whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel

        # int8 keeps it cold-start friendly on CPU; base.en is a real-time
        # factor well under 1 on a laptop.
        _WHISPER = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _WHISPER


def _piper():
    global _PIPER
    if _PIPER is None:
        from piper import PiperVoice

        _PIPER = PiperVoice.load(PIPER_MODEL)
    return _PIPER


class SpeechRequest(BaseModel):
    model: Literal["piper"] = "piper"
    voice: Literal["en_US-lessac-medium"] = "en_US-lessac-medium"
    input: str = Field(min_length=1, max_length=MAX_TTS_INPUT_CHARACTERS)
    response_format: Literal["wav"] = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@app.get("/v1/models")
def models() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": WHISPER_MODEL, "object": "model"},
            {"id": "piper", "object": "model"},
        ],
    }


async def _read_bounded_audio(file: UploadFile) -> bytes:
    """Read at most the supported audio limit and fail before model inference."""
    allowed_types = {
        "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3",
        "audio/ogg", "audio/webm", "audio/mp4", "application/octet-stream",
    }
    if file.content_type and file.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="Unsupported audio content type.")
    audio = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
    if not audio:
        raise HTTPException(status_code=400, detail="Audio upload is empty.")
    if len(audio) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio upload exceeds the configured limit.")
    return audio


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: Literal["base.en"] = Form("base.en"),
    language: Literal["en"] = Form("en"),
    response_format: Literal["json", "text"] = Form("json"),
):
    """OpenAI STT shape: bounded multipart audio in, transcript out."""
    audio = await _read_bounded_audio(file)
    tmp = io.BytesIO(audio)
    tmp.name = file.filename or "audio.wav"
    segments, _info = _whisper().transcribe(tmp, beam_size=1, language=language)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if response_format == "text":
        return Response(content=text, media_type="text/plain")
    return {"text": text}


@app.post("/v1/audio/speech")
def synthesize(req: SpeechRequest):
    """OpenAI-compatible TTS shape with explicit local-model limitations."""
    if req.speed != 1.0:
        raise HTTPException(
            status_code=422,
            detail="The local Piper shim currently supports speed=1.0 only.",
        )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        _piper().synthesize_wav(req.input, wav)
    return Response(content=buf.getvalue(), media_type="audio/wav")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
