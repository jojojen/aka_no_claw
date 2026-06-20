"""Canonical Market Entity Registry & Alias Graph (issue #12).

The same real-world collectible shows up under many strings — official JP name,
English/Chinese name, set-prefixed title, a noisy Mercari listing, an SNS
nickname, a card number, a JAN code. Without a canonical layer the system cannot
join price observations, demand signals, listings, reputation evidence and
opportunity candidates onto one item.

This module is that layer: a deterministic registry of ``MarketEntity`` records,
an alias graph (``EntityAlias``) that maps every observed string to an entity, a
parent/child relation graph (``EntityRelation``), and a deterministic resolver
that turns a query into a stable ``entity_id``, ranked candidates, or an
unresolved result. It deliberately stays deterministic — the issue's Non-goals
exclude valuation, forecasting and an ML resolver; those are follow-ups that use
``entity_id`` as the join key.

Persistence mirrors the #9/#11 registries: SQLite tables in the same repo style,
seedable in tests, with runtime aliases addable without code changes and a
``entity_resolution_events`` audit log.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# ── Deliverable 1: closed vocabularies ───────────────────────────────────────
ENTITY_KINDS: tuple[str, ...] = (
    "single_card",
    "sealed_box",
    "sealed_pack",
    "set",
    "series",
    "character_goods",
    "cd",
    "figure",
    "book",
    "event_merch",
    "other",
)
DEFAULT_ENTITY_KIND = "other"

GRADE_SCOPES: tuple[str, ...] = ("raw", "graded", "both", "unknown")
DEFAULT_GRADE_SCOPE = "unknown"

# ── Deliverable 2: alias-type vocabulary ─────────────────────────────────────
ALIAS_TYPES: tuple[str, ...] = (
    "official",
    "marketplace",
    "sns",
    "derived",
    "manual",
    "translation",
    "product_code",
)
DEFAULT_ALIAS_TYPE = "derived"

# ── Deliverable 4: relation-type vocabulary ──────────────────────────────────
RELATION_TYPES: tuple[str, ...] = (
    "contains",
    "belongs_to_set",
    "belongs_to_series",
    "release_family",
    "variant_of",
    "graded_version_of",
    "character_of",
)
DEFAULT_RELATION_TYPE = "release_family"

ENTITY_STATUSES: tuple[str, ...] = ("active", "merged", "deprecated")
DEFAULT_ENTITY_STATUS = "active"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snap(value: str | None, vocab: tuple[str, ...], default: str) -> str:
    token = (value or "").strip().lower()
    return token if token in vocab else default


def clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# Product codes / JAN / card numbers are matched structurally, so normalize them
# tightly (digits + letters only) — "234/193" and "234-193" collapse to one key.
_CODE_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str | None) -> str:
    return _CODE_RE.sub("", (value or "").lower())


def normalize_alias_text(value: str | None) -> str:
    """Fold an alias / query to a comparison key: lower-cased, punctuation and
    runs of whitespace collapsed to single spaces, trimmed. Keeps word
    boundaries (unlike code slugs) so noisy titles still tokenize."""
    # \w (UNICODE) keeps ASCII alphanumerics, CJK, kana and fullwidth letters;
    # drop underscore and everything else to spaces, then collapse runs.
    raw = re.sub(r"[\W_]+", " ", (value or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", raw).strip()


def build_entity_id(
    *,
    entity_kind: str,
    franchise: str | None = None,
    canonical_title: str = "",
    set_code: str | None = None,
    card_number: str | None = None,
    product_code: str | None = None,
    jan_code: str | None = None,
    grade_scope: str = DEFAULT_GRADE_SCOPE,
) -> str:
    """Stable, deterministic id, e.g. ``ent_pokemon_m2a_234193_raw``.

    Prefers structured identifiers (set code, card/product/JAN code) so the same
    catalog item always maps to the same id regardless of how its title is
    written; falls back to the franchise + canonical title when no structured
    code is known. ``grade_scope`` is appended only when it distinguishes a
    version (``raw``/``graded``) so raw and graded variants get distinct ids."""
    kind = _snap(entity_kind, ENTITY_KINDS, DEFAULT_ENTITY_KIND)
    grade = _snap(grade_scope, GRADE_SCOPES, DEFAULT_GRADE_SCOPE)
    parts: list[str] = []
    fr = _slug(franchise)
    if fr:
        parts.append(fr)

    structured = [_slug(s) for s in (set_code, card_number, product_code, jan_code)]
    structured = [s for s in structured if s]
    if structured:
        parts.extend(structured)
    else:
        title = _slug(canonical_title)
        if title:
            parts.append(title)
        parts.append(_slug(kind))

    if grade in ("raw", "graded"):
        parts.append(grade)

    if not any(p for p in parts):
        # Nothing identifying at all — fall back to a content hash so the id is
        # still deterministic and never empty.
        parts = [sha1((canonical_title or "").encode("utf-8")).hexdigest()[:12]]
    return "ent_" + "_".join(parts)


# ── Models ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MarketEntity:
    entity_id: str
    entity_kind: str
    canonical_title: str
    franchise: str | None = None
    canonical_lang: str | None = None
    product_type: str | None = None
    set_name: str | None = None
    set_code: str | None = None
    card_number: str | None = None
    product_code: str | None = None
    jan_code: str | None = None
    grade_scope: str = DEFAULT_GRADE_SCOPE
    release_date: str | None = None  # ISO date string; SQLite has no date type
    status: str = DEFAULT_ENTITY_STATUS


@dataclass(frozen=True, slots=True)
class EntityAlias:
    entity_id: str
    alias_text: str
    alias_type: str = DEFAULT_ALIAS_TYPE
    alias_lang: str | None = None
    confidence: float = 1.0
    source: str | None = None


@dataclass(frozen=True, slots=True)
class EntityRelation:
    parent_entity_id: str
    child_entity_id: str
    relation_type: str = DEFAULT_RELATION_TYPE
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class EntityMatch:
    entity_id: str
    score: float
    matched_alias: str
    match_reason: str          # exact | normalized | substring | code
    ambiguous: bool = False


# ── Persistence (Deliverable 6) ──────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_entities (
    entity_id       TEXT PRIMARY KEY,
    entity_kind     TEXT NOT NULL,
    canonical_title TEXT NOT NULL,
    franchise       TEXT,
    canonical_lang  TEXT,
    product_type    TEXT,
    set_name        TEXT,
    set_code        TEXT,
    card_number     TEXT,
    product_code    TEXT,
    jan_code        TEXT,
    grade_scope     TEXT NOT NULL DEFAULT 'unknown',
    release_date    TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS market_entity_aliases (
    entity_id   TEXT NOT NULL,
    alias_norm  TEXT NOT NULL,   -- normalize_alias_text(alias_text), the match key
    alias_text  TEXT NOT NULL,   -- original, for display/audit
    alias_type  TEXT NOT NULL DEFAULT 'derived',
    alias_lang  TEXT,
    confidence  REAL NOT NULL DEFAULT 1.0,
    source      TEXT,
    PRIMARY KEY (entity_id, alias_norm),
    FOREIGN KEY (entity_id) REFERENCES market_entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mealias_norm ON market_entity_aliases(alias_norm);

CREATE TABLE IF NOT EXISTS market_entity_relations (
    parent_entity_id TEXT NOT NULL,
    child_entity_id  TEXT NOT NULL,
    relation_type    TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (parent_entity_id, child_entity_id, relation_type),
    FOREIGN KEY (parent_entity_id) REFERENCES market_entities(entity_id) ON DELETE CASCADE,
    FOREIGN KEY (child_entity_id)  REFERENCES market_entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_merel_parent ON market_entity_relations(parent_entity_id);
CREATE INDEX IF NOT EXISTS idx_merel_child  ON market_entity_relations(child_entity_id);

CREATE TABLE IF NOT EXISTS entity_resolution_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,
    query_norm  TEXT NOT NULL,
    entity_id   TEXT,            -- NULL when unresolved
    outcome     TEXT NOT NULL,   -- resolved | ambiguous | unresolved
    score       REAL,
    match_reason TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""

# A resolver is "confident" only when the top candidate clears this score AND is
# clearly ahead of the runner-up; otherwise it reports ambiguity rather than
# silently picking one (Deliverable 3 acceptance).
_RESOLVE_MIN_SCORE = 0.60
_AMBIGUITY_MARGIN = 0.15


class MarketEntityRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Entity CRUD ──────────────────────────────────────────────────────────
    def upsert_entity(
        self,
        *,
        entity_kind: str,
        canonical_title: str,
        franchise: str | None = None,
        canonical_lang: str | None = None,
        product_type: str | None = None,
        set_name: str | None = None,
        set_code: str | None = None,
        card_number: str | None = None,
        product_code: str | None = None,
        jan_code: str | None = None,
        grade_scope: str = DEFAULT_GRADE_SCOPE,
        release_date: str | None = None,
        status: str = DEFAULT_ENTITY_STATUS,
        entity_id: str | None = None,
        aliases: tuple[EntityAlias, ...] = (),
    ) -> MarketEntity:
        """Insert or update a canonical entity and register its aliases. The id
        is derived deterministically from structured identifiers unless one is
        passed explicitly. The canonical title is always registered as an alias
        so a query equal to the title resolves."""
        kind = _snap(entity_kind, ENTITY_KINDS, DEFAULT_ENTITY_KIND)
        grade = _snap(grade_scope, GRADE_SCOPES, DEFAULT_GRADE_SCOPE)
        eid = entity_id or build_entity_id(
            entity_kind=kind,
            franchise=franchise,
            canonical_title=canonical_title,
            set_code=set_code,
            card_number=card_number,
            product_code=product_code,
            jan_code=jan_code,
            grade_scope=grade,
        )
        rec = MarketEntity(
            entity_id=eid,
            entity_kind=kind,
            canonical_title=canonical_title,
            franchise=franchise,
            canonical_lang=canonical_lang,
            product_type=product_type,
            set_name=set_name,
            set_code=set_code,
            card_number=card_number,
            product_code=product_code,
            jan_code=jan_code,
            grade_scope=grade,
            release_date=release_date,
            status=_snap(status, ENTITY_STATUSES, DEFAULT_ENTITY_STATUS),
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO market_entities (
                    entity_id, entity_kind, canonical_title, franchise, canonical_lang,
                    product_type, set_name, set_code, card_number, product_code,
                    jan_code, grade_scope, release_date, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    entity_kind = excluded.entity_kind,
                    canonical_title = excluded.canonical_title,
                    franchise = excluded.franchise,
                    canonical_lang = excluded.canonical_lang,
                    product_type = excluded.product_type,
                    set_name = excluded.set_name,
                    set_code = excluded.set_code,
                    card_number = excluded.card_number,
                    product_code = excluded.product_code,
                    jan_code = excluded.jan_code,
                    grade_scope = excluded.grade_scope,
                    release_date = excluded.release_date,
                    status = excluded.status
                """,
                (
                    rec.entity_id, rec.entity_kind, rec.canonical_title, rec.franchise,
                    rec.canonical_lang, rec.product_type, rec.set_name, rec.set_code,
                    rec.card_number, rec.product_code, rec.jan_code, rec.grade_scope,
                    rec.release_date, rec.status,
                ),
            )
            self._add_alias_inside(
                conn, EntityAlias(entity_id=eid, alias_text=canonical_title,
                                  alias_type="official", alias_lang=canonical_lang)
            )
            # Structured codes are first-class aliases so a card number / JAN
            # resolves directly to the entity.
            for code, atype in ((card_number, "product_code"), (set_code, "product_code"),
                                (product_code, "product_code"), (jan_code, "product_code")):
                if code:
                    self._add_alias_inside(
                        conn, EntityAlias(entity_id=eid, alias_text=code, alias_type=atype)
                    )
            for alias in aliases:
                self._add_alias_inside(conn, alias)
        return rec

    def get_entity(self, entity_id: str) -> MarketEntity | None:
        eid = (entity_id or "").strip()
        if not eid:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_entities WHERE entity_id = ?", (eid,)
            ).fetchone()
        return _row_to_entity(row) if row else None

    # ── Alias graph (Deliverable 2) ──────────────────────────────────────────
    def add_alias(
        self,
        entity_id: str,
        alias_text: str,
        *,
        alias_type: str = DEFAULT_ALIAS_TYPE,
        alias_lang: str | None = None,
        confidence: float = 1.0,
        source: str | None = None,
    ) -> bool:
        """Register an alias for an existing entity. Returns False if the entity
        is unknown (so a typo can't strand a dangling alias)."""
        if self.get_entity(entity_id) is None:
            return False
        with self.connect() as conn:
            return self._add_alias_inside(
                conn,
                EntityAlias(entity_id=entity_id, alias_text=alias_text,
                            alias_type=alias_type, alias_lang=alias_lang,
                            confidence=confidence, source=source),
            )

    @staticmethod
    def _add_alias_inside(conn: sqlite3.Connection, alias: EntityAlias) -> bool:
        norm = normalize_alias_text(alias.alias_text)
        if not norm:
            return False
        atype = _snap(alias.alias_type, ALIAS_TYPES, DEFAULT_ALIAS_TYPE)
        conn.execute(
            """
            INSERT INTO market_entity_aliases (
                entity_id, alias_norm, alias_text, alias_type, alias_lang, confidence, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id, alias_norm) DO UPDATE SET
                alias_text = excluded.alias_text,
                alias_type = excluded.alias_type,
                alias_lang = excluded.alias_lang,
                confidence = MAX(market_entity_aliases.confidence, excluded.confidence),
                source = COALESCE(excluded.source, market_entity_aliases.source)
            """,
            (alias.entity_id, norm, alias.alias_text, atype, alias.alias_lang,
             clamp_confidence(alias.confidence), alias.source),
        )
        return True

    def aliases_of(self, entity_id: str) -> list[EntityAlias]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_entity_aliases WHERE entity_id = ? "
                "ORDER BY confidence DESC, alias_text",
                (entity_id,),
            ).fetchall()
        return [_row_to_alias(r) for r in rows]

    # ── Relations (Deliverable 4) ────────────────────────────────────────────
    def add_relation(
        self,
        parent_entity_id: str,
        child_entity_id: str,
        *,
        relation_type: str = DEFAULT_RELATION_TYPE,
        confidence: float = 1.0,
    ) -> bool:
        """Link two existing entities. Returns False unless both exist — a
        relation to a missing entity would be unjoinable noise."""
        if self.get_entity(parent_entity_id) is None or self.get_entity(child_entity_id) is None:
            return False
        rtype = _snap(relation_type, RELATION_TYPES, DEFAULT_RELATION_TYPE)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO market_entity_relations (
                    parent_entity_id, child_entity_id, relation_type, confidence
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(parent_entity_id, child_entity_id, relation_type)
                DO UPDATE SET confidence = excluded.confidence
                """,
                (parent_entity_id, child_entity_id, rtype, clamp_confidence(confidence)),
            )
        return True

    def children_of(self, entity_id: str) -> list[EntityRelation]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_entity_relations WHERE parent_entity_id = ?",
                (entity_id,),
            ).fetchall()
        return [_row_to_relation(r) for r in rows]

    def parents_of(self, entity_id: str) -> list[EntityRelation]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_entity_relations WHERE child_entity_id = ?",
                (entity_id,),
            ).fetchall()
        return [_row_to_relation(r) for r in rows]

    # ── Resolver (Deliverable 3) ─────────────────────────────────────────────
    def resolve_entity_candidates(self, query: str, *, limit: int = 5) -> list[EntityMatch]:
        """Ranked candidate entities for a free-text query. Exact normalized
        match scores highest, then structured-code match, then substring overlap
        of a stored alias inside the (noisy) query. Deterministic: ties break on
        entity_id so output is stable. Never raises."""
        norm = normalize_alias_text(query)
        if not norm:
            return []
        code = _slug(query)
        best: dict[str, EntityMatch] = {}

        def offer(eid: str, score: float, matched: str, reason: str) -> None:
            prev = best.get(eid)
            if prev is None or score > prev.score:
                best[eid] = EntityMatch(entity_id=eid, score=score,
                                        matched_alias=matched, match_reason=reason)

        with self.connect() as conn:
            # 1) exact normalized alias match
            for r in conn.execute(
                "SELECT entity_id, alias_text, confidence FROM market_entity_aliases "
                "WHERE alias_norm = ?", (norm,),
            ).fetchall():
                offer(r["entity_id"], 1.0 * float(r["confidence"]), r["alias_text"], "exact")

            # 2) structured-code match (card no / set / product / JAN)
            if code and code != norm.replace(" ", ""):
                for r in conn.execute(
                    "SELECT entity_id, alias_text, confidence FROM market_entity_aliases "
                    "WHERE REPLACE(alias_norm, ' ', '') = ?",
                    (code,),
                ).fetchall():
                    offer(r["entity_id"], 0.95 * float(r["confidence"]), r["alias_text"], "code")

            # 3) substring: a stored alias appears inside a noisy query (e.g. a
            #    Mercari title). Score by how much of the query the alias covers.
            for r in conn.execute(
                "SELECT entity_id, alias_norm, alias_text, confidence "
                "FROM market_entity_aliases",
            ).fetchall():
                a = r["alias_norm"]
                if len(a) >= 3 and a != norm and a in norm:
                    coverage = len(a) / max(len(norm), 1)
                    score = (0.55 + 0.35 * coverage) * float(r["confidence"])
                    offer(r["entity_id"], round(score, 4), r["alias_text"], "substring")

        ranked = sorted(best.values(), key=lambda m: (-m.score, m.entity_id))
        ambiguous = (
            len(ranked) >= 2
            and ranked[0].score - ranked[1].score < _AMBIGUITY_MARGIN
        )
        if ambiguous:
            ranked = [
                EntityMatch(m.entity_id, m.score, m.matched_alias, m.match_reason, ambiguous=True)
                for m in ranked
            ]
        return ranked[:limit]

    def resolve_entity(self, query: str, *, log: bool = True) -> MarketEntity | None:
        """Resolve a query to a single entity, or None when unknown OR ambiguous.

        Returns an entity only when the top candidate clears ``_RESOLVE_MIN_SCORE``
        and is clearly ahead of the runner-up; an ambiguous tie returns None
        (callers wanting the tie should use ``resolve_entity_candidates``) so we
        never silently pick the wrong entity. Resolution is logged for audit."""
        candidates = self.resolve_entity_candidates(query)
        outcome, chosen, top = "unresolved", None, None
        if candidates:
            top = candidates[0]
            if top.ambiguous or top.score < _RESOLVE_MIN_SCORE:
                outcome = "ambiguous" if top.ambiguous else "unresolved"
            else:
                outcome = "resolved"
                chosen = self.get_entity(top.entity_id)
        if log:
            self._log_resolution(query, chosen.entity_id if chosen else None,
                                 outcome, top, len(candidates))
        return chosen

    def _log_resolution(
        self, query: str, entity_id: str | None, outcome: str,
        top: EntityMatch | None, candidate_count: int,
    ) -> None:
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO entity_resolution_events ("
                    "query, query_norm, entity_id, outcome, score, match_reason, "
                    "candidate_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (query, normalize_alias_text(query), entity_id, outcome,
                     top.score if top else None, top.match_reason if top else None,
                     candidate_count, _utc_now_iso()),
                )
        except Exception:  # audit log must never break resolution
            logger.exception("market_entity: failed to log resolution event")

    def describe_entity(self, entity_id: str) -> dict | None:
        """Diagnostic view for dashboards (Deliverable 5): title + aliases +
        relation counts. Returns None for an unknown id."""
        rec = self.get_entity(entity_id)
        if rec is None:
            return None
        return {
            "entity_id": rec.entity_id,
            "canonical_title": rec.canonical_title,
            "entity_kind": rec.entity_kind,
            "franchise": rec.franchise,
            "grade_scope": rec.grade_scope,
            "aliases": [(a.alias_text, a.alias_type) for a in self.aliases_of(entity_id)],
            "children": len(self.children_of(entity_id)),
            "parents": len(self.parents_of(entity_id)),
        }


# ── Deliverable 7: practical seed set ────────────────────────────────────────
# Small, hand-checked starter entities exercising every resolver path: a clean
# single card, a sealed box (parent set), a non-Pokémon TCG card, a deliberately
# ambiguous alias ("bocchi" → two entities), a noisy marketplace title, and an
# SNS nickname. This is bootstrap data, not an open-world catalog — runtime
# aliases are added via add_alias without code changes (issue #12 D6/D7).
def seed_market_entities(registry: "MarketEntityRegistry") -> list[str]:
    """Load the starter entities + aliases + one relation. Idempotent (upserts).
    Returns the list of seeded entity ids."""
    ids: list[str] = []

    # 1) Pokémon single card (raw) — structured identifiers drive the id.
    card = registry.upsert_entity(
        entity_kind="single_card", franchise="pokemon",
        canonical_title="リザードンex SAR", canonical_lang="ja",
        product_type="card", set_name="黒炎の支配者", set_code="sv3",
        card_number="201/108", grade_scope="raw",
    )
    registry.add_alias(card.entity_id, "Charizard ex SAR", alias_type="translation",
                       alias_lang="en", source="seed")
    registry.add_alias(card.entity_id, "リザex sar 黒炎", alias_type="marketplace",
                       confidence=0.8, source="seed")
    ids.append(card.entity_id)

    # 2) Pokémon sealed box + its set, related parent→child.
    box = registry.upsert_entity(
        entity_kind="sealed_box", franchise="pokemon",
        canonical_title="黒炎の支配者 BOX", canonical_lang="ja",
        product_type="box", set_name="黒炎の支配者", set_code="sv3",
        jan_code="4521329369334", grade_scope="raw",
    )
    registry.add_alias(box.entity_id, "Black Bolt Box JP", alias_type="translation",
                       alias_lang="en", source="seed")
    the_set = registry.upsert_entity(
        entity_kind="set", franchise="pokemon",
        canonical_title="黒炎の支配者", canonical_lang="ja", set_code="sv3",
    )
    registry.add_relation(box.entity_id, the_set.entity_id, relation_type="contains")
    registry.add_relation(the_set.entity_id, card.entity_id, relation_type="belongs_to_set")
    ids += [box.entity_id, the_set.entity_id]

    # 3) Non-Pokémon TCG card (Weiss Schwarz) — ambiguous "bocchi" alias case.
    ws = registry.upsert_entity(
        entity_kind="single_card", franchise="ws",
        canonical_title="後藤ひとり SP", canonical_lang="ja",
        product_type="card", set_name="ぼっち・ざ・ろっく！",
        set_code="btr", card_number="sp", grade_scope="raw",
    )
    registry.add_alias(ws.entity_id, "bocchi sp ws", alias_type="sns",
                       confidence=0.7, source="seed")
    ids.append(ws.entity_id)

    # 4) Union Arena card that ALSO answers to "bocchi" → ambiguity on the bare
    #    nickname; the resolver must return candidates, not silently pick one.
    ua = registry.upsert_entity(
        entity_kind="single_card", franchise="union_arena",
        canonical_title="ぼっち・ざ・ろっく！ 後藤ひとり", canonical_lang="ja",
        product_type="card", set_code="ua-btr", card_number="001",
        grade_scope="raw",
    )
    registry.add_alias(ws.entity_id, "bocchi", alias_type="sns", confidence=0.6, source="seed")
    registry.add_alias(ua.entity_id, "bocchi", alias_type="sns", confidence=0.6, source="seed")
    ids.append(ua.entity_id)

    return ids


# ── Row mappers ──────────────────────────────────────────────────────────────
def _row_to_entity(row: sqlite3.Row) -> MarketEntity:
    return MarketEntity(
        entity_id=row["entity_id"],
        entity_kind=row["entity_kind"],
        canonical_title=row["canonical_title"],
        franchise=row["franchise"],
        canonical_lang=row["canonical_lang"],
        product_type=row["product_type"],
        set_name=row["set_name"],
        set_code=row["set_code"],
        card_number=row["card_number"],
        product_code=row["product_code"],
        jan_code=row["jan_code"],
        grade_scope=row["grade_scope"],
        release_date=row["release_date"],
        status=row["status"],
    )


def _row_to_alias(row: sqlite3.Row) -> EntityAlias:
    return EntityAlias(
        entity_id=row["entity_id"],
        alias_text=row["alias_text"],
        alias_type=row["alias_type"],
        alias_lang=row["alias_lang"],
        confidence=float(row["confidence"]),
        source=row["source"],
    )


def _row_to_relation(row: sqlite3.Row) -> EntityRelation:
    return EntityRelation(
        parent_entity_id=row["parent_entity_id"],
        child_entity_id=row["child_entity_id"],
        relation_type=row["relation_type"],
        confidence=float(row["confidence"]),
    )
