"""Configuration loading for the desktop voice agent.

All configuration comes from environment variables, optionally seeded from a
``.env`` file next to the project root. Nothing here raises on missing values;
:func:`Config.problems` reports what is missing so ``main.py doctor`` can tell
the user exactly what to fix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"


def env_file_path() -> Path:
    """Which .env to load.

    Override with ``VOICE_OS_ENV=/path/to/file`` to run against a different
    profile without touching the main one.
    """
    override = os.environ.get("VOICE_OS_ENV", "").strip()
    return Path(override) if override else ENV_FILE


def load_dotenv(path: Path | None = None) -> None:
    """Seed ``os.environ`` from a ``.env`` file without overriding real env vars.

    Supports ``KEY=value``, ``export KEY=value`` and ``#`` comments. Values may be
    wrapped in matching single or double quotes.
    """
    path = path or env_file_path()
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class LiveKitConfig:
    """Credentials for the LiveKit transport that carries the realtime audio."""

    url: str = ""
    api_key: str = ""
    api_secret: str = ""
    agent_name: str = "vox"

    @property
    def missing(self) -> list[str]:
        gaps = []
        if not self.url:
            gaps.append("LIVEKIT_URL")
        if not self.api_key:
            gaps.append("LIVEKIT_API_KEY")
        if not self.api_secret:
            gaps.append("LIVEKIT_API_SECRET")
        return gaps


@dataclass(frozen=True)
class LLMConfig:
    """Chat model settings.

    ``base_url`` points at 9Router (an OpenAI-compatible gateway) by default, so
    the brain of the agent can be any of the providers 9Router fronts.
    """

    provider: str = "openai"
    model: str = "gpt-4.1"
    base_url: str = "http://localhost:20128/v1"
    api_key: str = "9router"
    temperature: float = 0.6
    max_completion_tokens: int = 1200


@dataclass(frozen=True)
class STTConfig:
    """Speech-to-text settings."""

    provider: str = "deepgram"
    model: str = "nova-3"
    language: str = "en-US"
    api_key: str = ""


@dataclass(frozen=True)
class TTSConfig:
    """Text-to-speech settings."""

    provider: str = "cartesia"
    model: str = "sonic-3"
    voice: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class TurnConfig:
    """Turn-taking tuning.

    ``turn_detection`` uses LiveKit's end-of-turn model so the agent stops
    talking the moment you finish your sentence instead of waiting on a silence
    timer. Set to ``vad`` to fall back to plain silence timing.

    The default is ``hosted``. ``livekit.plugins.turn_detector`` (the ``local``
    option) is deprecated in livekit-agents 1.8 and needs a job context to load,
    so it cannot be validated by ``doctor`` and is not the default here.
    """

    turn_detection: str = "hosted"
    endpointing_min_delay: float = 0.4
    endpointing_max_delay: float = 2.0
    allow_interruptions: bool = True
    min_interruption_words: int = 1
    min_interruption_duration: float = 0.5
    preemptive_generation: bool = False
    noise_cancellation: bool = False


@dataclass(frozen=True)
class SafetyConfig:
    """Guardrails for a voice agent that can move the mouse and run commands."""

    #: Seconds a pending high-risk confirmation stays valid.
    confirmation_ttl: float = 90.0
    #: Auto-disarm after this many minutes without a tool call. 0 disables.
    auto_disarm_minutes: float = 15.0
    #: Master switch for the ``run_powershell_command`` tool.
    allow_shell: bool = False
    #: Extra substrings that block a shell command even when the shell is enabled.
    shell_denylist: list[str] = field(
        default_factory=lambda: [
            "rm -rf",
            "format ",
            "diskpart",
            "reg delete",
            "reg add",
            "cipher /w",
            "cipher /x",
            "del /f /s /q",
            "remove-item -recurse",
            "rd /s /q",
            "takeown",
            "icacls",
            "shutdown",
            "stop-computer",
            "set-executionpolicy",
            "vssadmin",
            "bcdedit",
            "mkfs",
            "dd if=",
            ":(){",
            "invoke-expression",
            "iex ",
            "downloadstring",
            "certutil -decode",
            "bitsadmin",
        ]
    )
    #: Screenshots land here so the operator can see what the agent saw.
    screenshot_dir: Path = ROOT / "screenshots"
    #: Append-only JSONL record of every gated tool call.
    audit_path: Path = ROOT / "logs" / "audit.jsonl"


@dataclass(frozen=True)
class BrainConfig:
    """Settings for the in-process brain: memory, skills and the data kernel.

    The brain is the part of the system that gives the agent something to
    remember and something to know. It runs inside the voice worker rather than
    behind HTTP, so it has no separate auth and no separate permission model:
    every capability it exposes goes through the same
    :class:`~voice_os.safety.SafetyController` as the desktop tools.
    """

    #: Master switch. With this off the brain is never constructed and none of
    #: its tools are registered, so the agent is a pure desktop controller.
    enabled: bool = True
    #: Where the skill packs live. Each subdirectory holds a ``SKILL.md``.
    skills_dir: Path = ROOT / "skills"
    #: SQLite file for memories, preferences, conversations and artifacts.
    db_path: Path = ROOT / ".data" / "brain.db"
    #: Cap on how much of a skill body goes into one prompt. Skill bodies run
    #: from 800 bytes to 88 KB, and a voice turn cannot absorb the large ones.
    skill_brief_chars: int = 6000
    #: How many packs a skill search returns.
    skill_search_limit: int = 5
    #: Cap on rows returned by a single memory recall.
    recall_limit: int = 8
    #: Remember things the user says about themselves and their work.
    remember_enabled: bool = True
    #: The data kernel imports pandas and numpy at module level, which is a real
    #: cost at voice-session startup. It stays off until it is asked for.
    data_kernel_enabled: bool = False
    #: Blob storage for ingested data-kernel documents.
    blob_dir: Path = ROOT / ".data" / "blobs"


@dataclass(frozen=True)
class Config:
    livekit: LiveKitConfig
    llm: LLMConfig
    stt: STTConfig
    tts: TTSConfig
    turn: TurnConfig
    safety: SafetyConfig
    brain: BrainConfig = field(default_factory=BrainConfig)
    control_host: str = "127.0.0.1"
    control_port: int = 8787
    #: Which screen corner the always-on-top overlay anchors to.
    overlay_corner: str = "bottom-right"
    agent_name: str = "VOX"
    extra_instructions: str = ""

    def problems(self) -> list[str]:
        """Human-readable list of blocking configuration gaps."""
        gaps: list[str] = []
        for name in self.livekit.missing:
            gaps.append(f"{name} is required (LiveKit Cloud free tier: cloud.livekit.io)")
        if self.stt.provider in {"deepgram", "groq", "openai"} and not self.stt.api_key:
            gaps.append(
                f"{self.stt.provider.upper()}_API_KEY is required for the "
                f"{self.stt.provider} speech-to-text provider"
            )
        if self.tts.provider == "cartesia" and not self.tts.api_key:
            gaps.append("CARTESIA_API_KEY is required for the cartesia voice")
        if self.tts.provider == "openai" and not self.tts.api_key:
            gaps.append("OPENAI_API_KEY is required for the openai voice")
        return gaps


def load_config() -> Config:
    """Build a :class:`Config` from the environment."""
    load_dotenv()
    return Config(
        livekit=LiveKitConfig(
            url=env_str("LIVEKIT_URL"),
            api_key=env_str("LIVEKIT_API_KEY"),
            api_secret=env_str("LIVEKIT_API_SECRET"),
            agent_name=env_str("AGENT_NAME", "vox") or "vox",
        ),
        llm=LLMConfig(
            provider=env_str("LLM_PROVIDER", "openai") or "openai",
            model=env_str("LLM_MODEL", "gpt-4.1") or "gpt-4.1",
            base_url=env_str("LLM_BASE_URL", "http://localhost:20128/v1")
            or "http://localhost:20128/v1",
            api_key=env_str("LLM_API_KEY", "9router") or "9router",
            temperature=float(env_int("LLM_TEMPERATURE_X100", 60)) / 100.0,
            max_completion_tokens=env_int("LLM_MAX_TOKENS", 1200),
        ),
        stt=STTConfig(
            provider=env_str("STT_PROVIDER", "deepgram") or "deepgram",
            model=env_str("STT_MODEL", "nova-3") or "nova-3",
            language=env_str("STT_LANGUAGE", "en-US") or "en-US",
            api_key=env_str(f"{env_str('STT_PROVIDER', 'deepgram').upper()}_API_KEY"),
        ),
        tts=TTSConfig(
            provider=env_str("TTS_PROVIDER", "cartesia") or "cartesia",
            model=env_str("TTS_MODEL", "sonic-3") or "sonic-3",
            voice=env_str("TTS_VOICE"),
            api_key=env_str(
                "CARTESIA_API_KEY"
                if env_str("TTS_PROVIDER", "cartesia") == "cartesia"
                else "OPENAI_API_KEY"
            ),
        ),
        turn=TurnConfig(
            turn_detection=env_str("TURN_DETECTION", "hosted") or "hosted",
            endpointing_min_delay=float(env_int("TURN_MIN_DELAY_X100", 40)) / 100.0,
            endpointing_max_delay=float(env_int("TURN_MAX_DELAY_X100", 200)) / 100.0,
            allow_interruptions=env_bool("ALLOW_INTERRUPTIONS", True),
            min_interruption_words=env_int("MIN_INTERRUPTION_WORDS", 1),
            min_interruption_duration=float(env_int("MIN_INTERRUPTION_MS", 500)) / 1000.0,
            preemptive_generation=env_bool("PREEMPTIVE_GENERATION", False),
            noise_cancellation=env_bool("NOISE_CANCELLATION", False),
        ),
        safety=SafetyConfig(
            confirmation_ttl=float(env_int("CONFIRMATION_TTL", 90)),
            auto_disarm_minutes=float(env_int("AUTO_DISARM_MINUTES", 15)),
            allow_shell=env_bool("ALLOW_SHELL", False),
            shell_denylist=env_list("SHELL_DENYLIST", None) or SafetyConfig().shell_denylist,
            screenshot_dir=Path(env_str("SCREENSHOT_DIR", str(ROOT / "screenshots"))),
            audit_path=Path(env_str("AUDIT_PATH", str(ROOT / "logs" / "audit.jsonl"))),
        ),
        brain=BrainConfig(
            enabled=env_bool("BRAIN_ENABLED", True),
            skills_dir=Path(env_str("BRAIN_SKILLS_DIR", str(ROOT / "skills"))),
            db_path=Path(env_str("BRAIN_DB", str(ROOT / ".data" / "brain.db"))),
            skill_brief_chars=env_int("BRAIN_SKILL_BRIEF_CHARS", 6000),
            skill_search_limit=env_int("BRAIN_SKILL_SEARCH_LIMIT", 5),
            recall_limit=env_int("BRAIN_RECALL_LIMIT", 8),
            remember_enabled=env_bool("BRAIN_REMEMBER", True),
            data_kernel_enabled=env_bool("BRAIN_DATA_KERNEL", False),
            blob_dir=Path(env_str("BRAIN_BLOB_DIR", str(ROOT / ".data" / "blobs"))),
        ),
        control_host=env_str("CONTROL_HOST", "127.0.0.1") or "127.0.0.1",
        control_port=env_int("CONTROL_PORT", 8787),
        overlay_corner=env_str("OVERLAY_CORNER", "bottom-right") or "bottom-right",
        agent_name=env_str("AGENT_DISPLAY_NAME", "VOX")
        or "VOX",
        extra_instructions=env_str("AGENT_EXTRA_INSTRUCTIONS"),
    )
