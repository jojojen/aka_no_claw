"""SQLite store for historical TCG collab outcomes (D1).

Tracks the profitability of past IP × TCG collab releases:
  ip_canonical × tcg_game × announce_date → profit_pct_30d / profit_pct_180d

Used by CollabSimilarityProvider (D3) to surface historical precedents when
a new collab is announced, giving the classifier a data-driven prior like
「相似 7 案、平均 +42% 利潤、勝率 86%」.

One row per collab release. The primary key is
  sha1(ip_canonical|tcg_game|announce_date).
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

CREATE TABLE IF NOT EXISTS historical_collab_outcomes (
    case_id              TEXT    PRIMARY KEY,
    ip_canonical         TEXT    NOT NULL,
    tcg_game             TEXT    NOT NULL,
    product_name         TEXT    NOT NULL,
    announce_date        TEXT    NOT NULL,          -- ISO date YYYY-MM-DD
    lottery_open_date    TEXT,                      -- ISO date or NULL
    release_date         TEXT,                      -- ISO date or NULL
    lottery_price_jpy    REAL,
    secondary_30d_ratio  REAL,                      -- 30d secondary / lottery price
    secondary_180d_ratio REAL,                      -- 180d secondary / lottery price
    profit_pct_30d       REAL,                      -- (30d_price - lottery) / lottery × 100
    profit_pct_180d      REAL,                      -- same at 180d
    ip_heat_at_announce  REAL,                      -- IP heat percentile at announce time
    confidence           REAL    NOT NULL DEFAULT 0.5,  -- 0-1 data quality score
    source_urls_json     TEXT    NOT NULL DEFAULT '[]',
    notes                TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collab_ip
    ON historical_collab_outcomes(ip_canonical);

CREATE INDEX IF NOT EXISTS idx_collab_tcg
    ON historical_collab_outcomes(tcg_game);

CREATE INDEX IF NOT EXISTS idx_collab_ip_tcg
    ON historical_collab_outcomes(ip_canonical, tcg_game);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_case_id(ip_canonical: str, tcg_game: str, announce_date: str) -> str:
    """Deterministic primary key: sha1(ip|tcg|date)."""
    key = f"{ip_canonical.strip().lower()}|{tcg_game.strip().lower()}|{announce_date.strip()}"
    return sha1(key.encode()).hexdigest()[:16]


@dataclass
class CollabOutcome:
    case_id: str
    ip_canonical: str
    tcg_game: str
    product_name: str
    announce_date: str             # YYYY-MM-DD
    lottery_open_date: str | None
    release_date: str | None
    lottery_price_jpy: float | None
    secondary_30d_ratio: float | None
    secondary_180d_ratio: float | None
    profit_pct_30d: float | None
    profit_pct_180d: float | None
    ip_heat_at_announce: float | None
    confidence: float
    source_urls: list[str] = field(default_factory=list)
    notes: str | None = None
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)


class CollabOutcomesStore:
    """SQLite persistence for historical_collab_outcomes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _bootstrap(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Write ──────────────────────────────────────────────────────────────

    def upsert(self, outcome: CollabOutcome) -> CollabOutcome:
        """Insert or update a collab outcome. Returns the stored record."""
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO historical_collab_outcomes (
                    case_id, ip_canonical, tcg_game, product_name,
                    announce_date, lottery_open_date, release_date,
                    lottery_price_jpy,
                    secondary_30d_ratio, secondary_180d_ratio,
                    profit_pct_30d, profit_pct_180d,
                    ip_heat_at_announce, confidence,
                    source_urls_json, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    product_name         = excluded.product_name,
                    lottery_open_date    = excluded.lottery_open_date,
                    release_date         = excluded.release_date,
                    lottery_price_jpy    = excluded.lottery_price_jpy,
                    secondary_30d_ratio  = excluded.secondary_30d_ratio,
                    secondary_180d_ratio = excluded.secondary_180d_ratio,
                    profit_pct_30d       = excluded.profit_pct_30d,
                    profit_pct_180d      = excluded.profit_pct_180d,
                    ip_heat_at_announce  = excluded.ip_heat_at_announce,
                    confidence           = excluded.confidence,
                    source_urls_json     = excluded.source_urls_json,
                    notes                = excluded.notes,
                    updated_at           = ?
                """,
                (
                    outcome.case_id,
                    outcome.ip_canonical.strip().lower(),
                    outcome.tcg_game.strip().lower(),
                    outcome.product_name,
                    outcome.announce_date,
                    outcome.lottery_open_date,
                    outcome.release_date,
                    outcome.lottery_price_jpy,
                    outcome.secondary_30d_ratio,
                    outcome.secondary_180d_ratio,
                    outcome.profit_pct_30d,
                    outcome.profit_pct_180d,
                    outcome.ip_heat_at_announce,
                    outcome.confidence,
                    json.dumps(outcome.source_urls, ensure_ascii=False),
                    outcome.notes,
                    outcome.created_at,
                    now,
                    now,  # updated_at for ON CONFLICT branch
                ),
            )
        return self.get(outcome.case_id) or outcome

    def backfill_profit(
        self,
        case_id: str,
        *,
        secondary_30d_ratio: float | None = None,
        secondary_180d_ratio: float | None = None,
        profit_pct_30d: float | None = None,
        profit_pct_180d: float | None = None,
        confidence: float | None = None,
    ) -> bool:
        """Update profit columns for an existing case. Returns True if found."""
        now = _utc_now_iso()
        sets: list[str] = ["updated_at = ?"]
        params: list = [now]
        if secondary_30d_ratio is not None:
            sets.append("secondary_30d_ratio = ?"); params.append(secondary_30d_ratio)
        if secondary_180d_ratio is not None:
            sets.append("secondary_180d_ratio = ?"); params.append(secondary_180d_ratio)
        if profit_pct_30d is not None:
            sets.append("profit_pct_30d = ?"); params.append(profit_pct_30d)
        if profit_pct_180d is not None:
            sets.append("profit_pct_180d = ?"); params.append(profit_pct_180d)
        if confidence is not None:
            sets.append("confidence = ?"); params.append(confidence)
        params.append(case_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE historical_collab_outcomes SET {', '.join(sets)} WHERE case_id = ?",
                params,
            )
            return cur.rowcount > 0

    def delete(self, case_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM historical_collab_outcomes WHERE case_id = ?", (case_id,)
            )
            return cur.rowcount > 0

    # ── Read ───────────────────────────────────────────────────────────────

    def get(self, case_id: str) -> CollabOutcome | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM historical_collab_outcomes WHERE case_id = ?", (case_id,)
            ).fetchone()
        return _row_to_outcome(row) if row else None

    def list_by_ip(
        self,
        ip_canonical: str,
        *,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> list[CollabOutcome]:
        """Return all outcomes for an IP, sorted by announce_date desc."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM historical_collab_outcomes
                WHERE ip_canonical = ? AND confidence >= ?
                ORDER BY announce_date DESC
                LIMIT ?
                """,
                (ip_canonical.strip().lower(), min_confidence, limit),
            ).fetchall()
        return [_row_to_outcome(r) for r in rows]

    def list_by_tcg(
        self,
        tcg_game: str,
        *,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> list[CollabOutcome]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM historical_collab_outcomes
                WHERE tcg_game = ? AND confidence >= ?
                ORDER BY announce_date DESC
                LIMIT ?
                """,
                (tcg_game.strip().lower(), min_confidence, limit),
            ).fetchall()
        return [_row_to_outcome(r) for r in rows]

    def list_all(
        self,
        *,
        min_confidence: float = 0.0,
        has_profit_data: bool = False,
        limit: int = 200,
    ) -> list[CollabOutcome]:
        """Return all outcomes, optionally filtered to only those with profit data."""
        extra = "AND profit_pct_180d IS NOT NULL" if has_profit_data else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM historical_collab_outcomes
                WHERE confidence >= ? {extra}
                ORDER BY announce_date DESC
                LIMIT ?
                """,
                (min_confidence, limit),
            ).fetchall()
        return [_row_to_outcome(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM historical_collab_outcomes"
            ).fetchone()[0]


def _row_to_outcome(row: sqlite3.Row) -> CollabOutcome:
    try:
        source_urls = json.loads(row["source_urls_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        source_urls = []
    return CollabOutcome(
        case_id=str(row["case_id"]),
        ip_canonical=str(row["ip_canonical"]),
        tcg_game=str(row["tcg_game"]),
        product_name=str(row["product_name"]),
        announce_date=str(row["announce_date"]),
        lottery_open_date=row["lottery_open_date"],
        release_date=row["release_date"],
        lottery_price_jpy=row["lottery_price_jpy"],
        secondary_30d_ratio=row["secondary_30d_ratio"],
        secondary_180d_ratio=row["secondary_180d_ratio"],
        profit_pct_30d=row["profit_pct_30d"],
        profit_pct_180d=row["profit_pct_180d"],
        ip_heat_at_announce=row["ip_heat_at_announce"],
        confidence=float(row["confidence"]),
        source_urls=source_urls,
        notes=row["notes"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
