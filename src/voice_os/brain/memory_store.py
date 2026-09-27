"""
Persistent Memory Module for Dynamic Agent

This module provides:
- Long-term memory storage for conversations and facts
- User preference persistence
- Session history management
- Vector-based semantic search for memory retrieval
- SQLite-based persistent storage
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from pathlib import Path
import hashlib

from . import embeddings as _emb


def _embed(text: str) -> Optional[List[float]]:
    """Best-effort vectorisation. Never let embedding failure block a write."""
    try:
        return _emb.encode_text(text)
    except Exception:
        return None


@dataclass
class MemoryEntry:
    """Represents a single memory entry."""
    id: str
    content: str
    memory_type: str  # 'conversation', 'fact', 'preference', 'learning'
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "embedding": self.embedding
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MemoryEntry':
        return cls(
            id=data["id"],
            content=data["content"],
            memory_type=data["memory_type"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
            embedding=data.get("embedding")
        )


#: Repo root. ``memory_store.py`` lives at ``<root>/src/voice_os/brain/``, so the
#: root is four levels up. This is resolved from ``__file__`` rather than the
#: working directory on purpose: agent-os had two divergent ``db_path()``
#: resolvers (one relative to the repo, one relative to its *parent*), and they
#: disagreed whenever a relative ``AGENT_OS_DB`` was set. One resolver, anchored
#: to the package, removes the ambiguity.
_ROOT = Path(__file__).resolve().parents[3]


def _default_db() -> str:
    """Where the brain's SQLite lives.

    ``VOICE_OS_DB`` wins, then ``AGENT_OS_DB`` (so an existing agent-os database
    can be adopted), then ``<repo>/.data/brain.db``. The path is always made
    absolute so nothing downstream can reinterpret a relative one.
    """
    for env in ("VOICE_OS_DB", "AGENT_OS_DB"):
        v = os.getenv(env, "").strip()
        if v:
            return str(Path(v).expanduser().resolve())
    return str(_ROOT / ".data" / "brain.db")


def _connect(db_path) -> sqlite3.Connection:
    """Shared-DB connection: WAL + busy timeout so concurrent writers wait
    instead of raising 'database is locked'. Mirrors memory_store.connect."""
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class PersistentMemory:
    """
    Persistent memory system for the dynamic agent.
    
    Features:
    - SQLite storage for durability
    - Semantic search capabilities (when embeddings available)
    - Conversation history tracking
    - User preference storage
    - Fact accumulation and retrieval
    """
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or _default_db())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()
        self._conversation_cache: Dict[str, List[Dict]] = {}
    
    def _init_database(self):
        """Initialize SQLite database with required tables."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Main memory table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata TEXT,
                    embedding TEXT
                )
            ''')
            
            # User preferences table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            
            # Conversation sessions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    started_at TEXT NOT NULL,
                    last_active TEXT NOT NULL,
                    messages TEXT
                )
            ''')
            
            # Create indexes for faster queries
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memory_type ON memories(memory_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_content ON memories(content)')

            # agent-os shared table: saber run-log events live in the same DB
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    dir TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '',
                    runId TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT ''
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(runId)')

            # agent-os shared table: artifacts survive restarts (same DDL as memory_store)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    type TEXT NOT NULL DEFAULT 'document',
                    content TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type)')

            conn.commit()
    
    def _generate_id(self, content: str) -> str:
        """Generate unique ID for memory entry."""
        timestamp = datetime.now().isoformat()
        return hashlib.sha256(f"{content}{timestamp}".encode()).hexdigest()[:16]
    
    async def add_memory(
        self,
        content: str,
        memory_type: str = "conversation",
        metadata: Optional[Dict[str, Any]] = None
    ) -> MemoryEntry:
        """Add a new memory entry."""
        entry = MemoryEntry(
            id=self._generate_id(content),
            content=content,
            memory_type=memory_type,
            timestamp=datetime.now(),
            metadata=metadata or {},
            embedding=_embed(content),  # vectorised on write for semantic recall
        )
        
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO memories (id, content, memory_type, timestamp, metadata, embedding)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                entry.id,
                entry.content,
                entry.memory_type,
                entry.timestamp.isoformat(),
                json.dumps(entry.metadata),
                _emb.pack(entry.embedding) if entry.embedding else None
            ))
            conn.commit()
        
        return entry
    
    async def get_memories(
        self,
        memory_type: Optional[str] = None,
        limit: int = 10,
        query: Optional[str] = None
    ) -> List[MemoryEntry]:
        """Retrieve memories with optional filtering and search."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if query:
                # Simple keyword search (can be enhanced with full-text search)
                cursor.execute('''
                    SELECT id, content, memory_type, timestamp, metadata, embedding
                    FROM memories
                    WHERE content LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (f"%{query}%", limit))
            elif memory_type:
                cursor.execute('''
                    SELECT id, content, memory_type, timestamp, metadata, embedding
                    FROM memories
                    WHERE memory_type = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (memory_type, limit))
            else:
                cursor.execute('''
                    SELECT id, content, memory_type, timestamp, metadata, embedding
                    FROM memories
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit,))
            
            rows = cursor.fetchall()
        
        entries = []
        for row in rows:
            entry = MemoryEntry(
                id=row[0],
                content=row[1],
                memory_type=row[2],
                timestamp=datetime.fromisoformat(row[3]),
                metadata=json.loads(row[4]) if row[4] else {},
                embedding=_emb.unpack(row[5])
            )
            entries.append(entry)
        
        return entries
    
    async def delete_memory(self, memory_id: str) -> bool:
        """Delete one memory by id. Returns whether a row was actually removed.

        Added in the merge. agent-os could write memories but had no way to take
        one back, which is not acceptable for a store fed by speech: a
        misheard sentence becomes a row, and if it cannot be removed it steers
        every later session. The id is matched exactly rather than by prefix,
        so a truncated id from a spoken transcript cannot delete the wrong note.
        """
        memory_id = (memory_id or "").strip()
        if not memory_id:
            return False
        with _connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cursor.rowcount > 0

    async def search_memories(self, query: str, limit: int = 5) -> List[MemoryEntry]:
        """Hybrid recall: keyword LIKE + vector cosine, fused by RRF.

        Keyword alone misses paraphrases ("how do I stop the car" vs
        "vehicle braking"). Vector alone can surface loosely-related rows.
        Reciprocal Rank Fusion keeps the benefit of both without tuning a
        weight: each list contributes 1/(k+rank), k=60.
        """
        if not (query or "").strip():
            return await self.get_memories(limit=limit)

        overfetch = max(limit * 5, 25)
        # Cap the vector scan so a huge store cannot make every query O(n) on
        # a cold path. 5000 rows is ample for a personal memory.
        vector_cap = 5000
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            # --- Keyword arm ---
            tokens = [t for t in _emb.tokenize(query) if len(t) > 2][:6]
            if tokens:
                where = " OR ".join(["content LIKE ?"] * len(tokens))
                like_args = [f"%{t}%" for t in tokens]
                cursor.execute(
                    f"SELECT id, content, memory_type, timestamp, metadata, embedding "
                    f"FROM memories WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                    (*like_args, overfetch),
                )
            else:
                cursor.execute(
                    "SELECT id, content, memory_type, timestamp, metadata, embedding "
                    "FROM memories ORDER BY timestamp DESC LIMIT ?", (overfetch,))
            rows = cursor.fetchall()

            def _mk(r):
                return MemoryEntry(
                    id=r[0], content=r[1], memory_type=r[2],
                    timestamp=datetime.fromisoformat(r[3]),
                    metadata=json.loads(r[4]) if r[4] else {},
                    embedding=_emb.unpack(r[5]),
                )

            entries = [_mk(r) for r in rows]

            # --- Vector arm scores the WHOLE table, not just keyword hits ---
            # (scoring only the keyword subset meant a pure paraphrase query
            #  with zero LIKE matches returned nothing at all)
            qvec = _embed(query)
            all_rows = cursor.execute(
                "SELECT id, content, memory_type, timestamp, metadata, embedding "
                "FROM memories ORDER BY timestamp DESC LIMIT ?", (vector_cap,)
            ).fetchall()

        by_id = {e.id: e for e in entries}
        for r in all_rows:
            if r[0] not in by_id:
                by_id[r[0]] = _mk(r)

        if not by_id:
            return []

        # Lazy backfill so pre-existing rows get vectors on first semantic search.
        self._backfill_embeddings()
        if qvec:
            with _connect(self.db_path) as conn:
                vrows = conn.execute(
                    "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL"
                ).fetchall()
            scored = []
            for mid, blob in vrows:
                s = _emb.cosine(qvec, _emb.unpack(blob) or [])
                if s > 0 and mid in by_id:
                    scored.append((s, mid))
            scored.sort(reverse=True)
            vector_rank = [by_id[mid] for _, mid in scored[:overfetch]]
        else:
            vector_rank = []

        # RRF fusion.
        K = 60
        scores: Dict[str, float] = {}
        for rank, e in enumerate(entries):
            scores[e.id] = scores.get(e.id, 0.0) + 1.0 / (K + rank + 1)
        for rank, e in enumerate(vector_rank):
            scores[e.id] = scores.get(e.id, 0.0) + 1.0 / (K + rank + 1)

        fused = sorted(by_id.values(), key=lambda e: scores.get(e.id, 0.0), reverse=True)
        return fused[:limit]

    def _fetch_one(self, memory_id: str) -> Optional[MemoryEntry]:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, content, memory_type, timestamp, metadata, embedding "
                "FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return None
        return MemoryEntry(
            id=row[0], content=row[1], memory_type=row[2],
            timestamp=datetime.fromisoformat(row[3]),
            metadata=json.loads(row[4]) if row[4] else {},
            embedding=_emb.unpack(row[5]),
        )

    def _backfill_embeddings(self, batch: int = 200) -> int:
        """Vectorise rows that have no usable vector (NULL, or legacy JSON
        format). Idempotent; re-packs legacy rows into the current format."""
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, content, embedding FROM memories "
                    "WHERE embedding IS NULL OR embedding LIKE '[%' LIMIT ?",
                    (batch,)).fetchall()
                if not rows:
                    return 0
                updates = []
                for mid, content, blob in rows:
                    if blob and _emb.unpack(blob) and not blob.lstrip().startswith("["):
                        continue  # already good
                    vec = _embed(content)
                    if vec:
                        updates.append((_emb.pack(vec), mid))
                if updates:
                    conn.executemany(
                        "UPDATE memories SET embedding = ? WHERE id = ?", updates)
                    conn.commit()
                return len(updates)
        except Exception:
            return 0
    
    async def add_preference(self, key: str, value: Any):
        """Store a user preference."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO preferences (key, value, updated_at)
                VALUES (?, ?, ?)
            ''', (key, json.dumps(value), datetime.now().isoformat()))
            conn.commit()
    
    async def get_preference(self, key: str, default: Any = None) -> Any:
        """Retrieve a user preference."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM preferences WHERE key = ?', (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
            return default
    
    async def get_all_preferences(self) -> Dict[str, Any]:
        """Get all user preferences."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM preferences')
            rows = cursor.fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}
    
    async def start_conversation(self, session_id: str, user_id: Optional[str] = None):
        """Start a new conversation session."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Check if session already exists
            cursor.execute('SELECT session_id FROM conversations WHERE session_id = ?', (session_id,))
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO conversations (session_id, user_id, started_at, last_active, messages)
                    VALUES (?, ?, ?, ?, ?)
                ''', (session_id, user_id, datetime.now().isoformat(), datetime.now().isoformat(), json.dumps([])))
                conn.commit()
            else:
                # Update last_active for existing session
                cursor.execute('''
                    UPDATE conversations SET last_active = ? WHERE session_id = ?
                ''', (datetime.now().isoformat(), session_id))
                conn.commit()
        
        if session_id not in self._conversation_cache:
            self._conversation_cache[session_id] = []
    
    async def add_message_to_conversation(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None
    ):
        """Add a message to an ongoing conversation."""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }
        
        # Update cache
        if session_id not in self._conversation_cache:
            self._conversation_cache[session_id] = []
        self._conversation_cache[session_id].append(message)
        
        # Persist to database
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT messages FROM conversations WHERE session_id = ?', (session_id,))
            row = cursor.fetchone()
            messages = json.loads(row[0]) if row and row[0] else []
            messages.append(message)
            
            cursor.execute('''
                UPDATE conversations
                SET messages = ?, last_active = ?
                WHERE session_id = ?
            ''', (json.dumps(messages), datetime.now().isoformat(), session_id))
            conn.commit()
    
    async def get_conversation_history(self, session_id: str, limit: int = 50) -> List[Dict]:
        """Get conversation history for a session."""
        if session_id in self._conversation_cache:
            return self._conversation_cache[session_id][-limit:]
        
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT messages FROM conversations WHERE session_id = ?', (session_id,))
            row = cursor.fetchone()
            if row and row[0]:
                messages = json.loads(row[0])
                self._conversation_cache[session_id] = messages
                return messages[-limit:]
            return []
    
    async def learn_fact(self, fact: str, context: Optional[str] = None):
        """Store a learned fact for future reference."""
        metadata = {"context": context} if context else {}
        await self.add_memory(
            content=fact,
            memory_type="fact",
            metadata=metadata
        )
    
    async def get_relevant_facts(self, query: str, limit: int = 3) -> List[str]:
        """Retrieve relevant facts based on a query."""
        memories = await self.search_memories(query, limit=limit)
        return [m.content for m in memories if m.memory_type == "fact"]
    
    async def clear_conversation_cache(self, session_id: Optional[str] = None):
        """Clear conversation cache."""
        if session_id:
            self._conversation_cache.pop(session_id, None)
        else:
            self._conversation_cache.clear()
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get memory system statistics."""
        with _connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM memories')
            total_memories = cursor.fetchone()[0]
            
            cursor.execute('SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type')
            by_type = dict(cursor.fetchall())
            
            cursor.execute('SELECT COUNT(*) FROM preferences')
            total_preferences = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM conversations')
            total_conversations = cursor.fetchone()[0]
        
        return {
            "total_memories": total_memories,
            "memories_by_type": by_type,
            "total_preferences": total_preferences,
            "total_conversations": total_conversations,
            "cached_sessions": len(self._conversation_cache)
        }

    def save_artifact(self, artifact: Dict[str, Any]) -> None:
        """Upsert an artifact row (crash-safe: artifacts live in SQLite, not RAM)."""
        import time as _time
        with _connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO artifacts (artifact_id, title, type, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    title=excluded.title, type=excluded.type, content=excluded.content,
                    updated_at=excluded.updated_at
            ''', (artifact["artifact_id"], artifact.get("title", ""),
                  artifact.get("type", "document"), artifact.get("content", ""),
                  artifact.get("created_at", _time.time()), _time.time()))
            conn.commit()

    def load_artifacts(self) -> List[Dict[str, Any]]:
        """Load all artifact rows, oldest first."""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT artifact_id, title, type, content, created_at FROM artifacts ORDER BY created_at ASC"
            ).fetchall()
        return [{"artifact_id": r[0], "title": r[1], "type": r[2], "content": r[3],
                 "created_at": r[4], "sources": []} for r in rows]


# Global memory instance (singleton pattern)
_memory_instance: Optional[PersistentMemory] = None


def get_memory(db_path: Optional[str] = None) -> PersistentMemory:
    """Get or create the global memory instance (agent-os shared DB by default)."""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = PersistentMemory(db_path or _default_db())
    return _memory_instance
