"""SQLite store for IP heat signals (C1).

Tracks multi-source IP popularity metrics over time:
  - x_mention    : X/Twitter hashtag/mention volume (7-day window)
  - reddit       : subreddit post/comment activity
  - google_trends: relative search interest (0-100 scale from Google)

One row per (ip_canonical, source, measured_at) — measured_at is truncated
to the hour so back-to-back runs don't create duplicate rows for the same
measurement window.

Percentile is pre-computed against the trailing 30-day history for the same
ip+source pair and stored alongside the raw value so the classifier can use
it directly without re-querying.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

SOURCES: tuple[str, ...] = ("x_mention", "reddit", "google_trends")

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ip_heat_signals (
    ip_canonical  TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    measured_at   TEXT    NOT NULL,  -- ISO8601, truncated to hour
    value         REAL    NOT NULL,  -- raw value (mention count / trend index)
    percentile    REAL,              -- 0-100 vs 30-day history; NULL until computed
    window_days   INTEGER NOT NULL DEFAULT 7,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (ip_canonical, source, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_heat_ip
    ON ip_heat_signals(ip_canonical);

CREATE INDEX IF NOT EXISTS idx_heat_source_time
    ON ip_heat_signals(source, measured_at DESC);

CREATE INDEX IF NOT EXISTS idx_heat_ip_source
    ON ip_heat_signals(ip_canonical, source, measured_at DESC);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _truncate_to_hour(dt: datetime) -> str:
    """Return ISO8601 string with minutes/seconds zeroed — one row per hour."""
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


@dataclass(frozen=True)
class HeatSignal:
    ip_canonical: str
    source: str            # one of SOURCES
    measured_at: str       # ISO8601 truncated to hour
    value: float
    percentile: float | None
    window_days: int
    created_at: str


class IpHeatStore:
    """SQLite persistence for ip_heat_signals."""

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
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Write ──────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        ip_canonical: str,
        source: str,
        value: float,
        window_days: int = 7,
        measured_at: datetime | None = None,
    ) -> HeatSignal:
        """Insert or replace a heat measurement. Returns the stored signal with
        percentile computed against the previous 30 days."""
        if source not in SOURCES:
            logger.warning("IpHeatStore.record: unknown source=%r", source)
        now = datetime.now(timezone.utc)
        at_str = _truncate_to_hour(measured_at if measured_at else now)
        created = _utc_now_iso()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO ip_heat_signals
                    (ip_canonical, source, measured_at, value, percentile, window_days, created_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(ip_canonical, source, measured_at) DO UPDATE SET
                    value      = excluded.value,
                    window_days = excluded.window_days
                """,
                (ip_canonical.strip().lower(), source, at_str, float(value), int(window_days), created),
            )

        # Recompute percentile based on 30-day history and update.
        percentile = self._compute_percentile(
            ip_canonical=ip_canonical.strip().lower(),
            source=source,
            current_value=float(value),
            days=30,
        )
        with self.connect() as conn:
            conn.execute(
                "UPDATE ip_heat_signals SET percentile = ? "
                "WHERE ip_canonical = ? AND source = ? AND measured_at = ?",
                (percentile, ip_canonical.strip().lower(), source, at_str),
            )
        return HeatSignal(
            ip_canonical=ip_canonical.strip().lower(),
            source=source,
            measured_at=at_str,
            value=float(value),
            percentile=percentile,
            window_days=int(window_days),
            created_at=created,
        )

    def _compute_percentile(
        self,
        *,
        ip_canonical: str,
        source: str,
        current_value: float,
        days: int = 30,
    ) -> float | None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).replace(microsecond=0).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT value FROM ip_heat_signals "
                "WHERE ip_canonical = ? AND source = ? AND measured_at >= ? "
                "ORDER BY measured_at DESC",
                (ip_canonical, source, cutoff),
            ).fetchall()
        values = [float(r["value"]) for r in rows]
        if not values:
            return None
        below = sum(1 for v in values if v <= current_value)
        return round(100.0 * below / len(values), 1)

    # ── Read ───────────────────────────────────────────────────────────────

    def latest(
        self,
        ip_canonical: str,
        source: str,
    ) -> HeatSignal | None:
        """Most recent measurement for an ip+source pair."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ip_heat_signals "
                "WHERE ip_canonical = ? AND source = ? "
                "ORDER BY measured_at DESC LIMIT 1",
                (ip_canonical.strip().lower(), source),
            ).fetchone()
        return _row_to_signal(row) if row else None

    def latest_for_ip(self, ip_canonical: str) -> list[HeatSignal]:
        """Most recent measurement per source for a given IP."""
        canon = ip_canonical.strip().lower()
        signals: list[HeatSignal] = []
        with self.connect() as conn:
            for source in SOURCES:
                row = conn.execute(
                    "SELECT * FROM ip_heat_signals "
                    "WHERE ip_canonical = ? AND source = ? "
                    "ORDER BY measured_at DESC LIMIT 1",
                    (canon, source),
                ).fetchone()
                if row:
                    signals.append(_row_to_signal(row))
        return signals

    def history(
        self,
        ip_canonical: str,
        source: str,
        *,
        days: int = 30,
    ) -> list[HeatSignal]:
        """Return all measurements for ip+source within the last `days` days."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).replace(microsecond=0).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ip_heat_signals "
                "WHERE ip_canonical = ? AND source = ? AND measured_at >= ? "
                "ORDER BY measured_at DESC",
                (ip_canonical.strip().lower(), source, cutoff),
            ).fetchall()
        return [_row_to_signal(r) for r in rows]

    def max_percentile_for_ip(self, ip_canonical: str) -> float | None:
        """Return the highest percentile across all sources for an IP.

        Used by the classifier to get a single 'is this IP hot right now?' score."""
        signals = self.latest_for_ip(ip_canonical)
        percentiles = [s.percentile for s in signals if s.percentile is not None]
        return max(percentiles) if percentiles else None

    def top_hot_ips(self, *, min_percentile: float = 70.0, limit: int = 20) -> list[tuple[str, float]]:
        """Return IPs with any source percentile ≥ min_percentile, ordered by max percentile desc.

        Returns list of (ip_canonical, max_percentile)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ip_canonical, MAX(percentile) AS max_pct
                FROM ip_heat_signals
                WHERE percentile >= ?
                GROUP BY ip_canonical
                ORDER BY max_pct DESC
                LIMIT ?
                """,
                (float(min_percentile), int(limit)),
            ).fetchall()
        return [(str(r["ip_canonical"]), float(r["max_pct"])) for r in rows]


def _row_to_signal(row: sqlite3.Row) -> HeatSignal:
    return HeatSignal(
        ip_canonical=str(row["ip_canonical"]),
        source=str(row["source"]),
        measured_at=str(row["measured_at"]),
        value=float(row["value"]),
        percentile=float(row["percentile"]) if row["percentile"] is not None else None,
        window_days=int(row["window_days"]),
        created_at=str(row["created_at"]),
    )
