"""Canonical Price Observation Ledger & Market Snapshot API (issue #13).

Price evidence is scattered across price_monitor_bot, marketplace scraping,
official store pricing, opportunity candidates and intelligence signals — all
heterogeneous and hard to compare. This module is the canonical layer on top of
the #12 entity registry: every price datum becomes an immutable
``PriceObservation`` keyed by a canonical ``entity_id`` (#12) and a ``source_id``
(#9), so the system can answer "what is the latest price / range / source mix /
trend?" for one item.

Provenance chain (Deliverable 3): ``entity_id -> observation -> source_id ->
domain_id``. The ledger stores ``entity_id`` and ``source_id``; ``source_id``
resolves to a ``domain_id`` via the #9/#11 source registry
(``KnowledgeDatabase.get_source(source_id).domain_id``), so no domain data is
duplicated here.

Observations are immutable historical records (Deliverable 1): the id is a
deterministic hash of the observation's identifying fields, so re-ingesting the
same datum is a no-op rather than a duplicate, while genuinely new observations
always append. This keeps the full history queryable for the time-series
foundation (Deliverable 4). Per the issue's Non-goals, this layer does NOT
compute fair value, liquidity curves or forecasts — it only stores and
summarizes; those are follow-ups that read this ledger.

Ingestion path (Deliverable 5): producers resolve their raw title/code to an
``entity_id`` via ``MarketEntityRegistry.resolve_entity`` and their URL to a
``source_id`` via ``KnowledgeDatabase.intern_source``, then call
``record_observation``. Unresolved entities should not be recorded (the ledger
requires a canonical key); callers hold such data until resolution improves.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha1
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# ── Deliverable 1: quote-type vocabulary ─────────────────────────────────────
QUOTE_TYPES: tuple[str, ...] = (
    "listing",
    "sold",
    "buyback",
    "official_retail",
    "auction_bid",
    "auction_result",
    "reference",
)
DEFAULT_QUOTE_TYPE = "listing"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snap(value: str | None, vocab: tuple[str, ...], default: str) -> str:
    token = (value or "").strip().lower()
    return token if token in vocab else default


def _to_decimal(value) -> Decimal:
    """Coerce a price to Decimal without binary-float drift (str() first)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid price_amount: {value!r}") from exc


def _norm_currency(value: str | None) -> str:
    return (value or "JPY").strip().upper() or "JPY"


def build_observation_id(
    *,
    entity_id: str,
    source_id: str,
    observed_at: str,
    price_amount: str,
    quote_type: str,
    condition: str | None = None,
) -> str:
    """Deterministic id so the same observation interned twice collapses to one
    immutable row, while any difference (price, time, condition, …) is a new
    observation."""
    key = "|".join((
        entity_id, source_id, observed_at, price_amount,
        quote_type, condition or "",
    ))
    return "obs_" + sha1(key.encode("utf-8")).hexdigest()[:16]


# ── Models ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PriceObservation:
    observation_id: str
    entity_id: str
    source_id: str
    observed_at: str           # ISO-8601 timestamp
    currency: str
    price_amount: Decimal
    quote_type: str
    condition: str | None = None
    quantity: int | None = None
    confidence: float = 1.0
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    entity_id: str
    currency: str | None
    count: int
    min_price: Decimal | None
    max_price: Decimal | None
    median_price: Decimal | None
    latest_price: Decimal | None
    latest_observed_at: str | None
    freshness_seconds: float | None      # age of the newest observation
    source_ids: tuple[str, ...]          # contributing sources (provenance)
    quote_type_mix: dict[str, int]
    latest_observations: tuple[PriceObservation, ...]


@dataclass(frozen=True, slots=True)
class PriceBucket:
    bucket: str                # e.g. "2026-06-20" (day) or "2026-W25" (week)
    count: int
    min_price: Decimal
    max_price: Decimal
    median_price: Decimal
    avg_price: Decimal


# ── Persistence ──────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_observations (
    observation_id TEXT PRIMARY KEY,
    entity_id      TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    currency       TEXT NOT NULL,
    price_amount   TEXT NOT NULL,   -- Decimal as string, exact (no float drift)
    quote_type     TEXT NOT NULL,
    condition      TEXT,
    quantity       INTEGER,
    confidence     REAL NOT NULL DEFAULT 1.0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_obs_entity ON price_observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_price_obs_entity_time
    ON price_observations(entity_id, observed_at);
"""

# Default number of newest observations a snapshot embeds.
_SNAPSHOT_LATEST_N = 10


class PriceLedger:
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

    # ── Deliverable 1: immutable ingestion ───────────────────────────────────
    def record_observation(
        self,
        *,
        entity_id: str,
        source_id: str,
        price_amount,
        observed_at: str | None = None,
        currency: str | None = None,
        quote_type: str = DEFAULT_QUOTE_TYPE,
        condition: str | None = None,
        quantity: int | None = None,
        confidence: float = 1.0,
    ) -> PriceObservation:
        """Append an immutable price observation; returns the stored record.

        Re-recording an identical observation (same entity/source/time/price/
        quote/condition) is a no-op that returns the existing row — the id is a
        deterministic hash of those fields. Requires a non-empty entity_id and
        source_id: the ledger is keyed on canonical provenance."""
        entity_id = (entity_id or "").strip()
        source_id = (source_id or "").strip()
        if not entity_id or not source_id:
            raise ValueError("record_observation requires entity_id and source_id")
        amount = _to_decimal(price_amount)
        cur = _norm_currency(currency)
        qtype = _snap(quote_type, QUOTE_TYPES, DEFAULT_QUOTE_TYPE)
        when = (observed_at or "").strip() or _utc_now_iso()
        amount_str = format(amount, "f")  # canonical, non-scientific
        obs_id = build_observation_id(
            entity_id=entity_id, source_id=source_id, observed_at=when,
            price_amount=amount_str, quote_type=qtype, condition=condition,
        )
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO price_observations (
                    observation_id, entity_id, source_id, observed_at, currency,
                    price_amount, quote_type, condition, quantity, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO NOTHING
                """,
                (obs_id, entity_id, source_id, when, cur, amount_str, qtype,
                 condition, quantity, max(0.0, min(1.0, float(confidence))), now),
            )
            row = conn.execute(
                "SELECT * FROM price_observations WHERE observation_id = ?", (obs_id,)
            ).fetchone()
        return _row_to_obs(row)

    def get_observation(self, observation_id: str) -> PriceObservation | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM price_observations WHERE observation_id = ?",
                ((observation_id or "").strip(),),
            ).fetchone()
        return _row_to_obs(row) if row else None

    def observations_for(
        self,
        entity_id: str,
        *,
        currency: str | None = None,
        quote_type: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[PriceObservation]:
        """Historical observations for an entity, newest first (Deliverable 4:
        history stays queryable). Optional currency / quote_type / since filters."""
        clauses = ["entity_id = ?"]
        params: list = [(entity_id or "").strip()]
        if currency:
            clauses.append("currency = ?")
            params.append(_norm_currency(currency))
        if quote_type:
            clauses.append("quote_type = ?")
            params.append(_snap(quote_type, QUOTE_TYPES, DEFAULT_QUOTE_TYPE))
        if since:
            clauses.append("observed_at >= ?")
            params.append(since)
        sql = (
            "SELECT * FROM price_observations WHERE " + " AND ".join(clauses)
            + " ORDER BY observed_at DESC, observation_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_obs(r) for r in rows]

    # ── Deliverable 2/3: market snapshot with provenance ─────────────────────
    def get_market_snapshot(
        self,
        entity_id: str,
        *,
        currency: str | None = None,
        quote_type: str | None = None,
        latest_n: int = _SNAPSHOT_LATEST_N,
    ) -> MarketSnapshot:
        """Summarize current market state for an entity from its observations.

        Sparse-safe: an entity with no observations returns a snapshot with
        count 0 and None statistics rather than raising. Mixing currencies
        produces meaningless min/max, so pass ``currency`` to scope; otherwise
        the snapshot reports across all currencies and leaves ``currency`` None."""
        obs = self.observations_for(entity_id, currency=currency, quote_type=quote_type)
        if not obs:
            return MarketSnapshot(
                entity_id=(entity_id or "").strip(), currency=_norm_currency(currency) if currency else None,
                count=0, min_price=None, max_price=None, median_price=None,
                latest_price=None, latest_observed_at=None, freshness_seconds=None,
                source_ids=(), quote_type_mix={}, latest_observations=(),
            )
        prices = [o.price_amount for o in obs]
        currencies = {o.currency for o in obs}
        newest = obs[0]  # observations_for is newest-first
        mix: dict[str, int] = {}
        for o in obs:
            mix[o.quote_type] = mix.get(o.quote_type, 0) + 1
        source_ids = tuple(dict.fromkeys(o.source_id for o in obs))  # stable, unique
        return MarketSnapshot(
            entity_id=newest.entity_id,
            currency=newest.currency if len(currencies) == 1 else (
                _norm_currency(currency) if currency else None),
            count=len(obs),
            min_price=min(prices),
            max_price=max(prices),
            median_price=_median_decimal(prices),
            latest_price=newest.price_amount,
            latest_observed_at=newest.observed_at,
            freshness_seconds=_age_seconds(newest.observed_at),
            source_ids=source_ids,
            quote_type_mix=mix,
            latest_observations=tuple(obs[:max(0, latest_n)]),
        )

    # ── Deliverable 4: time-series aggregation path ──────────────────────────
    def aggregate_series(
        self,
        entity_id: str,
        *,
        bucket: str = "day",
        currency: str | None = None,
        quote_type: str | None = None,
    ) -> list[PriceBucket]:
        """Bucketed price aggregates over history, oldest bucket first. ``bucket``
        is ``day`` or ``week`` (ISO week). The data model keeps every raw
        observation, so finer/other buckets can be added later without migration."""
        if bucket not in ("day", "week"):
            raise ValueError("bucket must be 'day' or 'week'")
        obs = self.observations_for(entity_id, currency=currency, quote_type=quote_type)
        groups: dict[str, list[Decimal]] = {}
        for o in obs:
            key = _bucket_key(o.observed_at, bucket)
            if key is None:
                continue
            groups.setdefault(key, []).append(o.price_amount)
        out: list[PriceBucket] = []
        for key in sorted(groups):
            ps = groups[key]
            out.append(PriceBucket(
                bucket=key, count=len(ps), min_price=min(ps), max_price=max(ps),
                median_price=_median_decimal(ps), avg_price=_avg_decimal(ps),
            ))
        return out


# ── helpers ──────────────────────────────────────────────────────────────────
def _median_decimal(values: list[Decimal]) -> Decimal:
    """Median preserving Decimal: for an even count, average the two middle
    values (statistics.median on Decimal can return a float)."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / Decimal(2)


def _avg_decimal(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / Decimal(len(values))) if values else Decimal(0)


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_seconds(observed_at: str) -> float | None:
    dt = _parse_iso(observed_at)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _bucket_key(observed_at: str, bucket: str) -> str | None:
    dt = _parse_iso(observed_at)
    if dt is None:
        return None
    if bucket == "day":
        return dt.date().isoformat()
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _row_to_obs(row: sqlite3.Row) -> PriceObservation:
    return PriceObservation(
        observation_id=row["observation_id"],
        entity_id=row["entity_id"],
        source_id=row["source_id"],
        observed_at=row["observed_at"],
        currency=row["currency"],
        price_amount=_to_decimal(row["price_amount"]),
        quote_type=row["quote_type"],
        condition=row["condition"],
        quantity=row["quantity"],
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
    )
