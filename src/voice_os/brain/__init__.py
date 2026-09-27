"""The brain: what the agent knows, in-process, with no second model.

This is the half of agent-os that earned its place. agent-os was five services
— an LLM gateway, a multi-agent supervisor, a memory sidecar, a visual studio
and a data kernel — reachable only over HTTP, each with its own bearer token
and its own notion of who may do what. The voice agent already had a working
realtime LLM loop with real function calling, so keeping a second, prompt-based
reasoning loop alongside it bought latency and a second permission model, and
nothing else.

What survives the merge is the part that is genuinely stateful or genuinely
hard to rewrite:

* :mod:`~voice_os.brain.memory_store` — persistent memory with hybrid recall
  (keyword + vector arms fused by Reciprocal Rank Fusion). Ported from
  ``loom_core/memory.py``.
* :mod:`~voice_os.brain.skills` — the skill-pack library, now actually readable.
* the data kernel — provenance, grounding and time-travel over ingested
  documents, ported from ``data-kernel/``.

What is deliberately *not* here: an LLM client. The brain answers with facts and
the agent decides what to do with them, in the session that can call a tool.
That keeps a single reasoning path, a single model, and a single set of
credentials.

Permission is not handled here at all. Every method on this class is called
from a tool in :mod:`voice_os.tools.brain`, and that tool asks the shared
:class:`~voice_os.safety.SafetyController` first, exactly like every desktop
tool does. The brain cannot act on the machine; it can only be asked.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ..config import BrainConfig
from .memory_store import MemoryEntry, PersistentMemory
from .skills import Skill, SkillDigest, SkillIndex

logger = logging.getLogger("voice_os.brain")

#: Module-level singleton. The brain is process-wide because memory is meant to
#: outlive a session, while the safety controller is deliberately not: arm state
#: is re-created per job so a new session never inherits permissions.
_BRAIN: "Brain | None" = None
_BRAIN_LOCK = threading.RLock()


class Brain:
    """Memory, skills and (optionally) the data kernel, in one object.

    Construction is cheap and does no heavy imports. The data kernel in
    particular imports pandas and numpy at module level, so it is not touched
    until something actually asks for it.
    """

    def __init__(self, cfg: BrainConfig) -> None:
        # Normalise path fields up front. BrainConfig is annotated as holding
        # Path objects and load_config() always provides them, but a config
        # built by hand (tests, an embedder, a future CLI) can pass a string,
        # and the failure would surface much later as an AttributeError on
        # .name inside an unrelated status call. Frozen dataclass, so this has
        # to go through object.__setattr__.
        for attr in ("skills_dir", "db_path", "blob_dir"):
            value = getattr(cfg, attr, None)
            if isinstance(value, str):
                object.__setattr__(cfg, attr, Path(value))
        self.cfg = cfg
        self.skills = SkillIndex(cfg.skills_dir)
        self._kernel: Any = None
        self._kernel_error: str = ""
        self._kernel_lock = threading.RLock()
        # An explicit path, never the module-level singleton: agent-os's
        # get_memory() latched the first caller's database path for the whole
        # process, which made per-test and per-profile isolation impossible.
        self.memory = PersistentMemory(str(cfg.db_path))

    # ----------------------------------------------------------------- status

    def describe(self) -> str:
        """One line for ``doctor`` and the console.

        Deliberately does not touch SQLite: ``doctor`` runs before the worker
        does, and counting rows is not worth an await on the startup path. The
        console and the agent's own status tool report the real counts.
        """
        return (
            f"skills={self.skills.count()} db={self.cfg.db_path.name} "
            f"kernel={'on' if self.kernel_available() else 'off'}"
        )

    async def status(self) -> dict[str, Any]:
        """Structured status for the operator console and the ``brain_status`` tool."""
        try:
            stats = await self.memory.get_stats()
        except Exception as exc:
            logger.warning("memory stats unavailable: %s", exc)
            stats = {}
        return {
            "enabled": self.cfg.enabled,
            "skills": self.skills.count(),
            "memories": stats.get("total_memories", 0),
            "memories_by_type": stats.get("memories_by_type", {}),
            "conversations": stats.get("total_conversations", 0),
            "preferences": stats.get("total_preferences", 0),
            "db_path": str(self.cfg.db_path),
            "data_kernel": self.kernel_available(),
            "data_kernel_error": self._kernel_error,
        }

    def quick_status(self) -> dict[str, Any]:
        """Same shape as :meth:`status`, computed without awaiting.

        The control server answers requests on plain threads with no event loop,
        and a status read should never be the thing that forces one into being
        created. The row counts are a few COUNT queries, so they are done
        directly rather than through the async memory API.
        """
        memories = conversations = preferences = 0
        error = ""
        try:
            from .memory_store import _connect

            with _connect(self.memory.db_path) as conn:
                memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                conversations = conn.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0]
                preferences = conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("could not read memory counts: %s", exc)
        return {
            "describe": self.describe(),
            "skills": self.skills.count(),
            "memories": memories,
            "conversations": conversations,
            "preferences": preferences,
            "db_path": str(self.cfg.db_path),
            "data_kernel": self.kernel_available(),
            "catalogue": self.skill_catalogue(),
            **({"error": error} if error else {}),
        }

    # ----------------------------------------------------------------- memory

    async def recall(self, query: str, limit: int | None = None) -> list[MemoryEntry]:
        """Hybrid keyword + vector recall, fused by RRF.

        Works while the agent is disarmed, because reading the user's own memory
        changes nothing on the machine.
        """
        limit = limit or self.cfg.recall_limit
        try:
            return await self.memory.search_memories(query, limit=limit)
        except Exception as exc:
            logger.warning("memory recall failed: %s", exc)
            return []

    async def remember(
        self, content: str, memory_type: str = "fact", metadata: dict[str, Any] | None = None
    ) -> MemoryEntry | None:
        """Store something worth keeping.

        Only ever called from an explicit tool, never as a side effect of the
        model finishing a sentence. agent-os extracted "facts" out of its own
        final answer with a handful of regexes, which for a voice agent driven
        by fallible transcription is a memory-poisoning primitive: one misheard
        sentence becomes permanent.
        """
        if not self.cfg.remember_enabled:
            return None
        content = (content or "").strip()
        if not content:
            return None
        try:
            return await self.memory.add_memory(content, memory_type=memory_type, metadata=metadata)
        except Exception as exc:
            logger.warning("could not store memory: %s", exc)
            return None

    async def relevant_facts(self, query: str, limit: int = 3) -> list[str]:
        """Facts only, as plain strings, for prompt injection by the caller."""
        try:
            return await self.memory.get_relevant_facts(query, limit=limit)
        except Exception as exc:
            logger.warning("fact recall failed: %s", exc)
            return []

    async def set_preference(self, key: str, value: str) -> bool:
        try:
            await self.memory.add_preference(key, value)
            return True
        except Exception as exc:
            logger.warning("could not store preference: %s", exc)
            return False

    async def preferences(self) -> dict[str, Any]:
        try:
            return await self.memory.get_all_preferences()
        except Exception:
            return {}

    # ----------------------------------------------------------------- skills

    def find_skills(self, query: str, limit: int | None = None) -> list[Skill]:
        return self.skills.search(query, limit=limit or self.cfg.skill_search_limit)

    def skill_catalogue(self) -> str:
        return self.skills.catalogue()

    def read_skill(self, skill_id: str, max_chars: int | None = None) -> SkillDigest | None:
        return self.skills.brief(skill_id, max_chars=max_chars or self.cfg.skill_brief_chars)

    def read_skill_file(self, skill_id: str, relative: str) -> str | None:
        return self.skills.read_reference(skill_id, relative)

    # ------------------------------------------------------------ data kernel

    def kernel_available(self) -> bool:
        """Whether the data kernel is switched on and imports cleanly.

        Deliberately does not raise: ``doctor`` needs to report a broken kernel
        without taking the voice agent down with it.
        """
        if not self.cfg.data_kernel_enabled:
            return False
        if self._kernel is not None:
            return True
        if self._kernel_error:
            return False
        try:
            self._load_kernel()
        except Exception as exc:
            self._kernel_error = f"{type(exc).__name__}: {exc}"
            logger.warning("data kernel unavailable: %s", self._kernel_error)
            return False
        return self._kernel is not None

    def _load_kernel(self) -> Any:
        """Import the data kernel, inserting its root on ``sys.path`` on demand.

        The kernel is a sys.path root rather than an installable package: its
        129 modules import each other absolutely (``from core.object.model
        import ...``). Rewriting those imports would touch every file and risk
        the 660-test suite for no behavioural gain, so the directory is put on
        the path instead — the same thing agent-os's own evals did.
        """
        with self._kernel_lock:
            if self._kernel is not None:
                return self._kernel
            import sys

            kernel_root = Path(__file__).resolve().parent / "data_kernel"
            if not kernel_root.is_dir():
                raise RuntimeError(f"data kernel not found at {kernel_root}")
            root_str = str(kernel_root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)

            from dataos_system import DataOS  # type: ignore[import-not-found]

            self.cfg.blob_dir.mkdir(parents=True, exist_ok=True)
            self._kernel = DataOS(
                db_path=str(self.cfg.db_path.with_name("kernel.db")),
                blob_dir=str(self.cfg.blob_dir),
            )
            return self._kernel

    def kernel(self) -> Any:
        """The live :class:`DataOS`, or raise with an actionable message."""
        if not self.cfg.data_kernel_enabled:
            raise RuntimeError(
                "The data kernel is disabled. Set BRAIN_DATA_KERNEL=true in .env to enable it. "
                "It imports pandas and numpy, so it stays off until you want document search."
            )
        if not self.kernel_available():
            raise RuntimeError(
                f"The data kernel is switched on but not usable ({self._kernel_error}). "
                'Install its dependencies with: pip install -e ".[kernel]"'
            )
        return self._kernel

    def close(self) -> None:
        kernel = self._kernel
        self._kernel = None
        if kernel is None:
            return
        try:
            kernel.close()
        except Exception:
            pass


def get_brain(cfg: BrainConfig | None = None) -> Brain:
    """Return the process-wide brain, constructing it on first use.

    With no argument, falls back to the environment configuration rather than
    raising. That matters because the tools in :mod:`voice_os.tools.brain` call
    this on every invocation: requiring an explicit init from ``agent.py`` would
    turn any startup ordering mistake, or a brain that failed to build, into a
    ``RuntimeError`` string in the user's ear instead of a working agent minus
    its memory.
    """
    global _BRAIN
    if cfg is not None and _BRAIN is not None and _BRAIN.cfg != cfg:
        # A different profile was requested. Rebuild rather than silently
        # serving the previous profile's memory.
        _BRAIN.close()
        _BRAIN = None
    with _BRAIN_LOCK:
        if _BRAIN is None:
            if cfg is None:
                from ..config import load_config

                cfg = load_config().brain
            _BRAIN = Brain(cfg)
        return _BRAIN


def reset_brain() -> None:
    """Drop the process-wide brain. Used by tests and by profile switches."""
    global _BRAIN
    with _BRAIN_LOCK:
        if _BRAIN is not None:
            _BRAIN.close()
        _BRAIN = None


__all__ = [
    "Brain",
    "get_brain",
    "reset_brain",
    "MemoryEntry",
    "PersistentMemory",
    "Skill",
    "SkillDigest",
    "SkillIndex",
]
