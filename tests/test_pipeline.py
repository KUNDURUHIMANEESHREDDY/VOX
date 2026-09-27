"""Pipeline construction tests.

Building the speech pipeline is where a wrong provider name or a missing
dependency surfaces, and it happens before any audio flows, so it is worth
pinning down.
"""

from __future__ import annotations

import pytest

from voice_os.config import Config, LLMConfig, SafetyConfig, STTConfig, TurnConfig, TTSConfig
from voice_os.pipeline import (
    PipelineError,
    build_llm,
    build_pipeline,
    build_turn_detection,
    build_turn_handling,
)


@pytest.fixture()
def cfg(tmp_path) -> Config:
    return Config(
        livekit=__import__("voice_os.config", fromlist=["LiveKitConfig"]).LiveKitConfig(),
        llm=LLMConfig(provider="openai", model="ag/claude-sonnet-4-6",
                      base_url="http://127.0.0.1:20128/v1", api_key="test-key"),
        stt=STTConfig(provider="deepgram", model="nova-3", api_key="test-stt"),
        tts=TTSConfig(provider="cartesia", model="sonic-3", api_key="test-tts"),
        turn=TurnConfig(turn_detection="off"),
        safety=SafetyConfig(audit_path=tmp_path / "audit.jsonl"),
    )


# --------------------------------------------------------------------- llm

def test_llm_points_at_the_router_base_url(cfg):
    """The 9Router integration is nothing more than base_url, so prove it lands.

    Without this the agent would silently talk to api.openai.com with a 9Router
    key and fail only once someone started speaking.
    """
    llm = build_llm(cfg)
    assert llm.model == "ag/claude-sonnet-4-6"
    assert str(llm._client.base_url).rstrip("/") == "http://127.0.0.1:20128/v1"


def test_llm_falls_back_to_openai_when_no_base_url(cfg, monkeypatch):
    """No base_url must mean the plugin default, not whatever the env says.

    The OpenAI plugin reads OPENAI_BASE_URL as an override, so this only holds
    if that variable is absent. Clearing it here rather than trusting the
    ambient environment is what makes the assertion real: without the
    monkeypatch this test passed or failed depending on whether a .env file
    happened to sit next to the repo.
    """
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    object.__setattr__(cfg.llm, "base_url", "")
    llm = build_llm(cfg)
    assert "api.openai.com" in str(llm._client.base_url)


def test_llm_honours_an_explicit_openai_base_url_override(cfg, monkeypatch):
    """The env override really does win, which is why the test above clears it.

    Pins the behaviour the previous test was accidentally asserting against.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:20128/v1")
    object.__setattr__(cfg.llm, "base_url", "")
    llm = build_llm(cfg)
    assert "20128" in str(llm._client.base_url)


def test_unknown_llm_provider_is_rejected(cfg):
    object.__setattr__(cfg.llm, "provider", "anthropic-native")
    with pytest.raises(PipelineError, match="Unknown LLM provider"):
        build_llm(cfg)


# ----------------------------------------------------------------- unknown

def test_unknown_stt_provider_is_rejected(cfg):
    object.__setattr__(cfg.stt, "provider", "mystery")
    with pytest.raises(PipelineError, match="Unknown STT provider"):
        build_pipeline(cfg)


def test_unknown_tts_provider_is_rejected(cfg):
    object.__setattr__(cfg.tts, "provider", "mystery")
    with pytest.raises(PipelineError, match="Unknown TTS provider"):
        build_pipeline(cfg)


def test_unknown_turn_mode_is_rejected(cfg):
    object.__setattr__(cfg.turn, "turn_detection", "telepathy")
    with pytest.raises(PipelineError, match="Unknown TURN_DETECTION"):
        build_turn_detection(cfg)


# ------------------------------------------------------------ turn taking

def test_turn_detection_off_returns_none(cfg):
    assert build_turn_detection(cfg) is None


def test_turn_detection_vad_returns_sentinel(cfg):
    object.__setattr__(cfg.turn, "turn_detection", "vad")
    assert build_turn_detection(cfg) == "vad"


def test_turn_handling_always_sets_endpointing(cfg):
    handling = build_turn_handling(cfg, None)
    assert handling["turn_detection"] is None
    assert handling["endpointing"]["mode"] == "dynamic"
    assert handling["endpointing"]["min_delay"] == cfg.turn.endpointing_min_delay


def test_interruptions_can_be_disabled(cfg):
    object.__setattr__(cfg.turn, "allow_interruptions", False)
    handling = build_turn_handling(cfg, None)
    assert handling["interruption"]["enabled"] is False


def test_interruptions_enabled_uses_adaptive_mode(cfg):
    handling = build_turn_handling(cfg, None)
    assert handling["interruption"]["enabled"] is True
    assert handling["interruption"]["mode"] == "adaptive"
    assert handling["interruption"]["min_words"] == cfg.turn.min_interruption_words


# ------------------------------------------------------------------ report

def test_pipeline_describe_mentions_every_stage(cfg):
    pipeline = build_pipeline(cfg)
    text = pipeline.describe()
    for expected in ("stt=", "llm=", "tts=", "vad=", "turn="):
        assert expected in text, f"{expected} missing from describe()"
    assert "vad-only" in text
