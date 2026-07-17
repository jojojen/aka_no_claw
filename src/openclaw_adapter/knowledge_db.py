"""Knowledge base for the SNS signal classifier (RAG layer).

Stores condensed, grounded knowledge about IPs / products / sets / events /
creators / stores that the SNS classifier looks up to enrich the prompt
when judging tweet relevance.

Two tables:
  - ``knowledge_entries``: one row per canonical entity, holds an LLM-condensed
    300-500 char summary, source URLs, confidence, origin.
  - ``entity_aliases``: many aliases → one canonical name (so "PJSK" / "プロセカ"
    / "Project Sekai" all resolve to "pjsk").

Knowledge is accumulated from three sources:
  1. ``EntityResearcher`` — web search + LLM condensation on unknown entity
  2. Manual user notes via the ``/knowledge add`` Telegram command
  3. (Phase B) Tweet aggregation — out of scope for this round

The DB is shared by sns_monitor_bot's classifier (read) and aka_no_claw's
``EntityResearcher`` / Telegram command (write).
"""

from __future__ import annotations

import array
import json
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from .domain_registry import build_domain_id, get_domain
from .url_canonicalize import canonicalize_url, is_traceable_source, source_domain

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@runtime_checkable
class Embedder(Protocol):
    """A text→vector callable carrying its model identity. Production wraps an
    Ollama multilingual model (bge-m3); tests inject a deterministic fake.

    ``__call__`` MUST be best-effort: return ``None`` on any failure (network,
    timeout, bad response) rather than raising — the KB write must never break
    because embedding was unavailable."""

    model: str
    dim: int

    def __call__(self, text: str) -> list[float] | None: ...


# Process-wide default embedder. The bot wires this ONCE at startup via
# ``set_default_embedder`` so every existing ``KnowledgeDatabase(path)`` call
# site picks it up without threading the dependency through. Left unset →
# embedding is fully disabled (pure-lexical behaviour, the pre-embedding state).
_DEFAULT_EMBEDDER: Embedder | None = None


def set_default_embedder(embedder: Embedder | None) -> None:
    """Install (or clear) the process-wide default embedder. Pass ``None`` to
    disable KB embedding everywhere — the kill switch for rollback."""
    global _DEFAULT_EMBEDDER
    _DEFAULT_EMBEDDER = embedder


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS knowledge_entries (
    entry_id           TEXT PRIMARY KEY,
    entity_canonical   TEXT NOT NULL UNIQUE,
    entity_type        TEXT NOT NULL,
    summary            TEXT NOT NULL,
    source_urls_json   TEXT NOT NULL DEFAULT '[]',
    confidence         REAL NOT NULL DEFAULT 0.5,
    origin             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_referenced_at TEXT
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias              TEXT NOT NULL,
    entity_canonical   TEXT NOT NULL,
    PRIMARY KEY (alias, entity_canonical),
    FOREIGN KEY (entity_canonical) REFERENCES knowledge_entries(entity_canonical) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON entity_aliases(entity_canonical);
CREATE INDEX IF NOT EXISTS idx_aliases_alias_lower ON entity_aliases(alias);

CREATE TABLE IF NOT EXISTS codegen_knowledge (
    knowledge_id  TEXT PRIMARY KEY,
    category      TEXT NOT NULL,
    title         TEXT NOT NULL,
    technique     TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    origin        TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.5,
    times_applied INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codegen_category ON codegen_knowledge(category);

-- Optional semantic index (additive; drop to fully remove). One row per
-- (kind, ref_id): kind='entry' → ref_id is entity_canonical; kind='codegen' →
-- ref_id is knowledge_id. ``vec`` is normalized float32 little-endian bytes.
-- ``model``/``dim`` let a model swap invalidate stale rows without a migration.
CREATE TABLE IF NOT EXISTS embeddings (
    kind       TEXT NOT NULL,
    ref_id     TEXT NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref_id)
);

-- Source registry (issue #9): each distinct canonical_url is stored once and
-- addressed by a stable compact id "S<rowid>". RAG findings cite these ids
-- instead of embedding multi-thousand-char redirect/tracking URLs. AUTOINCREMENT
-- guarantees ids are never reused, so a citation stays valid forever.
-- domain_id references the canonical Domain Registry record (issue #11) by id,
-- collapsing host aliases (twitter.com → dom_xcom) instead of repeating the
-- bare host string. Derived deterministically from the canonical host so the
-- reference stays valid forever; NULL only for legacy rows with no host.
CREATE TABLE IF NOT EXISTS sources (
    source_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url TEXT NOT NULL UNIQUE,
    raw_url       TEXT,
    title         TEXT,
    domain        TEXT,
    domain_id     TEXT,
    fetched_at    TEXT
);
"""

_SOURCE_ID_RE = re.compile(r"^S(\d+)$", re.IGNORECASE)


# Allowed entity_type values. Free-text values are accepted (the writer side
# may invent new types as the system evolves) but the classifier prompt and
# retrieval rendering treat these as the canonical set.
ENTITY_TYPES: tuple[str, ...] = ("ip", "tcg", "product", "set", "creator", "event", "store", "other")
# "tcg" — TCG game system itself (e.g. UNION ARENA, Weiss Schwarz, Pokemon TCG)
#         distinct from "ip" (a content brand) and "product" (a SKU).
ORIGINS: tuple[str, ...] = ("web_research", "manual", "tweet_aggregation", "research_command")

# Minimum cosine similarity for a codegen rule to be pulled in via the semantic
# fallback. Tuned conservatively: the lexical gate deliberately excludes
# unrelated recipes, so the fallback must stay relevant-only.
_CODEGEN_SEMANTIC_MIN_SIM = 0.5


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _vec_to_array(vec: list[float] | None) -> array.array | None:
    """Normalize to unit length and return a float32 array (cosine via dot).
    Returns None for empty/zero/non-finite vectors."""
    if not vec:
        return None
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if not math.isfinite(norm) or norm == 0.0:
        return None
    return array.array("f", [float(x) / norm for x in vec])


def _vec_to_blob(vec: list[float] | None) -> bytes | None:
    arr = _vec_to_array(vec)
    return arr.tobytes() if arr is not None else None


def _dot(a: array.array, b: array.array) -> float:
    return math.fsum(a[i] * b[i] for i in range(len(a)))


def _normalize_canonical(name: str) -> str:
    """Canonical entity names are stored lower-case and stripped. Keeps lookup
    simple (case-insensitive) and avoids duplicate rows for case variants."""
    return (name or "").strip().lower()


def build_entry_id(*, entity_canonical: str, entity_type: str) -> str:
    return sha1(f"{entity_canonical}|{entity_type}".encode("utf-8")).hexdigest()


def _resolve_domain_id(domain: str | None) -> str | None:
    """Canonical Domain Registry id for a source host (issue #11).

    A seeded host (incl. an alias like ``twitter.com``) collapses to its
    canonical record's id (``dom_xcom``); an unseeded host gets a deterministic
    ``dom_*`` id from ``build_domain_id`` so the reference is stable even before
    the domain is seeded. Returns None only when there is no host."""
    if not domain:
        return None
    rec = get_domain(domain)
    return rec.domain_id if rec is not None else build_domain_id(domain)


@dataclass(frozen=True)
class SourceRecord:
    source_id: str          # compact, stable: "S1", "S2", …
    canonical_url: str
    raw_url: str | None = None
    title: str | None = None
    domain: str | None = None
    domain_id: str | None = None  # canonical Domain Registry id (issue #11)
    fetched_at: str | None = None


def is_source_id(token: str) -> bool:
    """True if *token* is a source-registry citation id (``S<n>``)."""
    return bool(_SOURCE_ID_RE.match((token or "").strip()))


# ── Codegen methodology RAG ──────────────────────────────────────────────────
# Abstract, transferable rules about HOW to write code correctly — the kind of
# thing a human reviewer corrects in a weak local model that it wouldn't know on
# its own. NOT entity/data-source facts (those live in the generated tool +
# manifest). Retrieved before each /new codegen and injected into the prompt.
CODEGEN_CATEGORIES: tuple[str, ...] = (
    "data_fetch", "numeric_method", "parsing", "validation", "output_contract", "finance",
)
CODEGEN_ORIGINS: tuple[str, ...] = ("seed", "distilled")


def build_codegen_knowledge_id(*, category: str, title: str) -> str:
    return sha1(f"{category}|{title}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CodegenKnowledge:
    knowledge_id: str
    category: str
    title: str
    technique: str
    keywords: tuple[str, ...] = ()
    origin: str = "seed"
    confidence: float = 0.5
    times_applied: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class KnowledgeEntry:
    entry_id: str
    entity_canonical: str
    entity_type: str
    summary: str
    source_urls: tuple[str, ...] = ()
    confidence: float = 0.5
    origin: str = "web_research"
    created_at: str = ""
    updated_at: str = ""
    last_referenced_at: str | None = None


class KnowledgeDatabase:
    def __init__(self, path: str | Path, embedder: Embedder | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Explicit arg wins; otherwise fall back to the process-wide default
        # (None → embedding disabled, identical to pre-embedding behaviour).
        self._embedder: Embedder | None = embedder if embedder is not None else _DEFAULT_EMBEDDER
        # Auto-bootstrap so callers don't have to remember a separate step —
        # CREATE TABLE IF NOT EXISTS is idempotent and cheap to re-run.
        self.bootstrap()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connect() as conn:
            # Stamp schema version only if currently at 0 (implicit v0 or fresh DB).
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version == 0:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.executescript(_SCHEMA)
            self._migrate_source_domain_id(conn)

    @staticmethod
    def _migrate_source_domain_id(conn: sqlite3.Connection) -> None:
        """Add sources.domain_id to pre-#11 DBs and backfill it from the stored
        domain. CREATE TABLE IF NOT EXISTS won't alter an existing table, so an
        old `sources` keeps its original columns until this runs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
        if "domain_id" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN domain_id TEXT")
        for row in conn.execute(
            "SELECT source_id, domain FROM sources "
            "WHERE domain_id IS NULL AND domain IS NOT NULL"
        ).fetchall():
            domain_id = _resolve_domain_id(row["domain"])
            if domain_id:
                conn.execute(
                    "UPDATE sources SET domain_id = ? WHERE source_id = ?",
                    (domain_id, int(row["source_id"])),
                )

    # ── Entry CRUD ──────────────────────────────────────────────────────────

    def get_entry(self, entity_canonical: str) -> KnowledgeEntry | None:
        canonical = _normalize_canonical(entity_canonical)
        if not canonical:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_entries WHERE entity_canonical = ?",
                (canonical,),
            ).fetchone()
        return _row_to_entry(row) if row else None

    # ── Source registry (issue #9) ──────────────────────────────────────────

    def intern_source(
        self, raw_url: str, *, title: str | None = None, fetched_at: str | None = None
    ) -> str | None:
        """Canonicalize *raw_url*, store it once, and return its stable id
        ``S<n>``. Different redirect/tracking wrappers around the same
        destination canonicalize identically and collapse to one record (D3).
        Returns None when *raw_url* has no usable http(s) URL, or when it is an
        opaque redirect that cannot be traced back to its origin (issue #9
        requires every stored source be expandable to a real article)."""
        if not is_traceable_source(raw_url):
            return None
        canonical = canonicalize_url(raw_url)
        if not canonical:
            return None
        domain = source_domain(canonical) or None
        domain_id = _resolve_domain_id(domain)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT source_id FROM sources WHERE canonical_url = ?", (canonical,)
            ).fetchone()
            if row is not None:
                sid = int(row["source_id"])
                if title:
                    conn.execute(
                        "UPDATE sources SET title = ? "
                        "WHERE source_id = ? AND (title IS NULL OR title = '')",
                        (title, sid),
                    )
                return f"S{sid}"
            cur = conn.execute(
                "INSERT INTO sources (canonical_url, raw_url, title, domain, domain_id, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    canonical,
                    (raw_url or "").strip() or None,
                    title or None,
                    domain,
                    domain_id,
                    fetched_at or _utc_now_iso(),
                ),
            )
            return f"S{int(cur.lastrowid)}"

    def _intern_source_refs(self, source_urls: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize an entry's source refs to stable registry ids (issue #9 D4).

        Already-interned ``S<n>`` ids pass through *only when they resolve* to a
        real registry row (e.g. EntityResearcher interns at the producer); raw
        URLs are interned to their id; opaque / non-traceable URLs and dangling
        ids (matching the shape but with no ``sources`` row) are dropped — a
        stored citation must resolve back to a real article."""
        refs: list[str] = []
        for ref in source_urls:
            ref = (ref or "").strip()
            if not ref:
                continue
            if is_source_id(ref):
                # Shape alone is not traceability: drop an id with no registry
                # row so we never store a dangling, unresolvable citation.
                if self.get_source(ref) is None:
                    logger.warning(
                        "upsert_entry: dropping dangling source id %r (no sources row)",
                        ref,
                    )
                    continue
                if ref not in refs:
                    refs.append(ref)
                continue
            sid = self.intern_source(ref)
            if sid and sid not in refs:
                refs.append(sid)
        return tuple(refs)

    def get_source(self, source_id: str) -> SourceRecord | None:
        """Look up a source by its ``S<n>`` id. Returns None for an unknown or
        malformed id."""
        match = _SOURCE_ID_RE.match((source_id or "").strip())
        if not match:
            return None
        rowid = int(match.group(1))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE source_id = ?", (rowid,)
            ).fetchone()
        if row is None:
            return None
        return SourceRecord(
            source_id=f"S{int(row['source_id'])}",
            canonical_url=str(row["canonical_url"]),
            raw_url=row["raw_url"],
            title=row["title"],
            domain=row["domain"],
            domain_id=row["domain_id"],
            fetched_at=row["fetched_at"],
        )

    def upsert_entry(
        self,
        *,
        entity_canonical: str,
        entity_type: str,
        summary: str,
        source_urls: tuple[str, ...] = (),
        confidence: float = 0.5,
        origin: str = "web_research",
        aliases: tuple[str, ...] = (),
    ) -> KnowledgeEntry:
        """Insert or update an entry, then register all aliases. Confidence
        rule: higher confidence wins. Same-confidence write overwrites
        summary (caller intent — e.g. re-running web research with fresh data
        keeps the latest)."""
        canonical = _normalize_canonical(entity_canonical)
        if not canonical:
            raise ValueError("entity_canonical cannot be empty")
        if origin not in ORIGINS:
            logger.warning("upsert_entry: unknown origin=%r (allowed: %s)", origin, ORIGINS)
        # Force every producer's sources through the registry (issue #9 D4): the
        # entry stores stable S<n> ids, never raw redirect/tracking URLs. Done
        # before the write transaction so each intern uses its own connection.
        source_refs = self._intern_source_refs(source_urls)
        now = _utc_now_iso()
        entry_id = build_entry_id(entity_canonical=canonical, entity_type=entity_type)

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT confidence, created_at FROM knowledge_entries "
                "WHERE entity_canonical = ?",
                (canonical,),
            ).fetchone()
            if existing is not None and float(existing["confidence"]) > float(confidence):
                # Higher-confidence existing entry wins — do not overwrite.
                logger.info(
                    "upsert_entry skip: existing confidence=%.2f > incoming %.2f for canonical=%s",
                    float(existing["confidence"]), float(confidence), canonical,
                )
            else:
                created_at = existing["created_at"] if existing else now
                conn.execute(
                    """
                    INSERT INTO knowledge_entries (
                        entry_id, entity_canonical, entity_type, summary,
                        source_urls_json, confidence, origin, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_canonical) DO UPDATE SET
                        entity_type = excluded.entity_type,
                        summary = excluded.summary,
                        source_urls_json = excluded.source_urls_json,
                        confidence = excluded.confidence,
                        origin = excluded.origin,
                        updated_at = excluded.updated_at
                    """,
                    (
                        entry_id, canonical, entity_type, summary,
                        json.dumps(list(source_refs), ensure_ascii=False),
                        float(confidence), origin, created_at, now,
                    ),
                )

            # Register aliases (idempotent; canonical itself is registered too
            # so substring scans hit it).
            self._add_aliases_inside_conn(conn, canonical, (canonical,) + tuple(aliases))

        # Reread for return.
        loaded = self.get_entry(canonical)
        assert loaded is not None, "upsert_entry expected to read back its write"
        self._reindex_entry(canonical)  # best-effort; never raises
        return loaded

    def add_alias(self, alias: str, entity_canonical: str) -> bool:
        """Idempotently register an alias for an existing entry. Returns False
        if the canonical entity doesn't exist."""
        canonical = _normalize_canonical(entity_canonical)
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM knowledge_entries WHERE entity_canonical = ?",
                (canonical,),
            ).fetchone()
            if exists is None:
                return False
            self._add_aliases_inside_conn(conn, canonical, (alias,))
        # Index text includes aliases, so an alias change must re-embed.
        self._reindex_entry(canonical)  # best-effort; never raises
        return True

    def _add_aliases_inside_conn(
        self, conn: sqlite3.Connection, canonical: str, aliases: tuple[str, ...],
    ) -> None:
        for raw in aliases:
            normalised = (raw or "").strip()
            if not normalised:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_aliases (alias, entity_canonical) VALUES (?, ?)",
                    (normalised, canonical),
                )
            except sqlite3.IntegrityError:
                pass  # duplicate ok

    # ── Alias / lookup ──────────────────────────────────────────────────────

    def lookup_canonical(self, alias: str) -> str | None:
        """Resolve any alias (case-insensitive) to its canonical name. Tries
        exact match first, then case-folded match."""
        if not alias:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT entity_canonical FROM entity_aliases WHERE alias = ? LIMIT 1",
                (alias.strip(),),
            ).fetchone()
            if row is not None:
                return str(row["entity_canonical"])
            row = conn.execute(
                "SELECT entity_canonical FROM entity_aliases "
                "WHERE lower(alias) = ? LIMIT 1",
                (alias.strip().lower(),),
            ).fetchone()
        return str(row["entity_canonical"]) if row else None

    def all_aliases(self) -> list[tuple[str, str]]:
        """Return every (alias, canonical) pair. Used by the entity extractor's
        substring scanner — for current volumes (~hundreds of aliases) this is
        cheap and accurate."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT alias, entity_canonical FROM entity_aliases"
            ).fetchall()
        return [(str(r["alias"]), str(r["entity_canonical"])) for r in rows]

    def mark_referenced(self, entity_canonical: str) -> None:
        canonical = _normalize_canonical(entity_canonical)
        if not canonical:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE knowledge_entries SET last_referenced_at = ? "
                "WHERE entity_canonical = ?",
                (_utc_now_iso(), canonical),
            )

    def recent_entries(self, limit: int = 20) -> list[KnowledgeEntry]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_entries ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def entries_since(self, since_iso: str) -> list[KnowledgeEntry]:
        """Return entries whose created_at >= since_iso, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_entries WHERE created_at >= ? ORDER BY created_at DESC",
                (since_iso,),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def delete_entry(self, entry_id: str) -> bool:
        """Delete a knowledge entry by entry_id. Returns True if a row was deleted."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT entity_canonical FROM knowledge_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            cursor = conn.execute(
                "DELETE FROM knowledge_entries WHERE entry_id = ?", (entry_id,)
            )
            deleted = cursor.rowcount > 0
        if deleted and row is not None:
            self._delete_embedding("entry", str(row["entity_canonical"]))
        return deleted

    def delete_codegen(self, knowledge_id: str) -> bool:
        """Delete a codegen knowledge row by knowledge_id. Returns True if deleted."""
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM codegen_knowledge WHERE knowledge_id = ?", (knowledge_id,)
            )
            deleted = cursor.rowcount > 0
        if deleted:
            self._delete_embedding("codegen", knowledge_id)
        return deleted

    # ── Embedding index (best-effort; all methods swallow failure) ──────────

    def _reindex_entry(self, entity_canonical: str) -> None:
        if self._embedder is None:
            return
        entry = self.get_entry(entity_canonical)
        if entry is None:
            return
        aliases = [a for a, c in self.all_aliases() if c == entry.entity_canonical]
        text = self._entry_index_text(entry, aliases)
        self._store_embedding("entry", entry.entity_canonical, text)

    def _reindex_codegen(self, knowledge_id: str) -> None:
        if self._embedder is None:
            return
        row = self._get_codegen_knowledge(knowledge_id)
        if row is None:
            return
        text = self._codegen_index_text(row)
        self._store_embedding("codegen", knowledge_id, text)

    @staticmethod
    def _entry_index_text(entry: KnowledgeEntry, aliases: list[str]) -> str:
        return f"{entry.entity_canonical} | {' ; '.join(aliases)} | {entry.summary}"

    @staticmethod
    def _codegen_index_text(row: CodegenKnowledge) -> str:
        return f"{row.title} | {row.technique} | {' ; '.join(row.keywords)}"

    def _store_embedding(self, kind: str, ref_id: str, text: str) -> None:
        emb = self._embedder
        if emb is None:
            return
        try:
            vec = emb(text)
        except Exception:  # embedder should return None, but double-guard.
            logger.warning("embedding failed for %s/%s", kind, ref_id, exc_info=True)
            return
        blob = _vec_to_blob(vec)
        if blob is None:
            logger.warning("embedding skipped (empty/invalid) for %s/%s", kind, ref_id)
            return
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings "
                    "(kind, ref_id, model, dim, vec, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, ref_id, emb.model, emb.dim, blob, _utc_now_iso()),
                )
        except sqlite3.Error:
            logger.warning("embedding store failed for %s/%s", kind, ref_id, exc_info=True)

    def _delete_embedding(self, kind: str, ref_id: str) -> None:
        try:
            with self.connect() as conn:
                conn.execute(
                    "DELETE FROM embeddings WHERE kind = ? AND ref_id = ?", (kind, ref_id)
                )
        except sqlite3.Error:
            logger.warning("embedding delete failed for %s/%s", kind, ref_id, exc_info=True)

    def search_semantic(self, kind: str, query: str, k: int = 5) -> list[tuple[str, float]]:
        """Cosine top-k over stored vectors for ``kind``. Returns
        ``[(ref_id, score), ...]`` best-first. Empty when embedding is disabled,
        the query can't be embedded, or no compatible vectors exist (e.g. after a
        model swap, until backfill re-runs)."""
        emb = self._embedder
        if emb is None:
            return []
        try:
            qvec = emb(query)
        except Exception:
            logger.warning("query embedding failed", exc_info=True)
            return []
        qarr = _vec_to_array(qvec)
        if qarr is None:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ref_id, vec FROM embeddings WHERE kind = ? AND model = ? AND dim = ?",
                (kind, emb.model, emb.dim),
            ).fetchall()
        scored: list[tuple[str, float]] = []
        for r in rows:
            v = array.array("f")
            v.frombytes(r["vec"])
            if len(v) != len(qarr):
                continue
            scored.append((str(r["ref_id"]), _dot(qarr, v)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: max(0, int(k))]

    def append_observation(
        self,
        *,
        entity_alias_or_canonical: str,
        observed_at: str,
        rationale: str,
        suggested_action: str,
        tweet_url: str,
        deadline: str | None = None,
    ) -> bool:
        """Append a dated bullet to an existing entity's summary under the
        ``最近觀察`` marker. Caller (typically sns_monitor_bot's silenced flow)
        passes an alias OR canonical name.

        Behavior:
          - Resolve canonical via lookup_canonical(); fall back to treating
            the input as canonical directly.
          - If the entity has no entry yet, log + return False (no stub —
            EntityResearcher owns entity creation).
          - Otherwise append the bullet, FIFO-trim observations to keep
            total summary ≤ ~2000 chars. The pre-marker head (condensed
            canonical knowledge) is preserved verbatim.
          - origin is NEVER changed (pedigree invariant). Only updates
            summary, updated_at, last_referenced_at.
        """
        raw_input = (entity_alias_or_canonical or "").strip()
        if not raw_input:
            return False
        canonical = self.lookup_canonical(raw_input) or _normalize_canonical(raw_input)
        if not canonical:
            return False
        entry = self.get_entry(canonical)
        if entry is None:
            logger.warning(
                "knowledge_db: append_observation skipped, unknown entity=%s (input=%s)",
                canonical, raw_input,
            )
            return False
        if is_insufficient_entry(entry):
            # The entity is a 資料不足 no-data stub (internal negative cache). Don't
            # accrete observations onto it — that would launder junk into a
            # user-visible 'knowledge' entry. Drop the observation silently.
            logger.info(
                "knowledge_db: append_observation skipped — entity=%s is a no-data stub (資料不足)",
                canonical,
            )
            return False

        bullet = _build_observation_bullet(
            observed_at=observed_at,
            rationale=rationale,
            suggested_action=suggested_action,
            tweet_url=tweet_url,
            deadline=deadline,
        )
        new_summary = _append_observation_to_summary(entry.summary or "", bullet)
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE knowledge_entries "
                "SET summary = ?, updated_at = ?, last_referenced_at = ? "
                "WHERE entity_canonical = ?",
                (new_summary, now, now, canonical),
            )
        return True

    # ── Codegen knowledge CRUD ──────────────────────────────────────────────

    def upsert_codegen_knowledge(
        self,
        *,
        category: str,
        title: str,
        technique: str,
        keywords: tuple[str, ...] = (),
        origin: str = "seed",
        confidence: float = 0.5,
    ) -> CodegenKnowledge:
        """Insert or update one abstract coding rule. Keyed on (category|title).
        Higher confidence wins; equal-or-higher overwrites the technique text."""
        category = (category or "").strip() or "other"
        title = (title or "").strip()
        technique = (technique or "").strip()
        if not title or not technique:
            raise ValueError("codegen knowledge requires title and technique")
        if origin not in CODEGEN_ORIGINS:
            logger.warning("upsert_codegen_knowledge: unknown origin=%r", origin)
        now = _utc_now_iso()
        knowledge_id = build_codegen_knowledge_id(category=category, title=title)
        keywords_json = json.dumps([k.strip() for k in keywords if k.strip()], ensure_ascii=False)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT confidence, created_at FROM codegen_knowledge WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
            if existing is not None and float(existing["confidence"]) > float(confidence):
                logger.info(
                    "upsert_codegen_knowledge skip: existing confidence higher for %s", title,
                )
            else:
                created_at = existing["created_at"] if existing else now
                conn.execute(
                    """
                    INSERT INTO codegen_knowledge (
                        knowledge_id, category, title, technique, keywords_json,
                        origin, confidence, times_applied, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(knowledge_id) DO UPDATE SET
                        category = excluded.category,
                        technique = excluded.technique,
                        keywords_json = excluded.keywords_json,
                        origin = excluded.origin,
                        confidence = excluded.confidence,
                        updated_at = excluded.updated_at
                    """,
                    (
                        knowledge_id, category, title, technique, keywords_json,
                        origin, float(confidence), created_at, now,
                    ),
                )
        loaded = self._get_codegen_knowledge(knowledge_id)
        assert loaded is not None
        self._reindex_codegen(knowledge_id)  # best-effort; never raises
        return loaded

    def _get_codegen_knowledge(self, knowledge_id: str) -> CodegenKnowledge | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM codegen_knowledge WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        return _row_to_codegen(row) if row else None

    def all_codegen_knowledge(self) -> list[CodegenKnowledge]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM codegen_knowledge ORDER BY confidence DESC, updated_at DESC"
            ).fetchall()
        return [_row_to_codegen(r) for r in rows]

    def retrieve_codegen_knowledge(self, request_text: str, k: int = 6) -> list[CodegenKnowledge]:
        """Return up to ``k`` rules most relevant to ``request_text``.

        Two classes of rules:
        - Always-on (keywords contain "*"): generic best practices, eligible on
          every request, ranked by confidence.
        - Topical (no "*"): API recipes / domain methods. Eligible ONLY when the
          request actually matches a keyword/category/title token — otherwise
          excluded entirely. Injecting an unrelated recipe (e.g. weather into a
          stock request) makes small models copy it verbatim, contaminating the
          generated tool with data nobody asked for."""
        rows = self.all_codegen_knowledge()
        if not rows:
            return []
        request_lc = (request_text or "").lower()
        scored: list[tuple[float, CodegenKnowledge]] = []
        for row in rows:
            always_on = "*" in row.keywords
            match = 0.0
            for kw in row.keywords:
                if kw and kw != "*" and kw.lower() in request_lc:
                    match += 2.0
            if row.category and row.category.lower() in request_lc:
                match += 1.0
            for token in row.title.lower().split():
                if len(token) >= 3 and token in request_lc:
                    match += 0.5
            if not always_on and match <= 0.0:
                continue
            scored.append((match + row.confidence, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        k = max(1, k)
        selected = [row for _, row in scored[:k]]
        if self._embedder is None or len(selected) >= k:
            return selected
        # Lexical under-filled → top up with semantic matches, but only ones
        # genuinely related (cosine ≥ threshold) so we don't reintroduce the
        # unrelated-recipe contamination the strict lexical gate guards against.
        chosen = {r.knowledge_id for r in selected}
        by_id = {r.knowledge_id: r for r in rows}
        for ref_id, score in self.search_semantic("codegen", request_text, k):
            if len(selected) >= k:
                break
            if score < _CODEGEN_SEMANTIC_MIN_SIM or ref_id in chosen:
                continue
            row = by_id.get(ref_id)
            if row is not None:
                selected.append(row)
                chosen.add(ref_id)
        return selected

    def mark_codegen_applied(self, knowledge_ids: tuple[str, ...]) -> None:
        if not knowledge_ids:
            return
        now = _utc_now_iso()
        with self.connect() as conn:
            for kid in knowledge_ids:
                conn.execute(
                    "UPDATE codegen_knowledge SET times_applied = times_applied + 1, "
                    "updated_at = ? WHERE knowledge_id = ?",
                    (now, kid),
                )

    def seed_codegen_knowledge(self) -> int:
        """Idempotently insert the baseline abstract rules. Existing rows (by
        id) are left untouched unless seed confidence is higher. Returns count
        of seed rules processed."""
        # Retire superseded seed rules so they stop being retrieved on every DB
        # (seeding runs at each startup, so a renamed rule's old row must be
        # explicitly deleted or it lingers forever with stale guidance).
        for category, title in DEPRECATED_CODEGEN_SEED:
            stale_id = build_codegen_knowledge_id(category=category, title=title)
            with self.connect() as conn:
                conn.execute(
                    "DELETE FROM codegen_knowledge WHERE knowledge_id = ? AND origin = 'seed'",
                    (stale_id,),
                )
        for spec in CODEGEN_SEED:
            self.upsert_codegen_knowledge(
                category=spec["category"],
                title=spec["title"],
                technique=spec["technique"],
                keywords=tuple(spec.get("keywords", ())),
                origin="seed",
                confidence=float(spec.get("confidence", 0.8)),
            )
        return len(CODEGEN_SEED)


def _row_to_codegen(row: sqlite3.Row) -> CodegenKnowledge:
    try:
        kws = json.loads(row["keywords_json"] or "[]")
        if not isinstance(kws, list):
            kws = []
    except (TypeError, ValueError, json.JSONDecodeError):
        kws = []
    return CodegenKnowledge(
        knowledge_id=row["knowledge_id"],
        category=row["category"],
        title=row["title"],
        technique=row["technique"],
        keywords=tuple(str(k) for k in kws),
        origin=row["origin"],
        confidence=float(row["confidence"]),
        times_applied=int(row["times_applied"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def format_codegen_knowledge_block(rows: list[CodegenKnowledge], *, max_chars: int = 4800) -> str:
    """Render retrieved rules into the codegen prompt's ``<代碼開發方法論>`` block."""
    if not rows:
        return "(無)"
    lines: list[str] = []
    used = 0
    for row in rows:
        line = f"- [{row.category}] {row.title}：{row.technique.strip()}"
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


# Baseline abstract coding rules. All are entity-agnostic and transferable.
# (category, title) of seed rules that have been renamed/superseded. Deleted at
# seed time so their stale text stops being retrieved on already-populated DBs.
DEPRECATED_CODEGEN_SEED: tuple[tuple[str, str], ...] = (
    ("output_contract", "可變參數放腳本頂端，不要散落在程式碼中間"),
)


CODEGEN_SEED: tuple[dict, ...] = (
    {
        "category": "concurrency",
        "title": "終態狀態必須在所有可恢復副作用落盤後才對讀者可見",
        "technique": (
            "背景工作要把狀態從 running 切到 completed/failed/cancelled 時，不能先更新可被 "
            "poll/read 看到的記憶體旗標、再慢慢寫入結果檔或事件日誌；讀者會觀察到『已完成但 "
            "沒有結果』的短暫矛盾。用一個鎖保護終態 compare-and-set：先判定取消/完成的勝者，"
            "先持久化 recovery snapshot 與可重播結果，再發布終態旗標。測試要刻意在每個邊界讀取，"
            "斷言任何終態回應都已帶齊其恢復資料。"
        ),
        "keywords": ["*", "concurrency", "terminal", "atomic", "durable", "event", "poll", "race"],
        "confidence": 0.95,
    },
    {
        "category": "performance",
        "title": "不要在正式請求熱路徑前重複執行同等成本的生成式健康探測",
        "technique": (
            "當正式 API／模型請求本身已能判定服務是否可用，而且呼叫端已有明確的錯誤處理或 "
            "fallback，便不要在每次正式請求前再送一次生成式 probe；這會把一次使用者操作變成 "
            "兩次昂貴呼叫，並讓 probe 的 timeout、重試與 token 下限直接疊加到延遲。應直接建立 "
            "client，讓正式請求成為權威健康檢查；若 UI 或監控需要預先顯示健康狀態，改用啟動時 "
            "或有 TTL 的背景探測。另以不記錄內容的分段耗時紀錄 build、generate 與 total，才能 "
            "區分 client 建立、上游生成及 fallback 的成本。"
        ),
        "keywords": ["*", "performance", "latency", "health probe", "hot path", "fallback", "timing"],
        "confidence": 0.95,
    },
    {
        "category": "performance",
        "title": "只需要首個合格結果時，使用增量結果流而非等待完整蒐集窗",
        "technique": (
            "處理探索、掃描或查詢時，先量測每個階段；若使用者操作只需要第一個符合條件的"
            "結果，應消費能逐筆產生結果的 iterator/stream，命中後立刻停止，而不是等待"
            "完整蒐集 timeout。保留完整 timeout 作為『沒有合格結果』的錯誤路徑，並以"
            "回歸測試保證首個合格結果不會繼續等待後續資料。這能縮短正常路徑，同時保留"
            "網路波動時的發現能力。"
        ),
        "keywords": ["*", "performance", "latency", "timeout", "iterator", "stream", "discovery"],
        "confidence": 0.95,
    },
    {
        "category": "operations",
        "title": "程序健康檢查要辨識實際 worker，不可把 supervisor wrapper 算成重複實例",
        "technique": (
            "以程序表驗證服務唯一性時，同一個服務常同時存在 supervisor、shell wrapper 與"
            "真正 worker；若只 grep 模組名稱，wrapper 的命令列也會內嵌完整啟動命令，造成"
            "正常單一 worker 被誤判為兩個實例，後續成功通知或部署閘門便會被錯誤阻擋。"
            "正解：健康檢查必須依程序角色過濾 supervisor/wrapper，並用包含 wrapper 與 child"
            "的真實程序表樣本做回歸測試；不要把裸字串命中數直接等同 worker 數。"
        ),
        "keywords": ["*", "process", "health-check", "supervisor", "worker", "restart"],
        "confidence": 0.95,
    },
    {
        "category": "architecture",
        "title": "相容入口應導向唯一的正式 UI，避免重啟後回到過期頁面",
        "technique": (
            "同一個服務若同時保留相容 HTTP 入口與新的正式前端，重啟腳本可能讓使用者"
            "再次從舊 port 或書籤進入，表面上像是『新版 UI 壞掉』。正解：相容入口只保留"
            "必要 API，對頁面 GET 回傳明確的暫時轉址到正式 UI，並依請求 Host 組出目標網址，"
            "以支援 localhost、LAN 與 IPv6。不要複製兩套 HTML 作為長期 fallback。驗證方式："
            "測試轉址狀態碼與 Location、確認舊 API 仍可用，並在重啟後以瀏覽器從舊入口實測"
            "最終頁面與 console error。"
        ),
        "keywords": ["*", "ui", "redirect", "compatibility", "routing", "restart", "web"],
        "confidence": 0.95,
    },
    {
        "category": "architecture",
        "title": "同一功能有多入口時，後端路由必須逐一稽核，UI 預設不會自動傳播到別的行程",
        "technique": (
            "一個面向使用者的功能常有多個入口：不同 UI（React 主控台、Telegram、輕量 chat "
            "頁）與不同行程（bridge、poller）。在其中一個 UI 選好的預設（例如前端 state 預設"
            "『雲端池』）只在該請求裡帶著送出，並不會綁定其他行程裡的處理器——sibling 行程的"
            "handler 各自有自己的預設，很容易停留在舊值（如 Telegram 自動翻譯 handler 寫死打"
            "本地 qwen3:14b），造成『同一功能，換個入口就走不同模型/後端』的分歧。症狀是使用"
            "者說『這功能應該預設用 X 才對，怎麼跑去用 Y』。正解：把後端/模型選擇抽成一個只吃"
            "settings 的共用進入點（不依賴特定 UI 的 request 物件），讓每個入口都路由到同一條"
            "選擇鏈（含 fallback）；修 bug 時逐一列出所有入口並確認各自實際打哪個後端，別只改"
            "你手上那條路徑。驗證方式：對『沒帶明確後端』的請求斷言它走的是共用預設，並實測"
            "每個入口的最終 provider 一致。"
        ),
        "keywords": ["*", "architecture", "entrypoint", "routing", "default", "backend", "llm", "consistency", "multi-process", "ui"],
        "confidence": 0.9,
    },
    {
        "category": "architecture",
        "title": "一次性網路送出必須自帶重試，別假設外層有迴圈保護",
        "technique": (
            "對外部服務（Telegram、HTTP API）的請求會間歇性失敗——TLS 交握被切斷"
            "（SSL: UNEXPECTED_EOF_WHILE_READING）、RemoteDisconnected、連線重置、逾時，"
            "常見成因是 VPN 出口切換或網路瞬斷。長輪詢類呼叫（getUpdates）因為外層 poll"
            "迴圈會重試而看似穩定，但一次性的送出（sendMessage、背景回覆、通知）沒有任何"
            "重試，單一瞬斷就把使用者的回覆永遠吞掉——症狀是『最近一次成功、前幾次失敗』"
            "這種間歇性掉訊。正解：在傳輸層本身對暫時性錯誤（URLError 及非 URLError 的原始"
            "串流錯誤 HTTPException/OSError，含 SSLError）做有上限的退避重試，讓所有呼叫端"
            "一致受惠；但對確定性的失敗（HTTPError 這種真正的 HTTP 狀態碼、應用層 ok:false）"
            "絕不重試。觀察到的失敗在交握階段、請求主體尚未送出，故重試安全；即使極少數"
            "情況在讀取階段失敗導致重送，一則罕見的重複訊息也遠優於整筆回覆遺失。驗證方式："
            "mock 傳輸層第一次拋暫時性錯誤、第二次成功，斷言有重試且最終送達；並斷言"
            "HTTPError 不被重試。"
        ),
        "keywords": ["*", "network", "retry", "backoff", "transient", "ssl", "tls", "timeout", "idempotent", "transport"],
        "confidence": 0.95,
    },
    {
        "category": "validation",
        "title": "新增模組或文件時，以全套品質閘門驗證登錄與靜態匯出",
        "technique": (
            "抽取模組、加入文件或重整匯出表面時，局部行為測試不足以發現交付物沒有被"
            "靜態分析或文件索引接納。變更完成前應同時跑受影響檔案的 linter 與文件健康"
            "檢查；對刻意 re-export 的名稱使用明確 alias 或 __all__，避免品質閘門把相容"
            "性匯出誤判為未使用。把這些 gate 納入該抽取 slice 的測試，讓新增資產在首次"
            "提交就被索引，且避免 CI 與本機驗證出現落差。"
        ),
        "keywords": ["*", "refactor", "lint", "documentation", "index", "re-export", "ci", "quality gate"],
        "confidence": 0.95,
    },
    {
        "category": "architecture",
        "title": "LLM 規劃器不可用推測的環境狀態否決使用者要求的動作",
        "technique": (
            "LLM 規劃器/路由器的 prompt 若含歷史執行紀錄（tool ledger、對話摘要），"
            "模型會把「最後一筆相關紀錄」當成現在的環境狀態，並以此拒絕使用者要求的"
            "動作——但非同步任務（goal loop、背景 job）的結果往往在下一次規劃之後才落帳"
            "（如：音樂明明在播放卻回答『沒有音樂在播放』而不派發停止）。正解不是把"
            "各領域的即時狀態塞進共用 prompt（那是領域特定硬編碼、每加一個裝置就要"
            "加一行），而是通用規則：使用者要求執行動作時一律以要求為準直接派發工具，"
            "工具本身是狀態的唯一真相來源，執行後回報實際結果；冪等動作（停止、靜音）"
            "重複執行本來就無害。驗證方式：重建失敗當下的 ledger+對話，對真實模型做"
            "有/無規則行的 A/B 探測，斷言決策翻轉。"
        ),
        "keywords": ["*", "llm", "planner", "router", "prompt", "state", "ledger", "stale", "idempotent", "dispatch"],
        "confidence": 0.95,
    },
    {
        "category": "validation",
        "title": "對定長 padding 的特徵做 pooling 前先裁掉 padding 區段",
        "technique": (
            "凡是模型把變長輸入 pad 到固定視窗（如音訊 pad 到 30 秒、序列 pad 到定長）再輸出"
            "frame/token 級特徵時，mean/max pooling 必須只涵蓋真實內容對應的 frames，"
            "不可對整個 padded 視窗取平均——否則短輸入的向量會被 padding 主導、全部塌縮到同方向"
            "（不同輸入 cosine 相似度可高到 0.98，喪失鑑別力）。驗證方式：對同一內容的不同"
            "rendition 與完全不同內容各算 pairwise 相似度，同類必須嚴格高於異類。"
        ),
        "keywords": ["*", "embedding", "pooling", "padding", "cosine", "audio", "feature"],
        "confidence": 0.95,
    },
    {
        "category": "validation",
        "title": "累積計數的成熟門檻必須配套「合併/強化」路徑並用端到端劇本驗證",
        "technique": (
            "若功能要求某記錄累積 N 次事件才解鎖（如 confirmed_count >= 3 才允許快路徑），"
            "寫入端就必須有把新事件歸併到既有記錄的邏輯（相似度合併、upsert、外鍵對應），"
            "不能每次都 insert 新列——否則計數永遠停在 1，門檻在現實中不可達，而單元測試"
            "各自綠燈完全看不出來。驗收要用完整劇本跑真實路徑：重複同一輸入 N 次後，"
            "斷言只有一筆記錄且計數等於 N、且解鎖行為真的發生。"
        ),
        "keywords": ["*", "counter", "threshold", "merge", "upsert", "maturity", "e2e"],
        "confidence": 0.95,
    },
    {
        "category": "architecture",
        "title": "抽取 collaborator 時保留既有 facade 的可替換 seam",
        "technique": (
            "把 orchestration 從既有 facade 抽到 collaborator 時，不要讓新物件直接繞過原本"
            "可替換的方法去呼叫網路、模型或副作用。collaborator 應經由明確 deps protocol 回呼"
            "facade 的同名 seam，facade 再薄委派到 collaborator；如此既有 consumer、instance "
            "monkeypatch、deterministic fake 與相容測試仍攔得到呼叫。為每個抽出的邊界保留一個"
            "測試：替換 facade seam 後，collaborator 的實際路徑必須使用替身，絕不碰真實外部服務。"
        ),
        "keywords": ["*", "refactor", "facade", "collaborator", "dependency injection", "seam", "monkeypatch", "compatibility"],
        "confidence": 0.95,
    },
    {
        "category": "validation",
        "title": "串流協定損毀必須可觀測且終止成功路徑",
        "technique": (
            "解析逐行或分幀串流時，JSON/欄位結構/版本驗證失敗都要轉成明確的錯誤狀態，"
            "不能略過壞資料後繼續把後續成功事件交給使用者。發出錯誤後要停止或取消該串流，"
            "並以 producer/consumer 測試確認損毀資料不會被誤報成空結果或成功。"
        ),
        "keywords": ["*", "stream", "protocol", "ndjson", "corrupt", "validation"],
        "confidence": 0.95,
    },
    {
        "category": "architecture",
        "title": "長時任務不要只綁在單一長連線，改用可輪詢的持久化任務",
        "technique": (
            "行動裝置上的長連線（SSE/NDJSON/WebSocket）在螢幕鎖定或切到背景時會被作業系統"
            "強制中斷，heartbeat 也擋不住。任何可能跑數十秒以上的工作，不要把結果只綁在那條"
            "連線上：建立一個持久化、可輪詢的任務（job），先把 job_id 回傳給前端；背景 worker"
            "無論客戶端是否還在都要把最終結果寫進可查詢的儲存區；前端在連線中斷或重新載入時"
            "改用 job_id 輪詢/重連取回結果，而不是把中斷當成失敗。以「發出 job → 中途關閉連線"
            "→ 仍能輪詢到最終結果」的 producer/consumer 測試驗證。"
        ),
        "keywords": ["*", "streaming", "sse", "ndjson", "job", "poll", "reconnect", "mobile", "background"],
        "confidence": 0.9,
    },
    {
        "category": "architecture",
        "title": "昂貴操作的去重要靠結構化 key 在執行邊界強制，不要只靠提示或評審模型",
        "technique": (
            "當一個多步驟迴圈可能重跑昂貴且不可逆或高成本的操作（例如一次網路研究、一次付費 "
            "API 呼叫），不要只在 planner 提示或 LLM 滿意度評審裡「請不要重複」——那會漂。"
            "改用結構化的 operation key（把指令名 + 正規化輸入 collapse 成穩定字串）當識別，"
            "在真正呼叫 handler 的 dispatcher／executor 邊界維護一份 run-scoped 的 memo："
            "key 已存在就直接回傳先前產物（artifact），不再執行。memo 要跨 draft 與每次 replan "
            "共用，且把觸發升級前就已跑過的那次操作（連同結果）當 seed 塞進去，才能保證「每個"
            "正規化操作最多執行一次」是可測試的確定性保證，而非機率。以「第一輪 partial → 進入"
            "重規劃 → 最終仍有答案，且該操作 handler 呼叫次數嚴格等於 1」的 mocked E2E 驗證。"
        ),
        "keywords": ["*", "dedup", "idempotent", "operation key", "planner", "executor", "replan", "goal loop", "memo"],
        "confidence": 0.9,
    },
    {
        "category": "validation",
        "title": "重啟後驗證相依性與對外服務真的可用",
        "technique": (
            "不要把「程序已被啟動」當成服務已恢復。先做能載入平台原生相依性的最小探針；"
            "探針失敗時，僅依既有 lockfile 重建本機相依性，再重新驗證。啟動後還要在固定"
            "port 或健康端點等待成功訊號，逾時時明確寫入可查的錯誤與日誌位置，避免讓使用者"
            "只看到表面成功卻沒有可連的服務。"
        ),
        "keywords": ["*", "restart", "health check", "dependency", "lockfile", "port"],
        "confidence": 0.95,
    },
    {
        "category": "numeric_method",
        "title": "年化報酬要分簡單與複利",
        "technique": (
            "年化報酬有「簡單年化」與「複利年化」兩種，結果差異大；"
            "未滿一年（partial-year）的期間報酬絕不可直接當成全年報酬，"
            "且年化的指數/倍率方向弄反是常見錯誤（未滿一年時年化會放大期間報酬，不是縮小）。"
            "輸出時明講用哪一種算法與期間天數，並同時給出期間原始報酬。"
            "公式不寫死於此，實作前以參考頁 Annualisation 一節的定義為準：\n"
            "參考: https://en.wikipedia.org/wiki/Rate_of_return"
        ),
        "keywords": ["年化", "annualized", "報酬", "return", "cagr", "複利", "ytd", "今年以來"],
        "confidence": 0.9,
    },
    {
        "category": "finance",
        "title": "報酬要分價格報酬與含息總報酬",
        "technique": (
            "股票/ETF 報酬分兩種：價格報酬只看收盤價變化；含息總報酬要把期間配息加回"
            "（或用還原股價 adjusted close）。算總報酬時明講有沒有含息、用了哪個欄位。"
        ),
        "keywords": ["配息", "股息", "dividend", "adjclose", "總報酬", "total return", "etf", "0050"],
        "confidence": 0.9,
    },
    {
        "category": "data_fetch",
        "title": "天氣資料用免費免 API key 的端點",
        "technique": (
            "天氣查詢 → 使用 wttr.in（直接接受城市名，無需座標，免 API key）：\n"
            "  import urllib.parse\n"
            "  city_enc = urllib.parse.quote(city_name, safe='')\n"
            "  url = f'https://wttr.in/{city_enc}?format=j1'\n"
            "  # 帶 User-Agent header 避免 403\n"
            "  req = urllib.request.Request(url, headers={'User-Agent': 'WeatherBot/1.0'})\n"
            "  回傳結構：current_condition[0].temp_C（現在氣溫），\n"
            "    weather[0].maxtempC / weather[0].mintempC（今日最高/最低），\n"
            "    weather[0].hourly 各時段 chanceofrain → max() 取最高降雨機率，\n"
            "    current_condition[0].weatherDesc[0].value（天氣描述文字），\n"
            "    current_condition[0].humidity（濕度%）。\n"
            "  ⚠️ 勿使用 Nominatim（403 Forbidden）＋open-meteo 的二段式流程。\n"
            "絕對不要用假 placeholder token 呼叫需要授權的 API——生成工具沒有合法 API key。"
        ),
        "keywords": ["天氣", "氣溫", "濕度", "weather", "temperature", "humidity",
                     "wttr", "open-meteo", "氣象", "降雨", "forecast", "下雨", "晴", "預報"],
        "confidence": 0.95,
    },
    {
        "category": "finance",
        "title": "Yahoo Finance chart API 查股價與報酬",
        "technique": (
            "Yahoo Finance chart API（台股/美股日線都可用，務必帶 User-Agent header）：\n"
            "  GET https://query1.finance.yahoo.com/v8/finance/chart/<symbol>"
            "?period1=<unix_ts>&period2=<unix_ts>&interval=1d&events=div\n"
            "  台股代號加 .TW（如 0050.TW），美股直接用代號（如 TSLA）。\n"
            "  回傳 JSON 結構（請用這些確切路徑取值，不要自創 key 如 'data'）：\n"
            "    r = json['chart']['result'][0]\n"
            "    時間戳: r['timestamp']  # list[int]，秒\n"
            "    收盤價(價格報酬用): r['indicators']['quote'][0]['close']  # list，可能含 None\n"
            "    還原收盤價(含息總報酬用): r['indicators']['adjclose'][0]['adjclose']  # list\n"
            "    配息: r['events']['dividends']  # dict，值為 {amount, date}\n"
            "  close/adjclose 陣列**一定含 None**（停牌日），取起點/終點前必須過濾：\n"
            "    prices = [p for p in raw_prices if p is not None]\n"
            "    start_price, end_price = prices[0], prices[-1]\n"
            "  千萬不要直接用 raw_prices[0] 或 raw_prices[-1]，那可能是 None。\n"
            "  抓取寫法（照抄；務必帶 User-Agent，裸 urlopen 會被擋 HTTP 429/401）：\n"
            "    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})\n"
            "    data = json.load(urllib.request.urlopen(req, timeout=20))\n"
            "  常見致命錯誤：\n"
            "    - datetime.date 物件沒有 .timestamp()——要用 datetime(..., tzinfo=timezone.utc).timestamp()。\n"
            "    - r['timestamp'] 是整數秒清單，不是資料列，絕不可對它的元素再取欄位。\n"
            "    - period1/period2 是 unix 秒；period1 不可設 0（會抓到上市以來全部）。\n"
            "    - 『某時點的值』當天若無交易資料，取該時點『之前』最近一筆收盤（carry-forward），"
            "不可拿之後的資料代替；因此查詢範圍要比所需期間起點再多抓前幾個交易日。\n"
            "    - 不要用 yfinance/pandas 套件，直接 urllib 打上面端點，簡單又不需安裝。\n"
            "  報酬率/YTD/年化等金融公式與期間慣例不寫死於此，實作前以參考頁的定義為準：\n"
            "  參考: https://en.wikipedia.org/wiki/Rate_of_return\n"
            "  參考: https://en.wikipedia.org/wiki/Year-to-date"
        ),
        "keywords": ["股", "股票", "股價", "報酬", "etf", "0050", "ticker", "stock",
                     "price", "yahoo", "台股", "美股", "收盤", "漲", "跌", "return",
                     "ytd", "今年以來", "匯率", "指數"],
        "confidence": 0.95,
    },
    {
        "category": "data_fetch",
        "title": "即時快照不能替代時間聚合——資料粒度語意合約",
        "technique": (
            "資料來源常有多種時間粒度：即時快照（current）vs 時間聚合（daily/weekly 的 max/min/avg）。"
            "兩者語意不同、不可互換：\n"
            "  即時快照：某一時刻的值（現在溫度、現在股價、現在 CPU 使用率）。\n"
            "  時間聚合：一段時間內的統計（今日最高溫、日高低點、日成交量）。\n"
            "常見錯誤：需要「今日最高/最低」時，用即時快照同時填入兩欄，"
            "導致輸出「26°C – 26°C」或 high=low 這類明顯錯誤。\n"
            "正確做法：輸出需要範圍（min–max）或聚合量時，找資料來源的 daily/aggregate 層；"
            "不確定時先把頂層 key 印出來探索結構。\n"
            "此原則適用於所有資料來源：天氣 API、金融 API、監控系統、資料庫查詢。"
        ),
        "keywords": ["*", "current", "daily", "snapshot", "aggregate", "max", "min",
                     "即時", "聚合", "粒度", "最高", "最低", "high", "low", "range"],
        "confidence": 0.92,
    },
    {
        "category": "data_fetch",
        "title": "API 欄位名稱先探索再使用",
        "technique": (
            "使用第三方 REST API 時，不要靠猜測或記憶決定欄位名稱。正確做法：\n"
            "1. 先用最小參數發一次探索請求，把回傳的 JSON key 印出來，確認實際欄位名稱後再寫完整邏輯。\n"
            "2. 若 API 回傳錯誤（如「invalid field name」），仔細閱讀錯誤訊息——通常會直接告訴你問題欄位。\n"
            "3. 聚合統計欄位（日最高/最低/累計）常有 _max / _min / _sum / _mean 後綴，和即時欄位不同；"
            "找不到欄位時試加這些後綴。\n"
            "4. 在正式查詢前加 assert 或 if key not in data: raise 提前捕捉欄位錯誤。"
        ),
        "keywords": ["*", "api", "field", "key", "欄位", "json", "explore", "探索", "suffix",
                     "_max", "_min", "error", "invalid"],
        "confidence": 0.9,
    },
    {
        "category": "data_fetch",
        "title": "外部 API 回傳先驗結構再索引",
        "technique": (
            "呼叫外部 API 後，先檢查 HTTP 狀態、payload 是否為預期型別、list 是否非空、"
            "first/last 元素是否為 None，全部通過才索引取值。不要假設欄位一定存在。"
        ),
        "keywords": ["*", "api", "http", "json", "request", "fetch", "endpoint"],
        "confidence": 0.85,
    },
    {
        "category": "parsing",
        "title": "解析 JSON 用 get 加預設值",
        "technique": (
            "解析巢狀 JSON 一律用 dict.get(key, default) 與防呆，逐層確認不是 None 再往下取；"
            "陣列取值前先確認長度。缺欄位時 raise 帶上實際 payload 片段的明確錯誤"
            "（如 raise KeyError(f'missing close in {list(d)}')），"
            "比裸 KeyError 更好定位，但仍要讓例外拋出，不可吞掉。"
        ),
        "keywords": ["*", "json", "parse", "dict", "解析", "欄位"],
        "confidence": 0.8,
    },
    {
        "category": "validation",
        "title": "可選 metadata 只做快篩，硬限制留在資料邊界",
        "technique": (
            "外部 API 標示 optional 的 size、duration、MIME 等 metadata 不可當成必填；"
            "欄位存在時先驗型別與上限，已知無效值應在網路或磁碟 I/O 前拒絕。"
            "欄位缺少時則繼續走真正的資料邊界，使用 bounded read、解碼後大小與時長檢查"
            "作為不可繞過的硬限制。如此同時保留相容性、early rejection 與資源安全。"
        ),
        "keywords": ["*", "optional", "metadata", "bounded read", "size limit", "mime", "validation"],
        "confidence": 0.95,
    },
    {
        "category": "validation",
        "title": "不得生成假 API token / placeholder 憑證",
        "technique": (
            "生成的程式碼絕對不能用假的、佔位用的 API key 或 token（如 \"1234567890abcdef\"、"
            "\"YOUR_API_KEY\"、\"mock-token\"、\"test123\" 等）——執行時必然失敗。\n"
            "正確做法：\n"
            "(1) 優先找完全不需要 API key 的公開端點（如 wttr.in、open-meteo、維基百科 API）。\n"
            "(2) 若 API 確實需要 key，在 ===ANSWER=== 裡告知使用者「此 API 需要申請 key，"
            "請前往 xxx 申請後填入」，不要自己填假的。\n"
            "(3) 不要從 os.environ 讀取任何 TOKEN/KEY/SECRET/PASSWORD 名稱的變數——"
            "執行環境已刻意清除這些變數。\n"
            "核心原則：寧願回答「無法取得此資料（需要授權）」也不要用假 token 假裝嘗試。"
        ),
        "keywords": ["*", "token", "api key", "placeholder", "credential", "secret",
                     "憑證", "假", "mock", "authorization", "授權"],
        "confidence": 0.95,
    },
    {
        "category": "data_fetch",
        "title": "時間範圍必須動態計算，不可寫死常數",
        "technique": (
            "查詢用的時間範圍（unix timestamp、日期字串）必須用 datetime 依『今天日期』"
            "動態算出，絕不可寫死數字常數（如 period1=1672531200）——"
            "寫死的常數是訓練資料裡的過期範例值，會抓到完全錯誤期間的資料。"
            "期間語意（今年以來、最近一週、本月）要先用今天日期換算成正確的起訖時點再查。"
            "為了取基準值而多抓緩衝資料時，絕不可直接拿陣列第一筆當期初——"
            "要用回傳的 timestamp 對齊：期初取『期間起點前最近一個資料點』"
            "（掃 timestamp 找最後一個 < 期間起點的索引）。"
            "緩衝資料只能用來取這個期初基準值；任何統計或掃描"
            "（極值、最高/最低、聚合）都只能在期間內的資料點上進行——"
            "先用 timestamp 把序列切出『期間內』子序列再掃，"
            "否則期間外的緩衝點會混進答案（例如把去年底的高點當成今年的峰值）。"
            "結束時間要含到今天：period2 取現在時刻（int(time.time())）而非今日零點，"
            "否則會切掉當天資料。"
        ),
        "keywords": ["*", "timestamp", "日期", "期間", "period", "range", "動態", "unix"],
        "confidence": 0.95,
    },
    {
        "category": "numeric_method",
        "title": "極值掃描要同步記錄達成極值的資料點",
        "technique": (
            "回報『序列中的極值及其發生位置』（最大跌幅、最長連續、最高/最低點的日期）時，"
            "必須在掃描迴圈裡、每次極值被更新的當下，同步記下構成該極值的那組資料點"
            "（索引/日期/數值）存成變數，最後與答案一起輸出。"
            "絕不可掃描完才用全域 max()/min() 事後重建那組資料點，"
            "也不可輸出『掃描結束時』的 running 狀態——"
            "那是最後一刻的狀態，常常不是構成答案的那組（順序條件會被破壞，"
            "例如印出的『高點』出現在『低點』之後，那組就不可能構成答案）。"
            "通用範本（best 與 best 的構成點要和 running 狀態分開存）：\n"
            "  best = None; best_points = None; running 狀態 = 初始值\n"
            "  for i, v in enumerate(series):\n"
            "      更新 running 狀態（如目前高點 cur_hi, cur_hi_i = v, i）\n"
            "      metric = 由 running 狀態與 v 算出的衡量值\n"
            "      if best is None or metric 更優: best = metric; "
            "best_points = (cur_hi_i, i)  # ←在這一刻記下\n"
            "  輸出時只用 best_points 取日期/數值，絕不用 cur_hi（那是掃描結束值）。\n"
            "輸出前自我驗算：用 best_points 那組資料點按同一公式重算一次，"
            "結果必須等於答案的數值，不相等就 raise 並附上兩個值。"
            "驗算的兩邊都必須是程式當場算出的值——絕不可拿寫死的『預期數字常數』來比對，"
            "你猜的預期值幾乎一定是錯的，會把正確結果打成失敗。"
        ),
        "keywords": ["回撤", "drawdown", "極值", "峰", "谷", "高點", "低點", "最大", "最小",
                     "連續", "argmax", "argmin", "max", "min", "peak", "trough"],
        "confidence": 0.92,
    },
    {
        "category": "numeric_method",
        "title": "百分比尺度只能轉換一次",
        "technique": (
            "計算過程一律用小數比例（0.0–1.0）表示比率，"
            "只在『最終輸出格式化』那一刻乘 100 加 % 號：f\"{ratio*100:.2f}%\"，"
            "且整條路徑只能乘一次 100。常見致命錯誤：\n"
            "  - 值還是比例就直接套 f\"{ratio:.2f}%\"（0.1084 被印成 0.11%，差 100 倍）；\n"
            "  - 中途先乘 100 存進變數，輸出時又乘一次（差 10000 倍）。\n"
            "輸出前量級自檢：格式化字串裡的數字必須等於 ratio*100（容差 1e-6），"
            "不等就 raise 並附上兩個值。"
        ),
        "keywords": ["%", "百分比", "百分點", "比率", "比例", "percent", "ratio", "％"],
        "confidence": 0.92,
    },
    {
        "category": "numeric_method",
        "title": "多序列運算必須先按 key 對齊取交集",
        "technique": (
            "對兩條以上獨立來源的序列做逐點運算（相關係數、價差、比值、共變異）時，"
            "絕不可假設它們等長、同起點、同間隔——不同來源的時間軸幾乎一定不一致"
            "（不同市場的交易日曆、不同 API 的回補規則、缺值位置不同）。"
            "正確流程：\n"
            "  1. 各序列先建成 dict：key=對齊鍵（日期字串/timestamp），value=數值，"
            "跳過 None/缺值；\n"
            "  2. keys = sorted(set(d1) & set(d2)) 取共同 key 的交集並排序；\n"
            "  3. 只在交集上重建等長序列，再做逐點運算；\n"
            "  4. 把交集後的樣本數一起輸出，並檢查樣本數不可離任一原序列長度過遠"
            "（交集過小代表對齊鍵格式不一致，例如一邊是日期一邊是 timestamp）。\n"
            "若運算定義在『變化率』上（如日報酬），先對齊再算變化率——"
            "順序顛倒會把跨缺口的變化率混進來。"
            "直接 zip 兩條原始序列是典型錯誤：長度不同會靜默截斷，日期錯位會算出看似合理"
            "其實完全錯誤的結果。"
        ),
        "keywords": ["相關", "correlation", "對齊", "align", "交集", "兩", "比較", "vs",
                     "之間", "序列", "spread", "價差", "比值", "共同", "intersect"],
        "confidence": 0.92,
    },
    {
        "category": "validation",
        "title": "算完做合理性檢查",
        "technique": (
            "輸出數值前做 sanity check：量級是否合理、正負號是否符合預期、有沒有極端離群值。"
            "異常時在輸出標註可疑，不要靜默回傳一個看似正常其實錯誤的數字。\n"
            "退化值自檢（這些幾乎一定代表抓錯資料，直接 raise 而不是輸出）：\n"
            "  - 變化率/報酬率恰好為 0；\n"
            "  - 最高值 == 最低值（high=low 的範圍）；\n"
            "  - 時間序列只有 1 筆資料、或全部值完全相同。\n"
            "raise 時附上實際取得的原始值，讓修復機制有線索。"
        ),
        "keywords": ["*", "sanity", "檢查", "validate", "合理"],
        "confidence": 0.85,
    },
    {
        "category": "validation",
        "title": "資料源紀律：只取需求要的資料，不替代、不自行拒答、不加料",
        "technique": (
            "(1) 只抓取並輸出需求明確要求的資料；找不到指定資料源時不可用『相近但不同』"
            "的資料替代（例如要 A 股票卻回 B 指數、要今晚航班卻回歷史均價）。\n"
            "(2) 需求可行性已由上游確認過——不要在腳本裡自行放棄，"
            "印出『此功能需要 API key』『請自行查詢』之類的佔位文字當答案；"
            "真的取不到資料就讓例外拋出，交給修復機制。\n"
            "(3) 不要加料：需求沒要求的衍生計算（年化、含息、與其他標的比較、預測）一律不算不印，"
            "只回答被問到的值。算得多不是加分，是答非所問。"
        ),
        "keywords": ["*", "資料源", "替代", "拒答", "加料", "scope", "需求"],
        "confidence": 0.9,
    },
    {
        "category": "error_handling",
        "title": "禁止 try/except 吞錯誤——讓例外直接拋出",
        "technique": (
            "不要用 try/except 包住主流程把例外轉成模糊訊息或自訂錯誤輸出——"
            "外層修復機制依賴 stderr 的完整 traceback 定位問題，吞掉就修不了。\n"
            "防呆檢查失敗時直接 raise，並把實際收到的內容帶進訊息，例如：\n"
            "  raise ValueError(f'unexpected payload: {str(data)[:200]}')\n"
            "絕不要 raise ValueError('Invalid response from API') 這種沒有上下文的空泛訊息，"
            "也不要把錯誤印進 ===ANSWER=== 區塊。"
        ),
        "keywords": ["*", "try", "except", "traceback", "錯誤", "exception"],
        "confidence": 0.9,
    },
    {
        "category": "output_contract",
        "title": "輸出乾淨的資料值，不傾倒原始結構",
        "technique": (
            "答案區塊只放人類可讀的最終值：\n"
            "  - 從巢狀 dict/list 取出純量再輸出，不要把整個 dict、list 或 JSON 原樣 print；\n"
            "  - 不要把 URL 當成答案描述印出來；\n"
            "  - API 回的數字常是字串，比較或計算前先轉 int/float；\n"
            "  - 不要在腳本裡寫死表情符號/排版模板，排版由外層統一處理。"
        ),
        "keywords": ["*", "輸出", "dict", "list", "url", "字串", "排版"],
        "confidence": 0.85,
    },
    {
        "category": "output_contract",
        "title": "可變參數放頂端 DEFAULTS 並讓輸出標的跟著參數走",
        "technique": (
            "若請求涉及特定城市、股票代碼、日期、金額等可能隨新需求改變的值，"
            "把它們收進腳本頂端的 DEFAULTS dict（緊接 import 之後），並讀 params.json 覆寫：\n"
            "  DEFAULTS = {'city': '台北'}\n"
            "  params = dict(DEFAULTS)\n"
            "  import os, json\n"
            "  if os.path.exists('params.json'): params.update(json.load(open('params.json', encoding='utf-8')))\n"
            "這樣同一支工具換個 params.json 就能服務不同城市/股票——這正是工具重用的機制，"
            "不需要為每個城市另生成新工具。\n"
            "關鍵：不只計算要用 params[...]，連『輸出文字裡的標的名稱』也必須用 params[...] 帶入。\n"
            "  ❌ DEFAULTS={'city':'Paris'} 卻 print(f\"巴黎氣溫{t}°C\")："
            "重用查倫敦時數據變了、標籤還是巴黎，答非所問。\n"
            "  ✅ print(f\"{params['city']}氣溫{t}°C\")：標的與資料同源，重用永遠一致。\n"
            "注意字面陷阱：DEFAULTS 存的英文 'Paris' 與輸出寫死的中文『巴黎』不會被字串比對抓到，"
            "唯一可靠做法就是輸出一律 echo params[...]，不要自己另寫標的字面值。"
        ),
        "keywords": ["*", "參數", "常數", "城市", "city", "ticker", "頂端", "constant", "硬編碼",
                     "重用", "reuse", "標的", "params", "答非所問", "輸出"],
        "confidence": 0.92,
    },
    {
        "category": "output_contract",
        "title": "答案夾在 ANSWER 標記並附計算依據",
        "technique": (
            "最終答案必須 print 在 ===ANSWER=== 與 ===END=== 之間，方便程式擷取；"
            "數值結果要附一句『怎麼算的』（資料源、期間、公式），用 f-string 帶入實際值。\n"
            "計算類答案同時印出關鍵原始值（如期初/期末收盤價與日期），讓人能對帳驗證；"
            "百分比要 ×100 再印（印 52.82% 而不是 0.5282）。"
        ),
        "keywords": ["*", "output", "answer", "stdout", "輸出", "格式"],
        "confidence": 0.85,
    },
    {
        "category": "finance",
        "title": "yfinance 損益表查詢（財報≠價格報酬）",
        "technique": (
            "查詢個股年度財報（損益表）用 ticker.income_stmt 或 ticker.financials，"
            "不是 ticker.history()——history() 是股價，財報要用損益表。\n"
            "income_stmt 是 DataFrame，columns 是財年結束日期（如 2025-12-31），"
            "index 是科目名（Total Revenue, Net Income, Diluted EPS, Gross Profit, Operating Income）。\n"
            "取最新年度：col = ticker.income_stmt.columns[0]，然後 df[col] 取各科目值。\n"
            "毛利率/營益率/淨利率等比率的定義不寫死於此，實作前以參考頁為準：\n"
            "參考: https://en.wikipedia.org/wiki/Profit_margin\n"
            "YoY 比較：cols = ticker.income_stmt.columns[:2]，分別取 col[0]（最新）與 col[1]（前一年）。\n"
            "金額單位是美元（不是十億），輸出時要除以 1e9 並標明 B。\n"
            "常見錯誤：(1)把 history() 的收盤價當作財報數字；(2)忘記除以 1e9；"
            "(3)取錯年度（columns 按時間倒序，columns[0] 是最新年度）。"
        ),
        "keywords": ["財報", "損益表", "income", "statement", "營收", "淨利", "eps",
                     "revenue", "yfinance", "年報", "financial"],
        "confidence": 0.95,
    },
    {
        "category": "finance",
        "title": "yfinance EPS 欄位不除以 1e9",
        "technique": (
            "income_stmt 中 Total Revenue、Net Income、Gross Profit、Operating Income 單位是美元（需除以 1e9 轉成十億）。"
            "但 Basic EPS 和 Diluted EPS 單位已是「美元/股」，絕對不要再除以 1e9——否則會得到接近 0 的錯誤值。"
            "讀取 EPS 時直接輸出 float 值，不做任何縮放。"
            "常見錯誤：用同一個 /1e9 縮放邏輯套用在所有科目，把 EPS 也壓縮成接近 0。"
        ),
        "keywords": ["eps", "每股盈餘", "diluted", "basic", "1e9", "billion", "縮放", "income_stmt"],
        "confidence": 0.95,
    },
    {
        "category": "validation",
        "title": "函數內生成器引用外層變數要確保已定義",
        "technique": (
            "在函數內部的生成器表達式（generator expression / list comprehension）中引用的變數，"
            "必須在函數被呼叫之前已經被定義在函數的本地 scope 或作為參數傳入。"
            "不能假設外層模組 scope 的變數在函數執行時一定可見——尤其是在定義函數之前尚未賦值的變數。"
            "正確做法：把需要的外層變數作為函數參數傳入，或在函數內部最頂端明確賦值。"
            "常見錯誤：在函數外定義 start_date，在函數內生成器裡用到它，但呼叫函數時 start_date 尚未賦值。"
        ),
        "keywords": ["*", "nameerror", "scope", "generator", "lambda", "作用域", "undefined", "closure"],
        "confidence": 0.9,
    },
    {
        "category": "numeric_method",
        "title": "Black-Scholes 選擇權定價與標準常態 CDF 實作",
        "technique": (
            "Black-Scholes 歐式選擇權定價：公式不寫死於此，實作前以參考頁的定義為準；"
            "call 與 put 公式不可混用，參數單位要一致（T 用『年』、r/σ/q 用年化值，"
            "天數要先除以 365）。\n"
            "標準常態累積分佈 N(x) 不需 scipy，用 Python stdlib 精確實作：\n"
            "  N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))\n"
            "參考: https://en.wikipedia.org/wiki/Black%E2%80%93Scholes_model"
        ),
        "keywords": [
            "black-scholes", "option", "選擇權", "bs", "call", "put",
            "歐式", "定價", "volatility", "波動率", "履約", "strike",
        ],
        "confidence": 0.95,
    },
    {
        "category": "orchestration",
        "title": "多步/重試流程要把已完成的子結果帶著走，不要重取",
        "technique": (
            "多步驟流程（抓網頁、呼叫 API、LLM 推理等昂貴步驟）在部分失敗後重試/重規劃時，"
            "常見錯誤是整組作廢從頭重跑。正確做法有三：（1）已成功取得的子結果要以「預先綁定的變數/參數」"
            "帶進重試回合，讓新計畫直接引用而不是重新取得——不要依賴查詢字串完全相同的快取，"
            "重規劃後的步驟描述幾乎必然不同，exact-match 快取會 miss；"
            "（2）每次嘗試（成功與失敗都要）記到一份決策端看得到的執行帳（ledger），"
            "下一輪決策才能選擇重用或改道，而不是盲目重做；"
            "（3）後面步驟失敗不可連帶丟棄前面已成功的輸出——唯讀取得型管線沒有 rollback 的理由。"
        ),
        "keywords": [
            "retry", "重試", "replan", "重規劃", "重工", "workflow", "多步",
            "pipeline", "orchestration", "快取", "cache", "partial", "部分失敗",
            "loop", "agent", "重跑",
        ],
        "confidence": 0.9,
    },
    {
        "category": "validation",
        "title": "多個結構相同的分支之一壞掉，先對照其餘同類分支再改邏輯",
        "technique": (
            "當程式裡有多個結構高度相似的函式/分支（例如同一組事件處理常式、"
            "同一類端點、同一種格式判斷）之一出錯，而其餘的行為正常時，優先假設"
            "是「這一個少接了某個已存在的參數/回呼/呼叫」，而不是這個分支需要"
            "全新的深層邏輯。做法：把壞掉的分支跟正常的兄弟分支逐項對照，找出"
            "兄弟分支有、這個分支沒有的那一小段接線（例如少傳一個 callback、"
            "少呼叫一次某個 setter/wrapper），照抄補上即可；不要一開始就重寫"
            "整段邏輯，那通常是捨近求遠。"
        ),
        "keywords": [
            "*", "debug", "除錯", "sibling", "對照", "重複邏輯", "callback",
            "wiring", "少接線", "分支", "handler",
        ],
        "confidence": 0.85,
    },
    {
        "category": "validation",
        "title": "新增回傳欄位要兩端對得上，不能只看產生端有沒有送出",
        "technique": (
            "幫既有的回傳結構（dict/物件/API 回應）新增一個欄位時，光確認"
            "『產生端有把值放進去送出』是不夠的——消費端如果沒有在型別定義/"
            "解析邏輯裡宣告或讀取這個欄位，資料會被靜默丟掉，看起來像是沒送，"
            "實際上是送了沒人讀。修完之後務必反向確認：在消費端的型別/解析"
            "程式裡搜尋這個新欄位名稱，確認真的有變數接住並被使用，而不是只"
            "驗證產生端輸出裡有這個 key。一個沒被任何地方讀取的欄位/型別定義"
            "（dead field）本身就是這種缺口的警訊，看到就該追查是不是漏了消費端。"
        ),
        "keywords": [
            "*", "schema", "欄位", "field", "契約", "contract", "靜默丟失",
            "silently dropped", "消費端", "producer", "consumer", "dead code",
        ],
        "confidence": 0.85,
    },
    {
        "category": "validation",
        "title": "分類器誤判前，先確認正確答案有沒有被列進選項",
        "technique": (
            "當 LLM 分類器/路由器把某種輸入判成錯誤類別時，不要急著假設是"
            "提示詞措辭不夠好或模型能力不足，先檢查決策空間本身：正確的那個"
            "類別，有沒有被當成一個合法選項明確列在提示詞裡？如果沒有，模型"
            "不是「選錯」，而是根本沒有選項可選，只能落到語意最接近的鄰居"
            "類別去——尤其當那個鄰居類別的說明文字剛好也用了同樣的關鍵字"
            "（例如兩種類別的範例都出現同一個詞），更會誘導它選錯。修法是"
            "把遺漏的類別明確加成一個獨立選項，並在說明文字裡把它跟鄰居類別"
            "的界線講清楚（用『這個不是那個』的對比句），而不是換句話修飾"
            "既有選項的措辭。另外要留意：同一個決策在系統裡若有多條路徑"
            "（例如串流與非串流、或不同入口各自呼叫一次分類器），修正決策"
            "空間之後每條路徑都要各自接上正確的分派邏輯，不能只修其中一條。"
        ),
        "keywords": [
            "*", "分類器", "classifier", "router", "路由", "intent", "misroute",
            "誤判", "決策空間", "decision space", "缺選項", "missing option",
        ],
        "confidence": 0.85,
    },
    {
        "category": "validation",
        "title": "工作流草稿重複加了兩個互斥的替代指令，而不是擇一",
        "technique": (
            "當 LLM 草稿生成器（工作流/計畫產生器）把同一份內容重複餵給兩個"
            "『殊途同歸』的指令（例如都能把同一段文字輸出成語音，只是傳遞方式"
            "不同：一個傳檔案、一個現場播放）時，先別急著怪指令的用法說明寫得"
            "不清楚——那些說明各自讀起來可能都合理，問題通常是提示詞裡根本"
            "沒有講『這些指令是彼此的替代方案，只能選一個』。LLM 看到需求裡"
            "同時出現『轉成語音』『念出來』這類重疊語意時，若沒有明講互斥，"
            "會傾向兩個都放進去『保險』，導致其中一個在非預期情境下就先整條"
            "工作流失敗（例如需要對話上下文的那個指令，在排程/非對話情境下"
            "直接炸掉，卡在它後面的替代指令永遠沒機會執行）。修法有兩層："
            "(1) 在提示詞的規則區塊加一條明講『功能重疊/互斥的指令只能擇一，"
            "不要每個都加』；(2) 讓每個指令自己的用法說明點名『與哪個指令互斥、"
            "各自適用什麼情境』，這樣哪怕只讀到其中一個指令的說明也能推斷出"
            "另一個的存在與取捨依據。"
        ),
        "keywords": [
            "*", "workflow", "工作流", "草稿", "draft", "互斥", "mutually exclusive",
            "替代方案", "alternative", "重複步驟", "duplicate step", "command_sink",
        ],
        "confidence": 0.85,
    },
    {
        "category": "data_fetch",
        "title": "網路請求必須設 timeout 並包重試迴圈",
        "technique": (
            "所有 urllib.request.urlopen / requests.get 呼叫都必須傳入明確的 timeout（秒），"
            "不可依賴系統預設（系統預設常常是無限等待）。\n"
            "  urllib 寫法：urllib.request.urlopen(req, timeout=20)\n"
            "  requests 寫法：requests.get(url, timeout=20)\n"
            "此外，短暫的連線重置（Connection reset by peer）是正常的暫態網路錯誤，"
            "不代表端點本身壞掉。正確做法是在 urlopen/get 外包一個小型重試迴圈（2–3 次），"
            "每次失敗後等幾秒（3–5 秒）再試，只有重試全部耗盡才向上拋例外：\n"
            "  for _attempt in range(3):\n"
            "      try:\n"
            "          with urllib.request.urlopen(req, timeout=20) as r:\n"
            "              data = r.read()\n"
            "          break\n"
            "      except Exception:\n"
            "          if _attempt < 2:\n"
            "              time.sleep(3)\n"
            "          else:\n"
            "              raise\n"
            "不要忽略 timeout 或省略重試——產生的工具在排程/自動化情境下執行時，"
            "一次暫態錯誤就足以讓整個工作流步驟失敗。"
        ),
        "keywords": [
            "*", "urlopen", "requests", "http", "fetch", "timeout", "connection reset",
            "retry", "重試", "network", "網路", "transient", "暫態", "urllib", "error",
        ],
        "confidence": 0.95,
    },
    {
        "category": "concurrency",
        "title": "延遲初始化的單例互相呼叫時，非重入鎖會自我死鎖",
        "technique": (
            "兩個（或多個）lazy-init 單例各自用 threading.Lock() 保護，"
            "而其中一個的初始化流程又會呼叫到另一個時，同一條執行緒可能"
            "重新進入自己已持有的鎖——threading.Lock 不可重入，於是永遠卡死，"
            "而且外觀上行程還活著、其他功能照常運作，只有踩到那條路徑的請求全部懸掛。\n"
            "判斷徵兆：某類請求（尤其是『服務啟動後的第一個』該類請求）無限懸掛，"
            "其他請求正常；執行緒傾印顯示多條執行緒等同一把鎖但找不到持有者在做事。\n"
            "修法（擇一）：\n"
            "  1) 打破環：初始化時不要急切（eager）解析對方，改註冊一個"
            "     lazy proxy / 閉包，等真正被呼叫（dispatch 時、已離開鎖）才解析；\n"
            "  2) 用 threading.RLock() —— 但這只治標，環仍在，之後改動容易再爆；\n"
            "  3) 重排初始化順序，讓依賴單向化。\n"
            "通則：持有鎖的區段內，不要呼叫任何『可能反過來呼叫自己模組』的函式。"
        ),
        "keywords": [
            "*", "deadlock", "死鎖", "lock", "threading", "lazy", "singleton",
            "初始化", "init", "hang", "懸掛", "卡住", "re-entrant", "RLock",
            "concurrency", "併發",
        ],
        "confidence": 0.9,
    },
    {
        "category": "output_contract",
        "title": "會被 LLM 判官驗收的回覆，必須自述動作與變化，不能只報末端狀態",
        "technique": (
            "當工具/指令的文字回覆會交給下游 LLM 判官（或使用者）判斷『需求是否已完成』時，"
            "回覆只報末端狀態（如『目前音量：80/100』）會被誤判為未完成——"
            "判官看不出這個狀態是剛剛執行動作的結果，於是升級成多步驟重試、燒光配額。\n"
            "正確做法：回覆要自述（1）執行了什麼動作（2）造成什麼變化（舊值 → 新值），"
            "例如『音量已調高：70 → 80/100』；若已在極限無法變化，也要明說"
            "（『音量已在最大值（100/100），無法再調高』），讓單獨這句話就足以證明需求已處理。\n"
            "同時，判官提示詞要為『改變狀態的動作類需求』加一條通用規則："
            "回覆已回報動作執行成功、或已在上限/下限無法再改變，即判定完成，"
            "不因缺少額外資訊而判未達成。"
        ),
        "keywords": [
            "*", "llm", "judge", "判官", "satisfaction", "驗收", "回覆", "reply",
            "state", "狀態", "action", "動作", "goal loop", "retry", "重試",
            "output", "contract",
        ],
        "confidence": 0.9,
    },
    {
        "category": "validation",
        "title": "語意相似度快取要加結構性守衛，且只能 fail-open 回慢路徑",
        "technique": (
            "用 embedding 相似度做快取/比對時，餘弦分數對三種結構差異「高得騙人」："
            "（1）只差數字的參數（調到50 vs 調到70）；（2）一文包含另一文"
            "（『開燈然後放歌』包含已快取的『開燈』→ 命中會把多步驟需求截斷成單步）；"
            "（3）反義改寫（調高 vs 調低 仍可到 ~0.87）。\n"
            "對策：門檻之外再加結構性守衛——數字序列必須完全相等、互為嚴格子字串者"
            "一律拒絕語意命中；守衛擋下的查詢 fail-open 回原本的慢路徑（LLM/完整計算），"
            "絕不猜。守衛是結構檢查（regex 數字、子字串關係），不是關鍵字表。"
            "門檻與守衛都要用真實 embedding 模型實測邊際，不可只憑文獻預設值。"
        ),
        "keywords": [
            "embedding", "相似度", "similarity", "cosine", "快取", "cache",
            "semantic", "語意", "門檻", "threshold", "guard", "守衛",
        ],
        "confidence": 0.9,
    },
    {
        "category": "validation",
        "title": "資料結構經 JSON 往返後型別會退化，重建時要還原",
        "technique": (
            "dataclass/物件序列化成 JSON 再讀回來時，tuple 變 list、set 變 list、"
            "datetime 變 str、int key 變 str——直接 **dict 重建物件會得到"
            "「欄位值型別跟新鮮物件不一致」的隱性 bug（等值比較、hash、isinstance 都會出錯）。\n"
            "對策：從快取/檔案/DB 重建物件時，逐欄把 list 還原成宣告的容器型別"
            "（或用有型別驗證的重建函式），並寫一個「快取重建結果 == 新鮮結果」的等值測試。"
        ),
        "keywords": [
            "json", "serialize", "序列化", "roundtrip", "tuple", "list",
            "dataclass", "asdict", "快取", "cache", "型別", "type",
        ],
        "confidence": 0.9,
    },
    {
        "category": "validation",
        "title": "區網連線被拒先做「系統工具 vs 自家程序」差分，權限算在 responsible app 頭上",
        "technique": (
            "macOS 上對區網主機的連線拿到 EHOSTUNREACH（No route to host）但 ping/ARP 正常時，"
            "先做差分測試：用 Apple 簽名的系統工具（nc、ping）打同一目標，再用自己的程序打——"
            "「系統工具通、自家程序不通」是作業系統權限層（Local Network privacy）攔截的指紋，"
            "不是路由、防火牆或裝置故障；OS 更新後權限庫被重設是常見觸發點。\n"
            "而且權限不是算在 binary 或啟動指令上，是算在 launch 血統回溯到的 responsible app 上——"
            "不要用 socket/session 名稱猜血統，用 responsibility_get_pid_responsible_for_pid() 實測。"
            "缺授權時程式層繞不過（disclaim 自立門戶也照樣被拒），修法只能在系統設定授權。"
        ),
        "keywords": [
            "EHOSTUNREACH", "no route to host", "區網", "LAN", "local network",
            "TCC", "權限", "permission", "responsible", "macos", "差分", "differential",
        ],
        "confidence": 0.9,
    },
    {
        "category": "validation",
        "title": "探測 API 行為要複製生產請求的完整參數；隱藏判斷步驟不可繼承使用者可調的模型設定",
        "technique": (
            "兩個常一起發生的坑：\n"
            "1) 用 curl/腳本重現線上問題時，省略的欄位會落到 parse 端預設值，"
            "可能把請求導到跟生產完全不同的路徑（例如省略 backend 欄位→預設 local，"
            "但真實前端送的是 cloud）。重現失敗/成功前，先抓一份真實請求的完整參數再探測，"
            "否則量到的是預設路徑不是生產路徑。\n"
            "2) 使用者可調的模型/供應商設定（UI 存檔、pool 覆寫）只該影響使用者可見的生成；"
            "管線裡的隱藏判斷步驟（意圖規劃、滿意度判定、流程草擬）要釘在專用的判斷模型上，"
            "不然使用者換一顆聊天用小模型，整條管線的判斷品質跟著沉。共用 resolver 時，"
            "逐一列出它的所有 caller，分清哪些是「回答」哪些是「判斷」。"
        ),
        "keywords": [
            "probe", "探測", "default", "預設值", "reproduce", "重現",
            "model override", "模型覆寫", "planner", "判斷", "judgment", "pipeline",
        ],
        "confidence": 0.9,
    },
    {
        "category": "testing",
        "title": "測試載入相依檔案的路徑，不能只寫死本機開發目錄佈局",
        "technique": (
            "測試/腳本用相對路徑走訪找 sibling 目錄或相依檔案時"
            "（例如 Path(__file__).resolve().parents[N] / \"other_repo\" / ...），"
            "若只硬編一種目錄佈局，換一個執行環境（CI checkout、容器、他人機器）"
            "佈局不同就會靜默指到不存在的路徑；若載入函式本身沒有明確的"
            "『找不到就報錯』防呆，缺檔會被下游當成『資料是空的』處理，"
            "產生一長串看似無關的斷言失敗（缺標題、缺欄位、query 退化成只剩 URL 等），"
            "很容易被誤判成別的成因（版本漂移、平台差異），浪費大量時間繞遠路。\n"
            "根治法：路徑解析函式列出所有可能佈局的候選路徑（本機 sibling 目錄、"
            "CI 巢狀 checkout 目錄等），依序嘗試，全部找不到才明確 raise "
            "FileNotFoundError 並把所有候選路徑印出來——絕不要吞掉成空字串/空結果。"
            "定位這類問題時，先確認『輸入資料是否真的被讀到』，不要跳過這步直接懷疑邏輯本身。"
        ),
        "keywords": [
            "*", "fixture", "sibling", "path", "路徑", "ci", "checkout",
            "監控", "測試環境", "test layout", "relative path", "找不到",
        ],
        "confidence": 0.9,
    },
    {
        "category": "testing",
        "title": "測試 fixture 不可硬編絕對日期去對滾動時間窗（time-bomb test）",
        "technique": (
            "被測邏輯若含滾動時間窗（『近 N 天』聚合、expires_at = 建立時間 + N 天之類），"
            "fixture 裡硬編的絕對時間戳（如 '2026-04-18T09:00:00+09:00'）寫下當下會過，"
            "但真實時間一走出窗外，測試就整批無聲翻紅——症狀看起來像邏輯壞了"
            "（聚合數變 0、驗證回 expired），實際上程式完全正確，是 fixture 過期。"
            "同一天在兩個不同 repo 撞到同一類問題（30 天回饋窗聚合、30 天證明效期驗證）。\n"
            "根治法：fixture 時間戳一律相對於 now 產生"
            "（datetime.now(tz) - timedelta(days=1) 這種），要測『過期』分支就用"
            "負向偏移或把效期參數設成負值，絕不硬編絕對日期。"
            "另一個變體：epoch 秒（0.0）不可拿去跟 time.monotonic() 比——"
            "monotonic 的原點是行程啟動不是 epoch，要用 time.monotonic() ± 偏移。\n"
            "定位這類問題時，先看失敗訊息是否含 expired/窗外/計數歸零，"
            "再查 fixture 是否硬編日期，不要先懷疑業務邏輯。"
        ),
        "keywords": [
            "*", "fixture", "timestamp", "時間窗", "expired", "過期", "time-bomb",
            "timedelta", "rolling window", "monotonic", "epoch", "測試翻紅",
        ],
        "confidence": 0.9,
    },
    {
        "category": "performance",
        "title": "把大集合展開成呼叫引數（spread/*args）在十萬元素級會爆呼叫棧，規模路徑要逐元素處理",
        "technique": (
            "任何『把整個集合展開成單一函式呼叫的引數』的寫法——JS 的 fn(...arr)、"
            "arr.push(...items)、Math.max(...nums)，Python 的 fn(*huge_list)——"
            "引數是放在呼叫棧上的，集合一到 ~10 萬元素就丟 RangeError: Maximum call "
            "stack size exceeded / 等價的棧溢位，而且小規模測試永遠不會踩到，"
            "只在生產資料長大後突然炸。曾在 CRDT ledger 的合併路徑用 push(...events) "
            "攤平 peer bucket，1k/10k 測試全綠，300k 事件一跑就棧溢位。\n"
            "根治法：規模會隨資料成長的路徑一律逐元素 push / extend / concat，"
            "不要用展開語法傳集合；聚合（max/min）改用 reduce 或迴圈。\n"
            "同場加映：對有硬上限的共用 heap（wasm32 4GB、embedded runtime）做大規模"
            "實驗時，同時只保留一份大物件並顯式釋放（如 Automerge 的 A.free），"
            "兩份 30 萬事件的文件同時在 heap 就 OOM——close()/GC 不保證釋放原生資源。\n"
            "定位這類問題：錯誤棧指在 push/呼叫行而非業務邏輯，且只在大 N 出現，"
            "先懷疑展開語法而不是資料本身。"
        ),
        "keywords": [
            "*", "performance", "spread", "棧溢位", "stack overflow", "RangeError",
            "push", "args", "scale", "大陣列", "wasm", "oom", "heap",
        ],
        "confidence": 0.9,
    },
    {
        "category": "testing",
        "title": "驗證持續增長系統的兩次讀取時，用回應自帶的高水位游標切齊比對窗做精確比對，別用牆鐘順序的包含檢查",
        "technique": (
            "被測系統若在測試進行中持續追加資料（心跳、audit tick、背景同步），"
            "兩個時刻的快照永遠不相等，直覺的處理是降級成包含檢查"
            "（快照A ⊆ 回應 ⊆ 快照B 的三明治）。但包含檢查有系統性盲點："
            "落在兩次讀取之間新增、又被受測路徑漏掉的資料，兩側包含都抓不到——"
            "測試綠燈但完整性沒被證明。同一個測試曾歷經三輪演化：精確相等（flaky）→"
            "三明治包含（有漏偵測盲點）→ 正解。\n"
            "正解：如果受測回應自帶『我計算到哪個時點』的標記（server cursor、"
            "high-water mark、sequence 上限、etag），就用該標記把稍後的完整快照"
            "切齊到同一瞬間（filter seq <= cursor[writer]），然後做雙向精確集合比對；"
            "窗外資料因超過游標被排除，比對既 race-free 又完整。"
            "若協定沒有這種標記，先考慮加上——它同時是生產端 resume 和測試端"
            "可驗證性的基礎。\n"
            "同場加映：接受外部量測檔（benchmark/probe artifacts）當驗收證據時，"
            "驗收規則必須驗證檔案的實驗參數（seed、樣本數、方法論）與預註冊宣稱"
            "配對一致，否則任何人丟一個參數不同的假檔就能讓規則誤判 PASS。"
        ),
        "keywords": [
            "*", "testing", "flaky", "race", "snapshot", "cursor", "high-water mark",
            "精確比對", "包含檢查", "收斂", "replication", "驗收", "benchmark", "evidence",
        ],
        "confidence": 0.9,
    },
    {
        "category": "architecture",
        "title": "分頁游標與資料頭游標是不同契約，live 訂閱不可拿第一頁尾端當 journal head",
        "technique": (
            "有分頁的 append-only API 通常同時存在兩個高水位：本頁最後一筆的 cursor，"
            "以及整份資料目前最新的 cursor。當 retained history 超過 page limit 時，"
            "第一頁的 server cursor 只代表『這一頁讀到哪裡』，不代表 journal head；"
            "若 live stream 用它當 bootstrap 起點，就會把後續歷史頁誤當成新事件重送，"
            "小資料測試全綠、累積到多頁才爆出 duplicate replay。\n"
            "契約應明確分開 page cursor 與 atomic latest/high-water cursor；live 訂閱從"
            "latest cursor 開始，歷史 recovery 才從 page cursor 逐頁前進。測試必須預載"
            "超過一頁的事件，斷言 negotiated stream 只送 bootstrap 後新增的事件，不能"
            "用空 journal fixture 代表這個邊界。"
        ),
        "keywords": [
            "*", "pagination", "cursor", "journal", "high-water mark", "live stream",
            "bootstrap", "duplicate replay", "分頁", "游標", "重播", "契約",
        ],
        "confidence": 0.95,
    },
    {
        "category": "architecture",
        "title": "去重 key 必須與序列閘門同尺度（per-writer），純時間戳 ID 會跨產生者碰撞",
        "technique": (
            "分散式事件複製常同時有兩層機制：以 event id 去重、以 per-writer sequence "
            "連續性閘門擋亂序。若去重用全域 id 而序列閘門是 per-writer，兩者尺度不一致："
            "不同 writer 合法鑄出相同 id（典型成因：id 只含 `<名稱>-<Date.now()>`，"
            "兩個節點同毫秒對同一資源動作）時，第二個 writer 的事件被靜默丟棄，"
            "卻仍佔用它自己的 sequence 槽位 → 閘門視為永遠補不上的 gap → 該 writer "
            "之後所有事件被永久拒收，且兩側都不會記錄任何錯誤。\n"
            "正解：(1) 去重 key 與序列閘門同尺度——(writer, event_id) 而非 event_id；"
            "(2) 產生 id 一律含產生者身分（node id）或真熵，純 Date.now() 不是 id；"
            "(3) key 串接要防注入式歧義（writer 名可含任意字元，用 length-prefix 或"
            "結構化 tuple，別用裸分隔符）。\n"
            "除錯線索：症狀是『事件存在於節點 X、永遠不出現在節點 Y、無任何錯誤 log』"
            "——先懷疑去重/閘門層，再懷疑傳輸層；『靜默丟棄＋永久閘門』的組合是最難"
            "觀測的失效形狀，值得在合併層對『被丟棄但佔序列槽』的事件加計數或斷言。"
        ),
        "keywords": [
            "*", "dedup", "event_id", "sequence", "contiguity", "replication",
            "distributed", "collision", "Date.now", "writer", "去重", "序列", "閘門",
        ],
        "confidence": 0.9,
    },
)


OBSERVATION_MARKER = "\n---\n最近觀察：\n"
OBSERVATION_SUMMARY_CAP = 2000

# A no-data stub: EntityResearcher couldn't find enough to describe an entity, so it
# cached a 資料不足 / confidence~0 placeholder ONLY to stop future encounters from
# re-hammering web search (a negative cache). Such stubs are NOT real knowledge —
# they must never be surfaced in the daily digest nor accrete 最近觀察 observations.
NO_DATA_SUMMARY = "資料不足"
# A common-knowledge stub: the entity is general public knowledge the local model
# already grounds on its own (Amazon / 日本 / YouTube …), so researching + storing it
# adds nothing to the classifier and only clutters the digest. Like a no-data stub it
# is cached at confidence~0 purely as a negative cache (stop re-research) and must
# never be surfaced nor accrete 最近觀察 observations.
COMMON_KNOWLEDGE_SUMMARY = "一般常識（地端模型已知，無需 grounding）"
INSUFFICIENT_CONFIDENCE = 0.1


def _summary_head(summary: str) -> str:
    """The canonical-knowledge head of a summary, stripped of appended 最近觀察 bullets."""
    return (summary or "").partition(OBSERVATION_MARKER)[0].strip()


def is_insufficient_entry(entry: KnowledgeEntry) -> bool:
    """True iff *entry* carries no surfaceable knowledge — either a no-data stub
    (資料不足) or a common-knowledge stub (一般常識). Detected by near-zero confidence
    (only the stub paths write <0.1; real research writes 0.5+) OR a stub knowledge
    head. Appended 最近觀察 bullets do NOT flip the verdict — the head stays a stub
    marker, so a stub can't be laundered into a 'real' entry just by logging
    observations onto it."""
    if float(entry.confidence) < INSUFFICIENT_CONFIDENCE:
        return True
    head = _summary_head(entry.summary)
    return head == "" or head in (NO_DATA_SUMMARY, COMMON_KNOWLEDGE_SUMMARY)


# An operational-cache entry is a non-knowledge lookup marker the system writes purely
# to avoid repeating an expensive step — e.g. the 遊々亭 game-code resolver caches each
# item→code mapping so the same item never pays a second /search (priority #2「不被封鎖」),
# and /research caches Mercari item-page facts so the same item URL/title can be found
# again. These are real caches the system needs, but they carry no human-reviewable RAG
# knowledge and must never be surfaced in the daily digest.
#
# The markers below are fixed protocol tokens (closed enums, NOT open-world entity
# recognition), so detecting them here does not violate the no-hardcode rule. NOTE:
# origin alone can't distinguish these — operational caches and real /research entity
# knowledge share origin="research_command" — so summary/canonical protocol markers are
# the robust signal.
YUYUTEI_CACHE_MARKER = "yuyutei_code="
MERCARI_ITEM_CACHE_PREFIX = "mercari:"
MERCARI_ITEM_CACHE_SUMMARY_PREFIX = "Mercari 商品頁資料："
OPERATIONAL_CACHE_MARKERS = (YUYUTEI_CACHE_MARKER,)


def is_operational_cache_entry(entry: KnowledgeEntry) -> bool:
    """True iff *entry* is an internal operational cache marker rather than
    human-reviewable knowledge. Detected by a fixed protocol marker at the
    head of the summary."""
    head = _summary_head(entry.summary)
    if any(head.startswith(marker) for marker in OPERATIONAL_CACHE_MARKERS):
        return True
    return (
        entry.origin == "research_command"
        and entry.entity_type == "product"
        and entry.entity_canonical.startswith(MERCARI_ITEM_CACHE_PREFIX)
        and head.startswith(MERCARI_ITEM_CACHE_SUMMARY_PREFIX)
    )


def _build_observation_bullet(
    *,
    observed_at: str,
    rationale: str,
    suggested_action: str,
    tweet_url: str,
    deadline: str | None,
) -> str:
    date_str = (observed_at or "")[:10] or _utc_now_iso()[:10]
    parts: list[str] = [f"- [{date_str}]"]
    rationale_clean = (rationale or "").strip()
    if rationale_clean:
        parts.append(rationale_clean)
    action_clean = (suggested_action or "").strip()
    if action_clean:
        parts.append(f"— {action_clean}")
    url_clean = (tweet_url or "").strip()
    if url_clean:
        parts.append(f"({url_clean})")
    if deadline:
        parts.append(f"[~{str(deadline)[:10]}]")
    return " ".join(parts)


def _append_observation_to_summary(summary: str, bullet: str) -> str:
    """Append ``bullet`` under the OBSERVATION_MARKER, FIFO-trim oldest bullets
    if the total exceeds OBSERVATION_SUMMARY_CAP. The pre-marker head is
    preserved verbatim — only observation bullets get rotated."""
    if OBSERVATION_MARKER in summary:
        head, _, tail = summary.partition(OBSERVATION_MARKER)
        existing = [line for line in tail.splitlines() if line.strip()]
    else:
        head = summary.rstrip()
        existing = []

    bullets = existing + [bullet]
    rendered = head + OBSERVATION_MARKER + "\n".join(bullets)
    # FIFO-drop oldest bullets until under cap. Always keep at least the
    # newest bullet so the just-appended observation isn't immediately lost.
    while len(rendered) > OBSERVATION_SUMMARY_CAP and len(bullets) > 1:
        bullets.pop(0)
        rendered = head + OBSERVATION_MARKER + "\n".join(bullets)
    return rendered


def _row_to_entry(row: sqlite3.Row) -> KnowledgeEntry:
    try:
        urls = json.loads(row["source_urls_json"] or "[]")
        if not isinstance(urls, list):
            urls = []
    except (TypeError, ValueError, json.JSONDecodeError):
        urls = []
    return KnowledgeEntry(
        entry_id=row["entry_id"],
        entity_canonical=row["entity_canonical"],
        entity_type=row["entity_type"],
        summary=row["summary"],
        source_urls=tuple(str(u) for u in urls),
        confidence=float(row["confidence"]),
        origin=row["origin"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_referenced_at=row["last_referenced_at"],
    )


def format_knowledge_block(
    entries: list[KnowledgeEntry],
    *,
    unknown_entities: tuple[str, ...] = (),
    max_chars: int = 1500,
) -> str:
    """Format retrieved knowledge entries into the LLM prompt's ``<知識庫參考>``
    block. Entries are truncated proportionally so total stays under
    ``max_chars``. ``unknown_entities`` are entities the classifier tried to
    retrieve but had no DB entry — surfaced as placeholders so the LLM can
    treat them as 'no grounded knowledge yet'."""
    if not entries and not unknown_entities:
        return "(無)"
    lines: list[str] = []
    per_entry_budget = max(80, max_chars // max(1, len(entries))) if entries else 0
    for entry in entries:
        summary = entry.summary.strip()
        if len(summary) > per_entry_budget:
            summary = summary[: per_entry_budget - 1] + "…"
        lines.append(f"- {entry.entity_canonical} ({entry.entity_type}): {summary}")
    for unk in unknown_entities:
        lines.append(f"- {unk}: (資料庫尚無此 entity；已排程 web research)")
    return "\n".join(lines)
