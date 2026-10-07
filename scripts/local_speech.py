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
import wave

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI(title="vox-local-speech", version="0.1.0")

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
    model: str = "piper"
    voice: str = "en_US-lessac-medium"
    input: str
    response_format: str = "wav"
    speed: float = 1.0


@app.get("/v1/models")
def models() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": WHISPER_MODEL, "object": "model"},
            {"id": "piper", "object": "model"},
        ],
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("base.en"),
                     language: str = Form("en"), response_format: str = Form("json")):
    """OpenAI STT shape: multipart audio file in, {"text": ...} out."""
    audio = await file.read()
    tmp = io.BytesIO(audio)
    tmp.name = file.filename or "audio.wav"
    segments, _info = _whisper().transcribe(tmp, beam_size=1, language=language)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if response_format == "text":
        return Response(content=text, media_type="text/plain")
    return {"text": text}


@app.post("/v1/audio/speech")
def synthesize(req: SpeechRequest):
    """OpenAI TTS shape: {"model", "voice", "input"} in, WAV bytes out."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        _piper().synthesize_wav(req.input, wav)
    media = "audio/wav" if req.response_format == "wav" else f"audio/{req.response_format}"
    return Response(content=buf.getvalue(), media_type=media)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
