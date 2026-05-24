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

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


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
"""


# Allowed entity_type values. Free-text values are accepted (the writer side
# may invent new types as the system evolves) but the classifier prompt and
# retrieval rendering treat these as the canonical set.
ENTITY_TYPES: tuple[str, ...] = ("ip", "product", "set", "creator", "event", "store", "other")
ORIGINS: tuple[str, ...] = ("web_research", "manual", "tweet_aggregation")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_canonical(name: str) -> str:
    """Canonical entity names are stored lower-case and stripped. Keeps lookup
    simple (case-insensitive) and avoids duplicate rows for case variants."""
    return (name or "").strip().lower()


def build_entry_id(*, entity_canonical: str, entity_type: str) -> str:
    return sha1(f"{entity_canonical}|{entity_type}".encode("utf-8")).hexdigest()


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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Auto-bootstrap so callers don't have to remember a separate step —
        # CREATE TABLE IF NOT EXISTS is idempotent and cheap to re-run.
        self.bootstrap()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
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
