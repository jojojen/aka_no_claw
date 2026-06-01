"""CollabProfitBackfiller (D4).

Tracks user-purchased collabs (💰) and auto-backfills secondary-market prices
at 30 and 180 days post-release into CollabOutcomesStore.

Usage:
    backfiller = CollabProfitBackfiller(
        store=collab_outcomes_store,
        db_path=Path("data/collab_backfill.sqlite3"),
        price_fetcher=my_mercari_avg_price_fn,
    )
    # when user clicks 💰:
    backfiller.record_purchase(case_id, release_date="2024-09-27")
    # in daily cron:
    updated = backfiller.run_pending()

The price_fetcher callable signature:
    (product_name: str, lottery_price_jpy: float) -> float | None
Should return the average secondary market price in JPY, or None if
insufficient data (fewer than 3 sold listings).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from openclaw_adapter.collab_outcomes_store import CollabOutcomesStore

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collab_backfill_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT    NOT NULL,
    due_at       TEXT    NOT NULL,   -- ISO date YYYY-MM-DD
    window_days  INTEGER NOT NULL,   -- 30 or 180
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending / done / skipped
    attempted_at TEXT,
    result_price REAL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bq_case ON collab_backfill_queue(case_id);
CREATE INDEX IF NOT EXISTS idx_bq_due  ON collab_backfill_queue(due_at, status);
"""

PriceFetcher = Callable[[str, float], float | None]


@dataclass
class BackfillResult:
    case_id: str
    window_days: int
    secondary_price_jpy: float | None
    profit_pct: float | None
    ok: bool


class CollabProfitBackfiller:
    """Records user purchases and auto-backfills profit data post-release."""

    def __init__(
        self,
        store: CollabOutcomesStore,
        *,
        db_path: str | Path,
        price_fetcher: PriceFetcher | None = None,
    ) -> None:
        self._store = store
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._price_fetcher = price_fetcher
        self._bootstrap()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _bootstrap(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Write ──────────────────────────────────────────────────────────────

    def record_purchase(self, case_id: str, release_date: str | None = None) -> bool:
        """Schedule 30d and 180d backfill tasks for this case.

        ``release_date`` is YYYY-MM-DD; if None the release_date stored in the
        outcome record is used.  Returns True if tasks were scheduled.
        """
        outcome = self._store.get(case_id)
        if outcome is None:
            logger.warning("record_purchase: unknown case_id=%s", case_id)
            return False

        rd = release_date or outcome.release_date
        if rd is None:
            logger.warning("record_purchase: no release_date for case_id=%s", case_id)
            return False

        try:
            release = date.fromisoformat(rd)
        except ValueError:
            logger.warning("record_purchase: bad release_date %s for case_id=%s", rd, case_id)
            return False

        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._connect() as conn:
            for window in (30, 180):
                due = release + timedelta(days=window)
                # avoid duplicate scheduling for the same (case_id, window_days)
                existing = conn.execute(
                    "SELECT id FROM collab_backfill_queue "
                    "WHERE case_id=? AND window_days=? AND status='pending'",
                    (case_id, window),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO collab_backfill_queue "
                    "(case_id, due_at, window_days, created_at) VALUES (?, ?, ?, ?)",
                    (case_id, due.isoformat(), window, now_iso),
                )
        logger.info("record_purchase: scheduled backfills for case_id=%s release=%s", case_id, rd)
        return True

    # ── Process ────────────────────────────────────────────────────────────

    def run_pending(self, *, as_of: date | None = None) -> list[BackfillResult]:
        """Process all due tasks. Returns list of BackfillResult."""
        today = as_of or date.today()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, case_id, window_days FROM collab_backfill_queue "
                "WHERE status='pending' AND due_at <= ? ORDER BY due_at",
                (today.isoformat(),),
            ).fetchall()

        results: list[BackfillResult] = []
        for row in rows:
            result = self._process_one(row["id"], row["case_id"], row["window_days"])
            results.append(result)
        return results

    def _process_one(self, queue_id: int, case_id: str, window_days: int) -> BackfillResult:
        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        outcome = self._store.get(case_id)

        secondary_price: float | None = None
        profit_pct: float | None = None
        ok = False

        if outcome is None:
            logger.warning("backfill: case_id=%s not found in store", case_id)
            self._mark(queue_id, "skipped", now_iso, None)
            return BackfillResult(case_id, window_days, None, None, False)

        if self._price_fetcher is None:
            logger.warning("backfill: no price_fetcher configured, skipping case_id=%s", case_id)
            self._mark(queue_id, "skipped", now_iso, None)
            return BackfillResult(case_id, window_days, None, None, False)

        try:
            secondary_price = self._price_fetcher(outcome.product_name, outcome.lottery_price_jpy or 0.0)
        except Exception:
            logger.exception("backfill price_fetcher error for case_id=%s", case_id)
            self._mark(queue_id, "skipped", now_iso, None)
            return BackfillResult(case_id, window_days, None, None, False)

        if secondary_price is not None and outcome.lottery_price_jpy:
            ratio = secondary_price / outcome.lottery_price_jpy
            profit_pct = round((ratio - 1.0) * 100.0, 1)
            kwargs: dict = {"confidence": min(outcome.confidence + 0.1, 1.0)}
            if window_days == 30:
                kwargs["secondary_30d_ratio"] = round(ratio, 3)
                kwargs["profit_pct_30d"] = profit_pct
            else:
                kwargs["secondary_180d_ratio"] = round(ratio, 3)
                kwargs["profit_pct_180d"] = profit_pct
            self._store.backfill_profit(case_id, **kwargs)
            ok = True
            logger.info(
                "backfill done case_id=%s window=%dd secondary=%.0f profit=%.1f%%",
                case_id, window_days, secondary_price, profit_pct,
            )

        self._mark(queue_id, "done" if ok else "skipped", now_iso, secondary_price)
        return BackfillResult(case_id, window_days, secondary_price, profit_pct, ok)

    def _mark(self, queue_id: int, status: str, now_iso: str, price: float | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE collab_backfill_queue SET status=?, attempted_at=?, result_price=? WHERE id=?",
                (status, now_iso, price, queue_id),
            )

    # ── Read ───────────────────────────────────────────────────────────────

    def pending_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM collab_backfill_queue WHERE status='pending'"
            ).fetchone()[0]

    def list_pending(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT case_id, window_days, due_at FROM collab_backfill_queue "
                "WHERE status='pending' ORDER BY due_at"
            ).fetchall()
        return [dict(r) for r in rows]
