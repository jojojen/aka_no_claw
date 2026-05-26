from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    build_listing_key,
    merge_string_list,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS opportunity_candidates (
    candidate_id TEXT PRIMARY KEY,
    game TEXT NOT NULL,
    product_type TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    product_identifier TEXT,
    search_query TEXT NOT NULL,
    heat_score REAL NOT NULL,
    reason TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_url TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    related_keywords_json TEXT NOT NULL DEFAULT '[]',
    is_target INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opportunity_price_checks (
    check_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    fair_value_jpy INTEGER NOT NULL,
    confidence REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    target_price_jpy INTEGER,
    notes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES opportunity_candidates(candidate_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS opportunity_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    listing_title TEXT NOT NULL,
    listing_price_jpy INTEGER NOT NULL,
    listing_url TEXT NOT NULL UNIQUE,
    thumbnail_url TEXT,
    fair_value_jpy INTEGER NOT NULL,
    price_confidence REAL NOT NULL,
    discount_pct REAL NOT NULL,
    opportunity_score REAL NOT NULL,
    accepted INTEGER NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    proof_url TEXT NOT NULL,
    seller_total_reviews INTEGER,
    seller_positive_rate REAL,
    seller_grade TEXT,
    reputation_status TEXT NOT NULL,
    notified_at TEXT,
    feedback_kind TEXT,           -- 'up' / 'down' / 'bought' / null
    feedback_at TEXT,             -- ISO timestamp of last feedback
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES opportunity_candidates(candidate_id) ON DELETE CASCADE
);
"""


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class OpportunityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def bootstrap(self) -> None:
        with self.connect() as connection:
            # One-time migration: legacy schemas had no product_type column.
            # Drop the opportunity tables so the new schema can be applied
            # cleanly. The opportunity cron tick repopulates candidates
            # within ~1 hour. Tests use tmp DBs and are unaffected.
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='opportunity_candidates'"
            ).fetchone()
            if existing is not None:
                column_names = {
                    row[1] for row in connection.execute("PRAGMA table_info(opportunity_candidates)")
                }
                if "product_type" not in column_names:
                    logger.warning(
                        "Dropping legacy opportunity tables to migrate to three-level (game / product_type / title) schema"
                    )
                    connection.executescript(
                        "DROP TABLE IF EXISTS opportunity_recommendations;"
                        "DROP TABLE IF EXISTS opportunity_price_checks;"
                        "DROP TABLE IF EXISTS opportunity_candidates;"
                    )
            connection.executescript(SCHEMA)
            # Idempotent ALTER for DBs that pre-date the alias columns.
            column_names = {
                row[1] for row in connection.execute("PRAGMA table_info(opportunity_candidates)")
            }
            if "aliases_json" not in column_names:
                connection.execute(
                    "ALTER TABLE opportunity_candidates ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "related_keywords_json" not in column_names:
                connection.execute(
                    "ALTER TABLE opportunity_candidates ADD COLUMN related_keywords_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "is_target" not in column_names:
                connection.execute(
                    "ALTER TABLE opportunity_candidates ADD COLUMN is_target INTEGER NOT NULL DEFAULT 0"
                )
            if "cooldown_until" not in column_names:
                connection.execute(
                    "ALTER TABLE opportunity_candidates ADD COLUMN cooldown_until TEXT"
                )
            rec_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(opportunity_recommendations)")
            }
            if "feedback_kind" not in rec_columns:
                connection.execute(
                    "ALTER TABLE opportunity_recommendations ADD COLUMN feedback_kind TEXT"
                )
            if "feedback_at" not in rec_columns:
                connection.execute(
                    "ALTER TABLE opportunity_recommendations ADD COLUMN feedback_at TEXT"
                )

    def upsert_candidate(self, candidate: OpportunityCandidate) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            # Read existing alias / related lists first so we can merge instead
            # of overwriting — user-curated entries via /hunt alias must not be
            # erased by a subsequent LLM extraction that didn't echo them back.
            existing = connection.execute(
                "SELECT aliases_json, related_keywords_json FROM opportunity_candidates "
                "WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            existing_aliases = _decode_json_list(existing["aliases_json"] if existing else "[]")
            existing_related = _decode_json_list(existing["related_keywords_json"] if existing else "[]")
            skip = (candidate.title, candidate.search_query)
            merged_aliases = merge_string_list(existing_aliases, candidate.aliases, skip=skip)
            merged_related = merge_string_list(existing_related, candidate.related_keywords, skip=skip)
            connection.execute(
                """
                INSERT INTO opportunity_candidates (
                    candidate_id, game, product_type, title, product_identifier,
                    search_query, heat_score, reason,
                    source_kind, source_url, metadata_json, created_at, updated_at,
                    aliases_json, related_keywords_json, is_target
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    game=excluded.game,
                    product_type=excluded.product_type,
                    title=excluded.title,
                    product_identifier=excluded.product_identifier,
                    search_query=excluded.search_query,
                    heat_score=MAX(opportunity_candidates.heat_score, excluded.heat_score),
                    reason=excluded.reason,
                    source_kind=excluded.source_kind,
                    source_url=excluded.source_url,
                    metadata_json=excluded.metadata_json,
                    status=CASE
                        WHEN opportunity_candidates.status = 'dismissed' THEN 'dismissed'
                        ELSE 'active'
                    END,
                    updated_at=excluded.updated_at,
                    aliases_json=excluded.aliases_json,
                    related_keywords_json=excluded.related_keywords_json,
                    is_target=MAX(opportunity_candidates.is_target, excluded.is_target)
                """,
                (
                    candidate.candidate_id,
                    candidate.game,
                    candidate.product_type,
                    candidate.title,
                    candidate.product_identifier,
                    candidate.search_query,
                    candidate.heat_score,
                    candidate.reason,
                    candidate.source_kind,
                    candidate.source_url,
                    _json(dict(candidate.metadata)),
                    candidate.created_at or now,
                    now,
                    _json(list(merged_aliases)),
                    _json(list(merged_related)),
                    1 if candidate.is_target else 0,
                ),
            )

    def update_candidate_aliases(
        self, candidate_id: str, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()
    ) -> tuple[str, ...] | None:
        return self._mutate_string_list(
            candidate_id, column="aliases_json", add=add, remove=remove
        )

    def update_candidate_related_keywords(
        self, candidate_id: str, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()
    ) -> tuple[str, ...] | None:
        return self._mutate_string_list(
            candidate_id, column="related_keywords_json", add=add, remove=remove
        )

    def _mutate_string_list(
        self,
        candidate_id: str,
        *,
        column: str,
        add: tuple[str, ...],
        remove: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        """Mutate the JSON list at column for candidate_id. Returns new list, or None if not found."""
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT title, search_query, {column} FROM opportunity_candidates "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            current = _decode_json_list(row[column])
            remove_set = {r.casefold() for r in remove if r}
            kept = tuple(item for item in current if item.casefold() not in remove_set)
            merged = merge_string_list(
                kept, add, skip=(row["title"], row["search_query"])
            )
            connection.execute(
                f"UPDATE opportunity_candidates SET {column} = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (_json(list(merged)), now, candidate_id),
            )
            return merged

    def dismiss_candidate(self, candidate_id: str) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE opportunity_candidates
                SET status = 'dismissed', updated_at = ?
                WHERE candidate_id = ? AND status = 'active'
                """,
                (now, candidate_id),
            )
        return cursor.rowcount > 0

    def list_due_candidates(self, *, limit: int, min_interval_seconds: int) -> list[OpportunityCandidate]:
        now = utc_now_iso()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM opportunity_candidates
                WHERE status = 'active'
                  AND (cooldown_until IS NULL OR cooldown_until < ?)
                  AND (
                    last_checked_at IS NULL
                    OR strftime('%s', ?) - strftime('%s', last_checked_at) >= ?
                  )
                ORDER BY heat_score DESC, updated_at DESC
                LIMIT ?
                """,
                (now, now, min_interval_seconds, limit),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def list_target_candidates(self, *, limit: int) -> list[OpportunityCandidate]:
        """Return active is_target=True candidates. Targets get every-tick
        attention (skipping the 30-min cooldown that auto-discovered ones use)."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM opportunity_candidates
                WHERE status = 'active' AND is_target = 1
                ORDER BY heat_score DESC, COALESCE(last_checked_at, '') ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def set_is_target(self, candidate_id: str, is_target: bool) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE opportunity_candidates SET is_target = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (1 if is_target else 0, now, candidate_id),
            )
        return cursor.rowcount > 0

    def has_any_target(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM opportunity_candidates "
                "WHERE status = 'active' AND is_target = 1 LIMIT 1"
            ).fetchone()
        return row is not None

    def get_candidate(self, candidate_id: str) -> "OpportunityCandidate | None":
        """Return the full candidate record, or None if not found."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM opportunity_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        return _candidate_from_row(row)

    def mark_candidate_checked(self, candidate_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE opportunity_candidates SET last_checked_at = ?, updated_at = ? WHERE candidate_id = ?",
                (now, now, candidate_id),
            )

    def record_price_check(self, price: PriceCheck) -> str:
        now = utc_now_iso()
        check_id = f"price_{price.candidate_id}_{now}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO opportunity_price_checks (
                    check_id, candidate_id, fair_value_jpy, confidence, sample_count,
                    target_price_jpy, notes_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    price.candidate_id,
                    price.fair_value_jpy,
                    price.confidence,
                    price.sample_count,
                    price.target_price_jpy,
                    _json(list(price.notes)),
                    now,
                ),
            )
        return check_id

    def listing_seen(self, url: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM opportunity_recommendations WHERE listing_url = ? LIMIT 1",
                (url,),
            ).fetchone()
        return row is not None

    def record_recommendation(self, recommendation: OpportunityRecommendation, *, accepted: bool) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO opportunity_recommendations (
                    recommendation_id, candidate_id, listing_id, listing_title, listing_price_jpy,
                    listing_url, thumbnail_url, fair_value_jpy, price_confidence, discount_pct,
                    opportunity_score, accepted, reasons_json, proof_url, seller_total_reviews,
                    seller_positive_rate, seller_grade, reputation_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_url) DO UPDATE SET
                    opportunity_score=excluded.opportunity_score,
                    accepted=excluded.accepted,
                    reasons_json=excluded.reasons_json,
                    proof_url=excluded.proof_url,
                    seller_total_reviews=excluded.seller_total_reviews,
                    seller_positive_rate=excluded.seller_positive_rate,
                    seller_grade=excluded.seller_grade,
                    reputation_status=excluded.reputation_status,
                    updated_at=excluded.updated_at
                """,
                (
                    recommendation.recommendation_id,
                    recommendation.candidate.candidate_id,
                    recommendation.listing.listing_id,
                    recommendation.listing.title,
                    recommendation.listing.price_jpy,
                    recommendation.listing.url,
                    recommendation.listing.thumbnail_url,
                    recommendation.price.fair_value_jpy,
                    recommendation.price.confidence,
                    recommendation.discount_pct,
                    recommendation.score,
                    int(accepted),
                    _json(list(recommendation.reasons)),
                    recommendation.reputation.proof_url,
                    recommendation.reputation.total_reviews,
                    recommendation.reputation.positive_rate,
                    recommendation.reputation.grade,
                    recommendation.reputation.status,
                    now,
                    now,
                ),
            )

    def mark_notified(self, recommendation_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE opportunity_recommendations SET notified_at = ?, updated_at = ? WHERE recommendation_id = ?",
                (now, now, recommendation_id),
            )

    def record_feedback(self, recommendation_id: str, kind: str) -> str | None:
        """Persist feedback_kind / feedback_at on the recommendation row.

        Returns the candidate_id for downstream side-effect handling, or None
        if the recommendation_id doesn't exist.
        """
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE opportunity_recommendations SET feedback_kind = ?, feedback_at = ?, "
                "updated_at = ? WHERE recommendation_id = ?",
                (kind, now, now, recommendation_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT candidate_id FROM opportunity_recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
        return str(row["candidate_id"]) if row else None

    def count_recent_feedback(
        self, candidate_id: str, kind: str, *, since_iso: str
    ) -> int:
        """Count recommendations for `candidate_id` whose `feedback_kind` matches
        and `feedback_at` is >= `since_iso`. Powers the 3-strikes auto-dismiss
        rule for 👎 feedback."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM opportunity_recommendations "
                "WHERE candidate_id = ? AND feedback_kind = ? AND feedback_at >= ?",
                (candidate_id, kind, since_iso),
            ).fetchone()
        return int(row["n"]) if row else 0

    def set_cooldown(self, candidate_id: str, until_iso: str | None) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE opportunity_candidates SET cooldown_until = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (until_iso, now, candidate_id),
            )
        return cursor.rowcount > 0

    def list_recent_recommendations(self, *, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT *
                    FROM opportunity_recommendations
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def list_recent_candidates(self, *, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT *
                    FROM opportunity_candidates
                    WHERE status = 'active'
                    ORDER BY updated_at DESC, heat_score DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )


def _decode_json_list(value: object) -> tuple[str, ...]:
    """Decode a JSON-encoded string list, tolerating missing/corrupt values."""
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str) and item.strip())


# Pre-fix DB rows can contain the same "網路佐證：…" sentence appended N
# times because WebOpportunityResearcher.enrich() previously concatenated
# the assessment reason on every cycle without dedup. Heal those rows on
# read so the next upsert writes a clean version. The forward fix in
# opportunity_agent.py (dedup-on-append) prevents new duplicates from
# accumulating, but legacy rows need this regex to recover.
_LEGACY_NETWORK_PROOF_RE = re.compile(
    r"(網路佐證：[^網]+?)(?:\s*\1)+",
)


def _normalize_legacy_reason(reason: str | None) -> str:
    if not reason:
        return reason or ""
    return _LEGACY_NETWORK_PROOF_RE.sub(r"\1", reason)


def _candidate_from_row(row: sqlite3.Row) -> OpportunityCandidate:
    metadata_raw = row["metadata_json"] or "{}"
    try:
        metadata = json.loads(metadata_raw)
    except json.JSONDecodeError:
        metadata = {}
    row_keys = row.keys() if hasattr(row, "keys") else ()
    aliases = _decode_json_list(row["aliases_json"]) if "aliases_json" in row_keys else ()
    related = (
        _decode_json_list(row["related_keywords_json"])
        if "related_keywords_json" in row_keys
        else ()
    )
    is_target = bool(row["is_target"]) if "is_target" in row_keys else False
    return OpportunityCandidate(
        candidate_id=row["candidate_id"],
        game=row["game"],
        product_type=row["product_type"] if "product_type" in row_keys else "other",
        title=row["title"],
        product_identifier=row["product_identifier"] if "product_identifier" in row_keys else None,
        search_query=row["search_query"],
        heat_score=float(row["heat_score"]),
        reason=_normalize_legacy_reason(row["reason"]),
        source_kind=row["source_kind"],
        source_url=row["source_url"],
        metadata=metadata,
        created_at=row["created_at"],
        aliases=aliases,
        related_keywords=related,
        is_target=is_target,
    )


def recommendation_id_for(listing: ListingOffer) -> str:
    return build_listing_key(listing.url)
