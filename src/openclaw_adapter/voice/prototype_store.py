"""SQLite store for voice personalization (#82 PR2, design §12).

Owns utterance rows (short-TTL, embedding only — raw audio is never written
anywhere), per-action prototypes, learning tokens (consumed in PR3) and action
stats. Contracts:

- schema versioned via PRAGMA user_version; unknown newer versions refuse to
  operate instead of guessing;
- embeddings carry their model version and are filtered on read — vectors from
  different model versions never meet (design §12.3);
- a corrupt database raises VoiceStoreCorruptError explicitly; it is never
  silently rebuilt as an empty store (design §13.4).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_PROFILE_ID = "default"

UTTERANCE_STATUS_PENDING = "pending"
UTTERANCE_STATUS_CONSUMED = "consumed"

PROTOTYPE_STATUS_ACTIVE = "active"
PROTOTYPE_STATUS_DISABLED = "disabled"
PROTOTYPE_STATUS_ORPHANED = "orphaned"

PROTOTYPE_SOURCE_CLARIFICATION = "clarification"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_utterances (
    utterance_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    transcript TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    language TEXT,
    language_probability REAL,
    embedding BLOB,
    embedding_dim INTEGER,
    embedding_model_version TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_prototypes (
    prototype_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    embedding BLOB NOT NULL,
    embedding_dim INTEGER NOT NULL,
    embedding_model_version TEXT NOT NULL,
    confirmed_count INTEGER NOT NULL DEFAULT 1,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_prototypes_profile_action
ON voice_prototypes(profile_id, action_id, status);

CREATE TABLE IF NOT EXISTS voice_learning_tokens (
    token_hash TEXT PRIMARY KEY,
    utterance_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    candidate_action_ids_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);

CREATE TABLE IF NOT EXISTS voice_action_stats (
    profile_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_success_at REAL,
    PRIMARY KEY (profile_id, action_id)
);
"""


class VoiceStoreError(RuntimeError):
    """Base error for voice store failures."""


class VoiceStoreCorruptError(VoiceStoreError):
    """The database exists but cannot be read/verified. NEVER handled by
    rebuilding an empty store — the operator must decide (design §13.4)."""


class VoiceStoreVersionError(VoiceStoreError):
    """The on-disk schema is newer than this code understands."""


def encode_embedding(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_embedding(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


@dataclass(frozen=True)
class UtteranceRecord:
    utterance_id: str
    profile_id: str
    transcript: str
    duration_ms: int
    language: str | None
    language_probability: float | None
    embedding: tuple[float, ...] | None
    embedding_model_version: str | None
    created_at: float
    expires_at: float
    status: str


@dataclass(frozen=True)
class VoicePrototype:
    prototype_id: str
    profile_id: str
    action_id: str
    embedding: tuple[float, ...]
    embedding_model_version: str
    confirmed_count: int
    rejected_count: int
    source: str
    status: str
    context_tags: tuple[str, ...]
    created_at: float
    updated_at: float
    last_used_at: float


class VoiceStore:
    def __init__(self, path: str, *, now: object = None) -> None:
        if not path:
            raise ValueError("VoiceStore requires a non-empty path")
        self._path = Path(path)
        self._now = now if callable(now) else time.time
        self._ensure_schema()

    # --- connection / schema -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise VoiceStoreVersionError(
                        f"voice store schema v{version} is newer than supported v{SCHEMA_VERSION}"
                    )
                conn.executescript(_SCHEMA)
                if version < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            raise VoiceStoreCorruptError(
                f"voice store corrupt at {self._path}: {exc}"
            ) from exc

    def _execute(self, fn):
        try:
            with self._connect() as conn:
                return fn(conn)
        except sqlite3.DatabaseError as exc:
            raise VoiceStoreCorruptError(
                f"voice store corrupt at {self._path}: {exc}"
            ) from exc

    # --- utterances (short-TTL, design §12.2) --------------------------------
    def save_utterance(
        self,
        *,
        utterance_id: str,
        transcript: str,
        duration_ms: int,
        ttl_seconds: float,
        language: str | None = None,
        language_probability: float | None = None,
        embedding: list[float] | None = None,
        embedding_model_version: str | None = None,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        now = self._now()
        blob = encode_embedding(embedding) if embedding else None
        dim = len(embedding) if embedding else None

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """INSERT OR REPLACE INTO voice_utterances
                   (utterance_id, profile_id, transcript, duration_ms, language,
                    language_probability, embedding, embedding_dim,
                    embedding_model_version, created_at, expires_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    utterance_id, profile_id, transcript, int(duration_ms),
                    language, language_probability, blob, dim,
                    embedding_model_version, now, now + max(1.0, ttl_seconds),
                    UTTERANCE_STATUS_PENDING,
                ),
            )

        self._execute(_op)

    def get_utterance(self, utterance_id: str) -> UtteranceRecord | None:
        """Return the utterance if it exists and has not expired."""
        now = self._now()

        def _op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM voice_utterances WHERE utterance_id = ?",
                (utterance_id,),
            ).fetchone()
            if row is None or row["expires_at"] <= now:
                return None
            embedding = None
            if row["embedding"] is not None and row["embedding_dim"]:
                embedding = tuple(
                    decode_embedding(row["embedding"], row["embedding_dim"])
                )
            return UtteranceRecord(
                utterance_id=row["utterance_id"],
                profile_id=row["profile_id"],
                transcript=row["transcript"],
                duration_ms=row["duration_ms"],
                language=row["language"],
                language_probability=row["language_probability"],
                embedding=embedding,
                embedding_model_version=row["embedding_model_version"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                status=row["status"],
            )

        return self._execute(_op)

    def mark_utterance_consumed(self, utterance_id: str) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE voice_utterances SET status = ? WHERE utterance_id = ?",
                (UTTERANCE_STATUS_CONSUMED, utterance_id),
            )

        self._execute(_op)

    def gc_expired(self) -> int:
        """Delete expired/consumed utterances and expired tokens; returns the
        number of utterance rows removed."""
        now = self._now()

        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                "DELETE FROM voice_utterances WHERE expires_at <= ? OR status = ?",
                (now, UTTERANCE_STATUS_CONSUMED),
            )
            conn.execute(
                "DELETE FROM voice_learning_tokens WHERE expires_at <= ?",
                (now,),
            )
            return cur.rowcount

        return self._execute(_op)

    # --- prototypes (design §7.2/§7.5) ---------------------------------------
    def add_prototype(
        self,
        *,
        prototype_id: str,
        action_id: str,
        embedding: list[float],
        embedding_model_version: str,
        source: str = PROTOTYPE_SOURCE_CLARIFICATION,
        context_tags: tuple[str, ...] = (),
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        if not embedding:
            raise ValueError("prototype requires a non-empty embedding")
        now = self._now()

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """INSERT INTO voice_prototypes
                   (prototype_id, profile_id, action_id, embedding, embedding_dim,
                    embedding_model_version, confirmed_count, rejected_count,
                    source, status, context_json, created_at, updated_at,
                    last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)""",
                (
                    prototype_id, profile_id, action_id,
                    encode_embedding(embedding), len(embedding),
                    embedding_model_version, source, PROTOTYPE_STATUS_ACTIVE,
                    json.dumps(list(context_tags), ensure_ascii=False),
                    now, now, now,
                ),
            )

        self._execute(_op)

    def list_prototypes(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        embedding_model_version: str | None = None,
        action_id: str | None = None,
        status: str | None = PROTOTYPE_STATUS_ACTIVE,
    ) -> tuple[VoicePrototype, ...]:
        """List prototypes. When embedding_model_version is given, rows from
        other model versions are excluded — cross-version vectors must never
        be compared (design §12.3)."""

        def _op(conn: sqlite3.Connection):
            query = "SELECT * FROM voice_prototypes WHERE profile_id = ?"
            params: list[object] = [profile_id]
            if embedding_model_version is not None:
                query += " AND embedding_model_version = ?"
                params.append(embedding_model_version)
            if action_id is not None:
                query += " AND action_id = ?"
                params.append(action_id)
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY updated_at DESC"
            rows = conn.execute(query, params).fetchall()
            out = []
            for row in rows:
                try:
                    tags = tuple(json.loads(row["context_json"]))
                except (ValueError, TypeError):
                    tags = ()
                out.append(
                    VoicePrototype(
                        prototype_id=row["prototype_id"],
                        profile_id=row["profile_id"],
                        action_id=row["action_id"],
                        embedding=tuple(
                            decode_embedding(row["embedding"], row["embedding_dim"])
                        ),
                        embedding_model_version=row["embedding_model_version"],
                        confirmed_count=row["confirmed_count"],
                        rejected_count=row["rejected_count"],
                        source=row["source"],
                        status=row["status"],
                        context_tags=tags,
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        last_used_at=row["last_used_at"],
                    )
                )
            return tuple(out)

        return self._execute(_op)

    def record_confirmation(self, prototype_id: str) -> None:
        now = self._now()

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """UPDATE voice_prototypes
                   SET confirmed_count = confirmed_count + 1,
                       updated_at = ?, last_used_at = ?
                   WHERE prototype_id = ?""",
                (now, now, prototype_id),
            )

        self._execute(_op)

    def record_rejection(self, prototype_id: str, *, disable_after: int = 3) -> None:
        """Negative feedback (design §7.6): bump rejected_count and disable the
        prototype once rejections reach the threshold."""
        now = self._now()

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """UPDATE voice_prototypes
                   SET rejected_count = rejected_count + 1, updated_at = ?
                   WHERE prototype_id = ?""",
                (now, prototype_id),
            )
            conn.execute(
                """UPDATE voice_prototypes SET status = ?, updated_at = ?
                   WHERE prototype_id = ? AND rejected_count >= ?""",
                (PROTOTYPE_STATUS_DISABLED, now, prototype_id, max(1, disable_after)),
            )

        self._execute(_op)

    def mark_action_orphaned(self, action_id: str) -> int:
        """An action permanently removed from the registry: its prototypes are
        kept for audit but must never auto-execute (design §6.2)."""
        now = self._now()

        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                """UPDATE voice_prototypes SET status = ?, updated_at = ?
                   WHERE action_id = ? AND status = ?""",
                (PROTOTYPE_STATUS_ORPHANED, now, action_id, PROTOTYPE_STATUS_ACTIVE),
            )
            return cur.rowcount

        return self._execute(_op)

    # --- learning tokens (design §13.2; consumed by learning.py in PR3) ------
    def create_learning_token(
        self,
        *,
        token_hash: str,
        utterance_id: str,
        candidate_action_ids: tuple[str, ...],
        ttl_seconds: float,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        now = self._now()

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """INSERT OR REPLACE INTO voice_learning_tokens
                   (token_hash, utterance_id, profile_id,
                    candidate_action_ids_json, created_at, expires_at, consumed_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (
                    token_hash, utterance_id, profile_id,
                    json.dumps(list(candidate_action_ids)),
                    now, now + max(1.0, ttl_seconds),
                ),
            )

        self._execute(_op)

    def consume_learning_token(self, token_hash: str) -> dict | None:
        """Atomically consume a token: returns its binding exactly once.

        A second call (duplicate/replayed token), an expired token, or an
        unknown hash all return None — single-use is enforced by the guarded
        UPDATE, not by a read-then-write race."""
        now = self._now()

        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                """UPDATE voice_learning_tokens SET consumed_at = ?
                   WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?""",
                (now, token_hash, now),
            )
            if cur.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM voice_learning_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            try:
                candidates = tuple(json.loads(row["candidate_action_ids_json"]))
            except (ValueError, TypeError):
                candidates = ()
            return {
                "utterance_id": row["utterance_id"],
                "profile_id": row["profile_id"],
                "candidate_action_ids": candidates,
            }

        return self._execute(_op)

    # --- action stats ---------------------------------------------------------
    def record_action_outcome(
        self,
        action_id: str,
        *,
        success: bool,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        now = self._now()

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """INSERT INTO voice_action_stats
                   (profile_id, action_id, success_count, failure_count, last_success_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(profile_id, action_id) DO UPDATE SET
                     success_count = success_count + excluded.success_count,
                     failure_count = failure_count + excluded.failure_count,
                     last_success_at = COALESCE(excluded.last_success_at, last_success_at)""",
                (
                    profile_id, action_id,
                    1 if success else 0, 0 if success else 1,
                    now if success else None,
                ),
            )

        self._execute(_op)

    # --- reset (design §13.3: deletes prototypes, utterances AND stats) -------
    def reset_profile(self, profile_id: str = DEFAULT_PROFILE_ID) -> None:
        def _op(conn: sqlite3.Connection):
            for table in (
                "voice_prototypes",
                "voice_utterances",
                "voice_learning_tokens",
                "voice_action_stats",
            ):
                conn.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))

        self._execute(_op)


def open_voice_store(settings: object) -> VoiceStore | None:
    """Settings-driven constructor. Returns None when disabled (empty path);
    logs and returns None on any failure EXCEPT corruption, which propagates —
    a corrupt store must be reported, not treated as absent (design §13.4)."""
    path = str(getattr(settings, "openclaw_voice_store_path", "") or "").strip()
    if not path:
        return None
    try:
        return VoiceStore(path)
    except VoiceStoreCorruptError:
        raise
    except Exception:  # noqa: BLE001 — e.g. unwritable directory
        logger.exception("voice store unavailable at %s", path)
        return None
