"""Embeddings for loom memory — pluggable, zero required dependencies.

Design constraint: recall must improve without forcing a multi-gigabyte
torch/sentence-transformers install. So:

  1. HashingWordEmbedder  — DEFAULT. sklearn HashingVectorizer (sklearn is
     already a dependency). Stateless: no vocabulary fitting, so vectors
     written today stay comparable with queries made next week. Catches
     morphological and phrase variants ("running" vs "run", exact phrases).
  2. SentenceTransformerEmbedder — OPTIONAL. Real paraphrase semantics.
     Only used when `sentence-transformers` is installed. Imported lazily
     inside __init__, never at module import time (import_hygiene eval).
  3. CharNgramHashEmbedder — last-resort fallback when sklearn is absent.
     Pure stdlib, so recall still works on a bare install.

Vectors are stored as space-separated float strings in the existing
memories.embedding TEXT column (compact, and readable with CAST(embedding
AS REAL) in SQL if ever needed).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Dict, List, Optional, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _l2(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return [0.0] * len(vec)
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class HashingWordEmbedder:
    """Default embedder. Stateless hashed bag-of-words with bigrams."""

    name = "hashing-word"
    dim = 256

    def __init__(self, dim: int = 256):
        self.dim = dim
        self._vec = None
        try:
            from sklearn.feature_extraction.text import HashingVectorizer
            self._vec = HashingVectorizer(
                n_features=dim, alternate_sign=False, norm=None,
                analyzer="word", ngram_range=(1, 2), lowercase=True,
            )
        except ImportError:
            self._vec = None

    def encode(self, text: str) -> List[float]:
        if self._vec is not None:
            return _l2(self._vec.transform([text or ""]).toarray()[0].tolist())
        return self._hash_fallback(text)

    def _hash_fallback(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        toks = tokenize(text)
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for g in grams:
            h = int(hashlib.blake2b(g.encode(), digest_size=8).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return _l2(vec)


class SentenceTransformerEmbedder:
    """Optional real-semantics embedder. Import is lazy and guarded."""

    name = "sentence-transformers"
    dim = 384

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        self.model_name = model_name
        self._m = SentenceTransformer(model_name)
        # get_sentence_embedding_dimension is deprecated in sentence-transformers 6.x
        get_dim = getattr(self._m, "get_embedding_dimension", None) \
            or self._m.get_sentence_embedding_dimension
        self.dim = int(get_dim())

    def encode(self, text: str) -> List[float]:
        return _l2(self._m.encode(text or "").tolist())


_CACHE: Dict[str, object] = {}


def get_embedder(preferred: Optional[str] = None) -> object:
    """Return a cached embedder. `preferred` may name one explicitly.

    Priority: explicit request -> sentence-transformers (if installed) ->
    hashing word (default) -> char-ngram fallback.
    """
    key = preferred or "auto"
    if key in _CACHE:
        return _CACHE[key]

    embedder = None
    if preferred in (None, "auto"):
        try:
            embedder = SentenceTransformerEmbedder()
        except Exception:
            embedder = None
    elif preferred == "sentence-transformers":
        embedder = SentenceTransformerEmbedder()  # let ImportError surface
    elif preferred in ("hashing", "hashing-word"):
        embedder = HashingWordEmbedder()
    else:
        raise ValueError(f"unknown embedder: {preferred}")

    if embedder is None:
        embedder = HashingWordEmbedder()

    _CACHE[key] = embedder
    return embedder


def encode_text(text: str, preferred: Optional[str] = None) -> List[float]:
    return get_embedder(preferred).encode(text)


def pack(vec: Sequence[float]) -> str:
    """Store vectors as a compact space-separated string."""
    return " ".join(f"{v:.6f}" for v in vec)


def unpack(blob: Optional[str]) -> Optional[List[float]]:
    """Read a stored vector. Accepts the current space-separated format and
    the legacy json.dumps([...]) format written before semantic recall, so
    existing rows stay readable and get re-packed by the backfill."""
    if not blob:
        return None
    try:
        return [float(x) for x in blob.split()]
    except (ValueError, AttributeError):
        pass
    try:
        val = json.loads(blob)
        if isinstance(val, list) and val and all(isinstance(x, (int, float)) for x in val):
            return [float(x) for x in val]
    except (ValueError, TypeError):
        pass
    return None
