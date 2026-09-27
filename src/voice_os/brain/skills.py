"""Skill packs: a searchable library of documented capabilities.

A skill pack is a directory containing ``SKILL.md`` — YAML-ish frontmatter
(``name``, ``description``) followed by a Markdown body that acts as a system
prompt for one kind of work.

Why this module exists
----------------------
agent-os shipped 32 of these packs and, as far as behaviour went, used almost
none of them. Its loader read ``SKILL.md``, pulled out ``name`` and
``description``, and then threw the body away. What actually reached the model
was a comma-joined list of the *names*::

    You are the Frontend Designer specialist (skills: shadcn, canvas-design).

So a 20,000-line ``shadcn/cli.md`` was never consulted by anything. The packs
were documentation pretending to be capability.

This module keeps the part that was real — the ``description`` fields are
genuinely good capability summaries, and they are what makes retrieval work —
and adds the part that was missing: the body is actually readable, on demand,
size-capped. A voice turn has a small prompt budget, so
:meth:`SkillIndex.brief` returns a bounded digest rather than the whole file,
and :meth:`SkillIndex.references` tells the model what else is on disk so it
can ask for a specific file instead of being handed 88 KB of prose.

No third-party dependencies. The frontmatter parser is hand-rolled because the
packs are not uniformly valid YAML and a strict parser rejects files that the
rest of the system happily reads.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

#: Matches a frontmatter key at the start of a line, e.g. ``name:`` or ``license:``.
_FRONT_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")

#: A frontmatter fence: ``---`` on its own line, or ``---`` with trailing junk.
_FENCE = re.compile(r"^---\s*$")

#: Default cap on a skill digest, in characters. Roughly 2.5k tokens, which is
#: a large slice of a voice turn but tolerable once. Callers can lower it.
DEFAULT_BRIEF_CHARS = 6000

#: Words that carry no retrieval signal. Kept small on purpose: over-stemming
#: hurts more than it helps when the corpus is only 32 descriptions.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in is it its of on or that the
    their then there these this to use used using was were what when where which who
    will with you your""".split()
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOPWORDS]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` frontmatter from the Markdown body.

    Returns ``(fields, body)``. Only top-level ``key: value`` pairs are read, and
    a value may continue onto following indented or non-``key:`` lines, which is
    how the long ``description:`` fields in these packs are written. A file with
    no fence yields empty fields and the whole text as the body, so a malformed
    pack degrades to "readable text" rather than being invisible.
    """
    lines = text.splitlines()
    # Only treat the head as frontmatter when it is actually fenced. Without
    # this, a pack with no ``---`` header would be scanned to EOF looking for a
    # closing fence that never comes, and its entire body would be discarded.
    if not lines or not _FENCE.match(lines[0].strip()):
        return {}, text.strip()

    fields: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []
    index = 1
    closed = False
    while index < len(lines):
        line = lines[index]
        if _FENCE.match(line.strip()):
            index += 1
            closed = True
            break
        match = _FRONT_KEY.match(line)
        if match and not line[:1].isspace():
            if key is not None:
                fields[key] = " ".join(part for part in buf if part).strip()
            key = match.group(1).lower()
            buf = [match.group(2).strip()]
        elif key is not None:
            buf.append(line.strip())
        index += 1
    if key is not None:
        fields[key] = " ".join(part for part in buf if part).strip()
    # An unterminated fence means the whole file was frontmatter and there is
    # no body. Returning the text keeps a malformed pack visible instead of
    # silently empty.
    body = "\n".join(lines[index:]).strip() if closed else text.strip()
    return fields, body


def _clean(value: str) -> str:
    """Strip wrapping quotes and collapse whitespace in a frontmatter value."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return " ".join(value.split())


@dataclass
class Skill:
    """One skill pack on disk."""

    #: Directory name. This is the canonical identifier and is what the model
    #: must use, because two packs declare the same frontmatter ``name``
    #: (``coder-agent`` and ``coder`` both say ``name: coder``).
    id: str
    name: str
    description: str
    path: Path
    body_chars: int = 0
    has_references: bool = False
    reference_count: int = 0

    @property
    def label(self) -> str:
        return f"{self.id} ({self.name})" if self.name != self.id else self.id


@dataclass
class SkillDigest:
    """A size-capped view of one skill, sized for a voice prompt."""

    skill: Skill
    text: str
    truncated: bool
    references: list[str] = field(default_factory=list)


def _score(query_tokens: list[str], skill_tokens: set[str], id_tokens: set[str]) -> float:
    """Weight a match in the description above a match in the identifier.

    A pack literally named ``researcher`` matching the word "researcher" is
    weak evidence; a pack whose *description* says it verifies facts matching
    "research" is strong. The 1.0/2.0 split is crude but the corpus is small
    and hand-checkable, which matters more here than tuning.
    """
    if not query_tokens:
        return 0.0
    description_hits = sum(1 for t in query_tokens if t in skill_tokens)
    id_hits = sum(1 for t in query_tokens if t in id_tokens)
    return description_hits * 2.0 + id_hits * 1.0


class SkillIndex:
    """Discovers, ranks and reads skill packs.

    Cheap to construct: discovery reads only each pack's frontmatter, so
    building the index does not pull 4 MB of prose into memory. Bodies are read
    on demand and cached by ``(path, mtime, size)`` so editing a pack during a
    session is picked up without a restart.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._skills: dict[str, Skill] = {}
        self._token_cache: dict[str, set[str]] = {}
        self._body_cache: dict[str, tuple[tuple, str]] = {}
        self._indexed_at: float = 0.0

    # ------------------------------------------------------------- discovery

    def refresh(self) -> int:
        """Rescan the skills directory. Returns the number of packs found."""
        found: dict[str, Skill] = {}
        if self.root.is_dir():
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.is_file():
                    continue
                try:
                    fields, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
                except OSError:
                    continue
                skill_id = entry.name
                references = [
                    p
                    for p in sorted((entry / "references").rglob("*"))
                    if p.is_file()
                ] if (entry / "references").is_dir() else []
                scripts = [
                    p for p in sorted((entry / "scripts").rglob("*")) if p.is_file()
                ] if (entry / "scripts").is_dir() else []
                found[skill_id] = Skill(
                    id=skill_id,
                    name=_clean(fields.get("name", "")) or skill_id,
                    description=_clean(fields.get("description", ""))
                    or "No description provided.",
                    path=skill_md,
                    body_chars=len(body),
                    has_references=bool(references or scripts),
                    reference_count=len(references) + len(scripts),
                )
        with self._lock:
            self._skills = found
            # Drop cached bodies for packs that no longer exist.
            self._body_cache = {k: v for k, v in self._body_cache.items() if k in found}
        return len(found)

    def _ensure(self) -> None:
        if not self._skills:
            self.refresh()

    # ---------------------------------------------------------------- queries

    def all(self) -> list[Skill]:
        self._ensure()
        with self._lock:
            return sorted(self._skills.values(), key=lambda s: s.id)

    def count(self) -> int:
        self._ensure()
        with self._lock:
            return len(self._skills)

    def get(self, skill_id: str) -> Skill | None:
        self._ensure()
        with self._lock:
            return self._skills.get(skill_id.strip().lower())

    def search(self, query: str, limit: int = 5) -> list[Skill]:
        """Rank packs by overlap between the query and each pack's summary.

        Descriptions are indexed, not bodies. That is deliberate: the body is
        the *answer* to a question, so matching on it would rank a pack highly
        for containing the word "design" in a 2000-line reference list whether
        or not it can do what the user asked.
        """
        self._ensure()
        query_tokens = _tokenize(query)
        if not query_tokens:
            return self.all()[:limit]
        with self._lock:
            items = list(self._skills.values())
            for skill in items:
                cached = self._token_cache.get(skill.id)
                if cached is None:
                    cached = set(_tokenize(f"{skill.id} {skill.name} {skill.description}"))
                    self._token_cache[skill.id] = cached
                skill._desc_tokens = cached  # type: ignore[attr-defined]
                skill._id_tokens = set(_tokenize(f"{skill.id} {skill.name}"))  # type: ignore[attr-defined]
        scored = [
            (_score(query_tokens, skill._desc_tokens, skill._id_tokens), skill)  # type: ignore[attr-defined]
            for skill in items
        ]
        hits = [pair for pair in scored if pair[0] > 0]
        # Nothing matched: returning the whole library ranked by id is more
        # useful than an empty list, because the model can then ask by name.
        if not hits:
            return sorted(items, key=lambda s: s.id)[:limit]
        hits.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [skill for _, skill in hits[:limit]]

    # ---------------------------------------------------------------- reading

    def _read_body(self, skill: Skill) -> str:
        with self._lock:
            try:
                stat = skill.path.stat()
                stamp = (stat.st_mtime, stat.st_size)
            except OSError:
                return ""
            cached = self._body_cache.get(skill.id)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            _, body = _parse_frontmatter(skill.path.read_text(encoding="utf-8"))
        except OSError:
            return ""
        with self._lock:
            self._body_cache[skill.id] = (stamp, body)
        return body

    def brief(self, skill_id: str, max_chars: int = DEFAULT_BRIEF_CHARS) -> SkillDigest | None:
        """Return a size-capped digest of a pack's body plus its reference list.

        The reference list is included even when the body is truncated, because
        that is the signal the model needs to ask for the rest by name instead
        of concluding the pack has nothing more.
        """
        skill = self.get(skill_id)
        if skill is None:
            return None
        body = self._read_body(skill)
        truncated = len(body) > max_chars
        text = body[:max_chars]
        references: list[str] = []
        if skill.has_references:
            references = self._reference_paths(skill)
        return SkillDigest(
            skill=skill, text=text, truncated=truncated, references=references
        )

    def _reference_paths(self, skill: Skill) -> list[str]:
        pack_dir = skill.path.parent
        out: list[str] = []
        for sub in ("references", "scripts", "assets", "templates"):
            folder = pack_dir / sub
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    out.append(str(path.relative_to(pack_dir)).replace("\\", "/"))
        return out

    def read_reference(self, skill_id: str, relative: str, max_chars: int = 20000) -> str | None:
        """Read one file from inside a pack.

        ``relative`` is resolved and then checked to be inside the pack, so a
        model cannot walk out of the skills tree with ``../..``.
        """
        skill = self.get(skill_id)
        if skill is None:
            return None
        pack_dir = skill.path.parent.resolve()
        try:
            target = (pack_dir / relative).resolve()
        except OSError:
            return None
        if not target.is_relative_to(pack_dir) or not target.is_file():
            return None
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if len(text) > max_chars:
            return text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
        return text

    # ------------------------------------------------------------- reporting

    def catalogue(self, max_description: int = 160) -> str:
        """One compact line per pack, for a system prompt or a spoken answer."""
        lines = []
        for skill in self.all():
            desc = skill.description
            if len(desc) > max_description:
                desc = desc[: max_description - 1].rstrip() + "…"
            lines.append(f"- {skill.id}: {desc}")
        return "\n".join(lines)


__all__ = ["Skill", "SkillDigest", "SkillIndex", "DEFAULT_BRIEF_CHARS"]
