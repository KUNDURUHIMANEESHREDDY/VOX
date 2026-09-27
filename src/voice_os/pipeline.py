"""Speech pipeline construction.

Turns the declarative :class:`~voice_os.config.Config` into live STT, LLM,
TTS and VAD objects. Provider selection is data-driven so a user can move from
9Router to a hosted LLM, or Deepgram to OpenAI for speech, purely through the
environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from livekit.agents import EndpointingOptions, InterruptionOptions
from livekit.agents import stt as stt_api
from livekit.agents import tts as tts_api
from livekit.agents.llm import LLM

from .config import Config


class PipelineError(RuntimeError):
    """Raised when a provider cannot be built from the current configuration."""


@dataclass
class Pipeline:
    """The resolved speech pipeline for one agent session."""

    stt: stt_api.STT
    llm: LLM
    tts: tts_api.TTS
    vad: Any
    turn_detection: Any
    turn_handling: dict[str, Any]

    def describe(self) -> str:
        return (
            f"stt={type(self.stt).__name__}({getattr(self.stt, 'model', '?')}) "
            f"llm={type(self.llm).__name__}({getattr(self.llm, 'model', '?')}) "
            f"tts={type(self.tts).__name__}({getattr(self.tts, 'model', '?')}) "
            f"vad={self._vad_label()} "
            f"turn={self._turn_label()}"
        )

    def _turn_label(self) -> str:
        mode = self.turn_detection
        if mode is None:
            return "vad-only"
        if isinstance(mode, str):
            return mode
        return getattr(mode, "model", None) or type(mode).__name__

    def _vad_label(self) -> str:
        # A pipeline built outside a job has no VAD yet; it arrives in prewarm.
        # Say so rather than printing "None", which reads like a failure.
        return "pending" if self.vad is None else type(self.vad).__name__


def build_stt(cfg: Config) -> stt_api.STT:
    provider = cfg.stt.provider.lower()
    if provider == "deepgram":
        from livekit.plugins import deepgram

        return deepgram.STT(
            model=cfg.stt.model,
            language=cfg.stt.language,
            interim_results=True,
            punctuate=True,
            smart_format=True,
            no_delay=True,
            api_key=cfg.stt.api_key,
        )
    if provider == "openai":
        from livekit.plugins import openai as openai_plugin

        return openai_plugin.STT(
            model=cfg.stt.model,
            language=cfg.stt.language.split("-")[0],
            api_key=cfg.stt.api_key,
        )
    raise PipelineError(
        f"Unknown STT provider {cfg.stt.provider!r}. Supported: deepgram, openai."
    )


def build_llm(cfg: Config) -> LLM:
    """Build the chat model.

    The default is the OpenAI plugin pointed at 9Router's OpenAI-compatible
    endpoint, which is why ``base_url`` matters more than ``api_key`` here: any
    provider 9Router fronts becomes available without a code change.
    """
    provider = cfg.llm.provider.lower()
    if provider != "openai":
        raise PipelineError(
            f"Unknown LLM provider {cfg.llm.provider!r}. Only 'openai' is wired up, "
            "but it accepts any OpenAI-compatible base_url (that is how 9Router is used)."
        )
    from livekit.plugins import openai as openai_plugin

    kwargs: dict[str, Any] = {
        "model": cfg.llm.model,
        "temperature": cfg.llm.temperature,
    }
    if cfg.llm.base_url:
        kwargs["base_url"] = cfg.llm.base_url
    if cfg.llm.api_key:
        kwargs["api_key"] = cfg.llm.api_key
    return openai_plugin.LLM(**kwargs)


def build_tts(cfg: Config) -> tts_api.TTS:
    provider = cfg.tts.provider.lower()
    if provider == "cartesia":
        from livekit.plugins import cartesia

        kwargs: dict[str, Any] = {
            "model": cfg.tts.model,
            "api_key": cfg.tts.api_key,
        }
        if cfg.tts.voice:
            kwargs["voice"] = cfg.tts.voice
        return cartesia.TTS(**kwargs)
    if provider == "openai":
        from livekit.plugins import openai as openai_plugin

        kwargs = {
            "model": cfg.tts.model,
            "api_key": cfg.tts.api_key,
        }
        if cfg.tts.voice:
            kwargs["voice"] = cfg.tts.voice
        return openai_plugin.TTS(**kwargs)
    raise PipelineError(
        f"Unknown TTS provider {cfg.tts.provider!r}. Supported: cartesia, openai."
    )


def build_vad(cfg: Config) -> Any:
    """Load the Silero VAD.

    Loaded from ``prewarm`` rather than per job. It works outside a job context,
    but it is documented as blocking and slow on first call, so paying that cost
    once per process keeps job dispatch fast.
    """
    from livekit.plugins import silero

    return silero.VAD.load()


def build_turn_detection(cfg: Config) -> Any:
    """Choose how the agent decides the user has finished speaking.

    ``hosted``
        LiveKit's streaming end-of-turn model. Fastest and the only option with
        backchannel awareness, so the agent tolerates "mm-hm" without cutting in.
    ``local``
        Runs the multilingual model in-process. No extra network hop, slightly
        less responsive.
    ``vad`` / ``off``
        Plain silence timing.
    """
    mode = (cfg.turn.turn_detection or "hosted").lower()
    if mode in {"off", "none", "disabled"}:
        return None
    if mode in {"vad", "silence"}:
        return "vad"
    if mode in {"hosted", "cloud", "inference"}:
        from livekit.agents import inference

        return inference.TurnDetector(version="v1-mini", local_fallback=True)
    if mode in {"local", "multilingual"}:
        from livekit.plugins import turn_detector

        try:
            return turn_detector.multilingual.MultilingualModel()
        except RuntimeError as exc:
            # livekit.plugins.turn_detector is deprecated in 1.8 and its model
            # load requires a job context, so it fails outside the entrypoint
            # with a message aimed at nobody. Say what is actually wrong.
            raise PipelineError(
                "TURN_DETECTION=local uses the deprecated livekit.plugins.turn_detector, "
                f"which needs a job context and could not load here ({exc}). "
                "Use TURN_DETECTION=hosted: it is the supported path, it is faster, and it is "
                "the only option with backchannel awareness, so the agent tolerates 'mm-hm'."
            ) from exc
    raise PipelineError(
        f"Unknown TURN_DETECTION {cfg.turn.turn_detection!r}. "
        "Supported: hosted, local, vad, off."
    )


def build_turn_handling(cfg: Config, turn_detection: Any) -> dict[str, Any]:
    """Assemble the ``turn_handling`` dict for ``AgentSession``."""
    handling: dict[str, Any] = {
        "turn_detection": turn_detection,
        "endpointing": EndpointingOptions(
            mode="dynamic",
            min_delay=cfg.turn.endpointing_min_delay,
            max_delay=cfg.turn.endpointing_max_delay,
        ),
    }
    if not cfg.turn.allow_interruptions:
        handling["interruption"] = InterruptionOptions(enabled=False)
    else:
        handling["interruption"] = InterruptionOptions(
            enabled=True,
            mode="adaptive",
            min_words=cfg.turn.min_interruption_words,
            min_duration=cfg.turn.min_interruption_duration,
        )
    return handling


def build_pipeline(cfg: Config, *, vad: Any = None) -> Pipeline:
    """Assemble the pipeline.

    ``vad`` is passed in rather than built here because loading it needs a job
    context (see :func:`build_vad`). Leaving it ``None`` gives a pipeline that
    is valid for inspection -- which is what ``doctor`` needs -- but not for
    serving audio.
    """
    turn_detection = build_turn_detection(cfg)
    return Pipeline(
        stt=build_stt(cfg),
        llm=build_llm(cfg),
        tts=build_tts(cfg),
        vad=vad,
        turn_detection=turn_detection,
        turn_handling=build_turn_handling(cfg, turn_detection),
    )
