"""End-to-end session regression test.

Every other test in this suite builds *pieces* of the agent: config, pipeline,
safety, brain, tools. Several serious livekit-agents 1.8 failures were invisible
to them precisely because they never constructed a session. The incident that
motivated this file: ``AgentSession.start`` in 1.8 validates its room argument
and rejects ``RoomInputOptions`` with ``"expected RoomOptions, got
RoomInputOptions"`` -- every job died before the agent joined, and only a live
run showed it.

So this test constructs the real thing through the same path ``main.py`` uses
-- :func:`build_agent`, :func:`build_session`, then ``session.start`` with
``room_options=RoomOptions()`` inside a ``fake_job_context`` (no worker, no
network, no audio). If a LiveKit upgrade changes the validation again, this
fails here instead of after deploy.
"""

from __future__ import annotations

import pytest
from livekit import rtc
from livekit.agents.testing import fake_job_context
from livekit.agents.utils import http_context
from livekit.agents.voice.room_io import RoomOptions

from voice_os.agent import build_agent, build_session
from voice_os.config import (
    Config,
    LLMConfig,
    LiveKitConfig,
    SafetyConfig,
    STTConfig,
    TTSConfig,
    TurnConfig,
)
from voice_os.pipeline import Pipeline, build_llm, build_stt, build_tts, build_turn_handling


@pytest.fixture()
def cfg(tmp_path) -> Config:
    return Config(
        livekit=LiveKitConfig(),
        llm=LLMConfig(
            provider="openai",
            model="ag/claude-sonnet-4-6",
            base_url="http://127.0.0.1:20128/v1",
            api_key="test-key",
        ),
        stt=STTConfig(provider="deepgram", model="nova-3", api_key="test-stt"),
        tts=TTSConfig(provider="cartesia", model="sonic-3", api_key="test-tts"),
        turn=TurnConfig(turn_detection="off"),
        safety=SafetyConfig(audit_path=tmp_path / "audit.jsonl"),
    )


def _pipeline(cfg: Config) -> Pipeline:
    """The same stages ``build_pipeline`` assembles, minus the VAD.

    Loading Silero needs a job context and a model download; neither belongs in
    a unit test. The HTTP session for STT/TTS comes from ``http_context.open()``
    below, so no plugin touches the network without a job context.
    """
    turn_detection = None  # TurnConfig(turn_detection="off") -> no turn detector
    return Pipeline(
        stt=build_stt(cfg),
        llm=build_llm(cfg),
        tts=build_tts(cfg),
        vad=None,
        turn_detection=turn_detection,
        turn_handling=build_turn_handling(cfg, turn_detection),
    )


async def test_session_start_accepts_room_options(cfg):
    """The 1.8 regression: start() must accept room_options=RoomOptions().

    This mirrors ``main.build_worker_options`` -- the exact keyword that 1.8
    renamed. Passing ``room_input_options`` here (the pre-1.8 shape) is what
    killed every job, so this pins the new shape against the real validator.
    """
    pipeline = _pipeline(cfg)
    room = rtc.Room()
    try:
        with fake_job_context(room=room) as ctx:
            agent = build_agent(cfg)
            session = await build_session(ctx, cfg, pipeline)
            try:
                async with http_context.open():
                    await session.start(agent, room=room, room_options=RoomOptions())
            finally:
                await session.aclose()
    finally:
        await room.disconnect()


async def test_session_wires_the_real_agent_and_toolset(cfg):
    """A session built the real way carries the whole gated toolset.

    The tool list lives on the session in 1.8 (the agent keeps only the
    persona), so this is the only level at which a missing or shadowed tool
    would surface.
    """
    pipeline = _pipeline(cfg)
    room = rtc.Room()
    try:
        with fake_job_context(room=room) as ctx:
            session = await build_session(ctx, cfg, pipeline)
            names = set()
            for tool in session.tools:
                name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
                names.add(name)
            # Read-only and gated alike: the gate is only meaningful if both
            # kinds of tool are actually registered.
            for expected in ("look_at_screen", "recall_memory", "arm_agent", "remember_this"):
                assert expected in names, f"{expected} missing from session tools"
            # 40 = 30 desktop + 10 brain tools (11 with the shell), see README.
            assert len(session.tools) >= 30
    finally:
        await room.disconnect()


async def test_session_turn_handling_comes_from_the_pipeline(cfg):
    """The dict ``build_turn_handling`` returns must survive into the session.

    If LiveKit ever stops accepting the dict shape, the session silently falls
    back to defaults and voice turns change without any test noticing.
    """
    pipeline = _pipeline(cfg)
    room = rtc.Room()
    try:
        with fake_job_context(room=room) as ctx:
            session = await build_session(ctx, cfg, pipeline)
            handling = session.options.turn_handling
            assert handling["turn_detection"] is None
            assert handling["endpointing"]["mode"] == "dynamic"
            assert handling["interruption"]["enabled"] is True
            assert handling["interruption"]["mode"] == "adaptive"
            assert handling["interruption"]["min_words"] == cfg.turn.min_interruption_words
    finally:
        await room.disconnect()
