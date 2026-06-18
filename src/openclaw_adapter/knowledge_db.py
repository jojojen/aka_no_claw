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
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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
"""


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
            conn.executescript(_SCHEMA)

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
                        json.dumps(list(source_urls), ensure_ascii=False),
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
# item→code mapping so the same item never pays a second /search (priority #2「不被封鎖」).
# The marker is a fixed protocol token (a closed enum, NOT open-world entity recognition),
# so detecting it here does not violate the no-hardcode rule. Like a no-data stub it is a
# real cache the system needs, but it carries no human-reviewable knowledge and must never
# be surfaced in the daily digest. NOTE: origin can't distinguish these — both the yuyutei
# cache and real /research entity knowledge share origin="research_command" — so the summary
# marker is the robust signal (and matches what YuyuteiGameCodeResolver itself keys on).
YUYUTEI_CACHE_MARKER = "yuyutei_code="
OPERATIONAL_CACHE_MARKERS = (YUYUTEI_CACHE_MARKER,)


def is_operational_cache_entry(entry: KnowledgeEntry) -> bool:
    """True iff *entry* is an internal operational cache marker rather than
    human-reviewable knowledge. Detected by a fixed protocol marker at the
    head of the summary."""
    head = _summary_head(entry.summary)
    return any(head.startswith(marker) for marker in OPERATIONAL_CACHE_MARKERS)


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
