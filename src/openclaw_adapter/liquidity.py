"""Sold-Comp Harvesting & Liquidity Curves (issue #14).

A listing price is only an *asking* price; it does not prove the item transacts.
This layer adds the demonstrated-demand side on top of the #12 entity registry
and the #13 price ledger, so the system can answer "does this sell, how fast,
how often, and what discount unlocks liquidity?" — the inputs a future Fair Value
Engine / Opportunity Scorer need.

Layering (single source of truth, no duplicated price storage):

- #13 ``PriceLedger`` remains the canonical store of *all* price points (listing,
  sold, reference, …). The market snapshot already exposes active-listing prices.
- This module's ``SoldCompLedger`` is the *lifecycle-aware* transaction store: a
  ``SoldComparable`` carries fields the generic ledger intentionally omits —
  ``listed_at`` / ``listing_id`` (so time-to-sale is derivable) and the
  normalized ``marketplace``. Entity linkage is by ``entity_id`` (#12).
- ``compute_liquidity_metrics`` / ``build_liquidity_curve`` *compose* the two:
  sold comps come from here, active-listing context is passed in from the #13
  snapshot. Nothing is recomputed from raw price storage twice.

Per the issue's Non-goals, this layer does NOT compute fair value, forecasts, or
trading decisions — it quantifies liquidity and exposes it for those follow-ups.
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
from typing import Iterator, Sequence

logger = logging.getLogger(__name__)


# ── shared coercion helpers (mirror #13 price_ledger conventions) ─────────────
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_decimal(value) -> Decimal:
    """Coerce a price to Decimal without binary-float drift (str() first)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid price: {value!r}") from exc


def _norm_currency(value: str | None) -> str:
    return (value or "JPY").strip().upper() or "JPY"


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_ts(ts: str) -> str:
    """Canonical UTC ISO-8601 so stored timestamps sort chronologically by raw
    string regardless of the offset they arrived in (the #13 lesson)."""
    dt = _parse_iso(ts)
    if dt is None:
        return (ts or "").strip()
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ── Deliverable 4: marketplace integration ───────────────────────────────────
# Maps a source hint (host fragment or source_id prefix) to a normalized
# marketplace key, so heterogeneous scrapers feed one vocabulary. Documented and
# extensible: adding a marketplace is one line here plus a field-alias entry.
MARKETPLACES: tuple[str, ...] = (
    "mercari",
    "rakuma",
    "yahoo_auctions",
    "surugaya_buyback",
    "yuyutei",
)
MARKETPLACE_SOURCE_MAP: dict[str, str] = {
    "mercari.com": "mercari",
    "jp.mercari.com": "mercari",
    "fril.jp": "rakuma",
    "rakuma": "rakuma",
    "auctions.yahoo.co.jp": "yahoo_auctions",
    "yahoo": "yahoo_auctions",
    "suruga-ya.jp": "surugaya_buyback",
    "surugaya": "surugaya_buyback",
    "yuyu-tei.jp": "yuyutei",
}
# Per-marketplace raw→canonical field aliases. Each marketplace exposes the same
# economic facts under different keys; normalization collapses them to the
# SoldComparable contract. ``buyback`` shops (Suruga-ya) quote a *buy* price that
# IS a demonstrated transaction the shop will honor, so it counts as a sold comp.
MARKETPLACE_FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "mercari": {
        "sold_price": ("price", "sold_price", "soldPrice"),
        "sold_at": ("sold_at", "updated", "soldDate"),
        "listed_at": ("created", "listed_at", "created_at"),
        "listing_id": ("id", "item_id", "listing_id"),
        "condition": ("condition", "item_condition"),
    },
    "rakuma": {
        "sold_price": ("price", "sold_price"),
        "sold_at": ("sold_at", "sold_time"),
        "listed_at": ("created_at", "listed_at"),
        "listing_id": ("id", "item_id"),
        "condition": ("condition",),
    },
    "yahoo_auctions": {
        "sold_price": ("winning_bid", "price", "endingPrice"),
        "sold_at": ("end_time", "sold_at", "endTime"),
        "listed_at": ("start_time", "listed_at"),
        "listing_id": ("auction_id", "id"),
        "condition": ("condition",),
    },
    "surugaya_buyback": {
        "sold_price": ("buyback_price", "buy_price", "price"),
        "sold_at": ("quoted_at", "sold_at", "updated_at"),
        "listed_at": ("listed_at",),
        "listing_id": ("product_id", "id"),
        "condition": ("condition",),
    },
    "yuyutei": {
        "sold_price": ("buy_price", "price"),
        "sold_at": ("quoted_at", "sold_at"),
        "listed_at": ("listed_at",),
        "listing_id": ("id",),
        "condition": ("condition",),
    },
}


def resolve_marketplace(source_hint: str | None) -> str | None:
    """Map a host / source hint to a normalized marketplace key, or None."""
    hint = (source_hint or "").strip().lower()
    if not hint:
        return None
    if hint in MARKETPLACES:
        return hint
    for needle, marketplace in MARKETPLACE_SOURCE_MAP.items():
        if needle in hint:
            return marketplace
    return None


def _first_present(raw: dict, keys: Sequence[str]):
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


# ── Deliverable 1: sold-comparable model + deterministic id ───────────────────
@dataclass(frozen=True, slots=True)
class SoldComparable:
    sold_comp_id: str
    entity_id: str
    source_id: str
    sold_price: Decimal
    currency: str
    sold_at: str                 # UTC ISO-8601
    condition: str | None = None
    quantity: int | None = None
    listed_at: str | None = None  # lifecycle: when the listing first appeared
    listing_id: str | None = None
    marketplace: str | None = None
    confidence: float = 1.0
    created_at: str | None = None

    @property
    def time_to_sale_days(self) -> float | None:
        """Days from listing to sale, when both ends are known; else None."""
        if not self.listed_at:
            return None
        start, end = _parse_iso(self.listed_at), _parse_iso(self.sold_at)
        if start is None or end is None:
            return None
        return max(0.0, (end - start).total_seconds() / 86400.0)


def build_sold_comp_id(
    *,
    entity_id: str,
    source_id: str,
    sold_at: str,
    sold_price: str,
    currency: str | None = None,
    listing_id: str | None = None,
) -> str:
    """Deterministic id: re-harvesting the same sale collapses to one immutable
    row. ``currency`` is part of the identity (the #13 lesson — same instant in
    two currencies must stay distinct), and ``listing_id`` distinguishes two
    different listings of the same item that sold at the same price/instant."""
    key = "|".join((
        entity_id, source_id, sold_at, sold_price,
        _norm_currency(currency), listing_id or "",
    ))
    return "sc_" + sha1(key.encode("utf-8")).hexdigest()[:16]


def normalize_sold_event(raw: dict, *, marketplace: str) -> dict:
    """Normalize a marketplace-specific sold record into ``record_sold_comp``
    kwargs. The caller supplies the resolved ``entity_id`` (#12) and ``source_id``
    (#9) — those are canonical-resolution concerns, not raw-field concerns."""
    mk = resolve_marketplace(marketplace) or marketplace
    aliases = MARKETPLACE_FIELD_ALIASES.get(mk, {})
    out: dict = {"marketplace": mk}
    for canonical, keys in aliases.items():
        value = _first_present(raw, keys)
        if value is not None:
            out[canonical] = value
    # Allow raw to carry resolved ids / currency straight through.
    for passthrough in ("entity_id", "source_id", "currency", "quantity", "confidence"):
        if raw.get(passthrough) is not None:
            out[passthrough] = raw[passthrough]
    return out


# ── persistence ───────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sold_comparables (
    sold_comp_id  TEXT PRIMARY KEY,
    entity_id     TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    sold_price    TEXT NOT NULL,   -- Decimal as string, exact (no float drift)
    currency      TEXT NOT NULL,
    sold_at       TEXT NOT NULL,   -- UTC-normalized ISO-8601
    condition     TEXT,
    quantity      INTEGER,
    listed_at     TEXT,
    listing_id    TEXT,
    marketplace   TEXT,
    confidence    REAL NOT NULL DEFAULT 1.0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sold_comp_entity ON sold_comparables(entity_id);
CREATE INDEX IF NOT EXISTS idx_sold_comp_entity_time
    ON sold_comparables(entity_id, sold_at);
"""


class SoldCompLedger:
    """Immutable, append-only store of demonstrated sales (Deliverable 1)."""

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

    def record_sold_comp(
        self,
        *,
        entity_id: str,
        source_id: str,
        sold_price,
        sold_at: str | None = None,
        currency: str | None = None,
        condition: str | None = None,
        quantity: int | None = None,
        listed_at: str | None = None,
        listing_id: str | None = None,
        marketplace: str | None = None,
        confidence: float = 1.0,
    ) -> SoldComparable:
        """Append an immutable sold comparable; returns the stored record.
        Re-recording an identical sale is a no-op (deterministic id). Requires a
        canonical entity_id and source_id."""
        entity_id = (entity_id or "").strip()
        source_id = (source_id or "").strip()
        if not entity_id or not source_id:
            raise ValueError("record_sold_comp requires entity_id and source_id")
        amount = _to_decimal(sold_price)
        cur = _norm_currency(currency)
        when = _normalize_ts((sold_at or "").strip() or _utc_now_iso())
        listed = _normalize_ts(listed_at) if listed_at else None
        amount_str = format(amount, "f")
        mk = resolve_marketplace(marketplace) or marketplace
        sc_id = build_sold_comp_id(
            entity_id=entity_id, source_id=source_id, sold_at=when,
            sold_price=amount_str, currency=cur, listing_id=listing_id,
        )
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sold_comparables (
                    sold_comp_id, entity_id, source_id, sold_price, currency,
                    sold_at, condition, quantity, listed_at, listing_id,
                    marketplace, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sold_comp_id) DO NOTHING
                """,
                (sc_id, entity_id, source_id, amount_str, cur, when, condition,
                 quantity, listed, listing_id, mk,
                 max(0.0, min(1.0, float(confidence))), now),
            )
            row = conn.execute(
                "SELECT * FROM sold_comparables WHERE sold_comp_id = ?", (sc_id,)
            ).fetchone()
        return _row_to_sold_comp(row)

    def get_sold_comp(self, sold_comp_id: str) -> SoldComparable | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sold_comparables WHERE sold_comp_id = ?",
                ((sold_comp_id or "").strip(),),
            ).fetchone()
        return _row_to_sold_comp(row) if row else None

    def sold_comparables_for(
        self,
        entity_id: str,
        *,
        currency: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[SoldComparable]:
        """Sold history for an entity, newest first (Deliverable 1: sold events
        remain historically queryable)."""
        clauses = ["entity_id = ?"]
        params: list = [(entity_id or "").strip()]
        if currency:
            clauses.append("currency = ?")
            params.append(_norm_currency(currency))
        if since:
            clauses.append("sold_at >= ?")
            params.append(_normalize_ts(since))
        sql = (
            "SELECT * FROM sold_comparables WHERE " + " AND ".join(clauses)
            + " ORDER BY sold_at DESC, sold_comp_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_sold_comp(r) for r in rows]


def _row_to_sold_comp(row: sqlite3.Row) -> SoldComparable:
    return SoldComparable(
        sold_comp_id=row["sold_comp_id"],
        entity_id=row["entity_id"],
        source_id=row["source_id"],
        sold_price=_to_decimal(row["sold_price"]),
        currency=row["currency"],
        sold_at=row["sold_at"],
        condition=row["condition"],
        quantity=row["quantity"],
        listed_at=row["listed_at"],
        listing_id=row["listing_id"],
        marketplace=row["marketplace"],
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
    )


# ── Deliverable 2: liquidity metrics ──────────────────────────────────────────
# Vocabulary of baseline liquidity metrics derivable from sold history (+ active
# listing context from the #13 snapshot). Keep this the single naming authority.
LIQUIDITY_METRICS: tuple[str, ...] = (
    "sales_per_day",
    "sales_per_week",
    "inventory_turnover",
    "median_time_to_sale_days",
    "sell_through_rate",
    "listing_to_sale_ratio",
)


@dataclass(frozen=True, slots=True)
class LiquidityMetrics:
    entity_id: str
    currency: str | None
    window_days: int
    sold_count: int
    sales_per_day: float | None
    sales_per_week: float | None
    inventory_turnover: float | None
    median_time_to_sale_days: float | None
    sell_through_rate: float | None
    listing_to_sale_ratio: Decimal | None
    observed_span_days: float | None


def compute_liquidity_metrics(
    entity_id: str,
    sold_comps: Sequence[SoldComparable],
    *,
    window_days: int = 30,
    currency: str | None = None,
    active_listing_count: int | None = None,
    active_listing_prices: Sequence[Decimal | int | str] | None = None,
) -> LiquidityMetrics:
    """Derive liquidity metrics for an entity. ``sold_comps`` is the sold history
    (typically ``SoldCompLedger.sold_comparables_for``); active-listing context is
    passed in from the #13 market snapshot. Sparse-safe: with no sales, rates are
    0.0 and ratios that need data are None rather than raising."""
    eid = (entity_id or "").strip()
    window_days = max(1, int(window_days))
    cur = _norm_currency(currency) if currency else None

    in_scope = [
        sc for sc in sold_comps
        if (cur is None or sc.currency == cur) and _within_window(sc.sold_at, window_days)
    ]
    # When unscoped, only report a currency if the sales agree on one.
    currencies = {sc.currency for sc in in_scope}
    effective_currency = cur or (next(iter(currencies)) if len(currencies) == 1 else None)

    sold_count = len(in_scope)
    sales_per_day = sold_count / window_days
    sales_per_week = sales_per_day * 7.0

    tts = [sc.time_to_sale_days for sc in in_scope if sc.time_to_sale_days is not None]
    median_tts = statistics.median(tts) if tts else None

    sell_through = None
    if active_listing_count is not None:
        denom = sold_count + max(0, int(active_listing_count))
        sell_through = (sold_count / denom) if denom else None

    inventory_turnover = None
    if active_listing_count is not None and int(active_listing_count) > 0:
        inventory_turnover = sold_count / int(active_listing_count)

    listing_to_sale = None
    if active_listing_prices and in_scope:
        actives = [_to_decimal(p) for p in active_listing_prices]
        if actives:
            sold_prices = [sc.sold_price for sc in in_scope]
            sold_median = _median_decimal(sold_prices)
            if sold_median > 0:
                listing_to_sale = _median_decimal(actives) / sold_median

    span = _span_days([sc.sold_at for sc in in_scope]) if in_scope else None

    return LiquidityMetrics(
        entity_id=eid,
        currency=effective_currency,
        window_days=window_days,
        sold_count=sold_count,
        sales_per_day=sales_per_day,
        sales_per_week=sales_per_week,
        inventory_turnover=inventory_turnover,
        median_time_to_sale_days=median_tts,
        sell_through_rate=sell_through,
        listing_to_sale_ratio=listing_to_sale,
        observed_span_days=span,
    )


# ── Deliverable 3: liquidity curve ────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class LiquidityCurvePoint:
    price_ceiling: Decimal          # "if priced at or below this…"
    probability_of_sale: float      # fraction of observed listings ≤ ceiling that sold
    expected_days_to_sale: float | None
    sample_count: int               # observations (sold + active) at ≤ ceiling


@dataclass(frozen=True, slots=True)
class LiquidityCurve:
    entity_id: str
    currency: str | None
    points: tuple[LiquidityCurvePoint, ...]
    sample_count: int

    @property
    def has_data(self) -> bool:
        return self.sample_count > 0


def build_liquidity_curve(
    entity_id: str,
    sold_comps: Sequence[SoldComparable],
    *,
    active_listing_prices: Sequence[Decimal | int | str] | None = None,
    currency: str | None = None,
    max_points: int = 8,
) -> LiquidityCurve:
    """Cumulative liquidity curve: at each price ceiling, the probability that a
    listing priced at or below it transacted, plus the expected days-to-sale among
    those that did. Combines sold comps (demand) with unsold active listings
    (supply) from the #13 snapshot. Sparse-safe: returns an empty curve when there
    is nothing to plot."""
    eid = (entity_id or "").strip()
    cur = _norm_currency(currency) if currency else None
    scoped = [sc for sc in sold_comps if cur is None or sc.currency == cur]
    currencies = {sc.currency for sc in scoped}
    effective_currency = cur or (next(iter(currencies)) if len(currencies) == 1 else None)

    # Each observation: (price, sold?, time_to_sale_days|None).
    observations: list[tuple[Decimal, bool, float | None]] = [
        (sc.sold_price, True, sc.time_to_sale_days) for sc in scoped
    ]
    for price in (active_listing_prices or ()):
        observations.append((_to_decimal(price), False, None))

    total = len(observations)
    if total == 0:
        return LiquidityCurve(eid, effective_currency, (), 0)

    ceilings = _curve_ceilings(sorted({p for p, _, _ in observations}), max_points)
    points: list[LiquidityCurvePoint] = []
    for ceiling in ceilings:
        at_or_below = [o for o in observations if o[0] <= ceiling]
        n = len(at_or_below)
        if n == 0:
            continue
        sold = [o for o in at_or_below if o[1]]
        tts = [o[2] for o in sold if o[2] is not None]
        points.append(LiquidityCurvePoint(
            price_ceiling=ceiling,
            probability_of_sale=len(sold) / n,
            expected_days_to_sale=(statistics.median(tts) if tts else None),
            sample_count=n,
        ))
    return LiquidityCurve(eid, effective_currency, tuple(points), total)


# ── Deliverable 5: liquidity-aware opportunity signals ────────────────────────
# Signal vocabulary + the rule semantics each encodes. These are *inputs* to a
# future scorer, not decisions — the classifier flags the pattern, scoring weighs
# it. Documented here as the naming authority.
LIQUIDITY_SIGNALS: dict[str, str] = {
    "cheap_and_liquid": "Priced below market AND transacts readily — the strongest buy setup.",
    "cheap_but_illiquid": "Priced below market but rarely sells — capital can get stuck.",
    "price_spike_without_liquidity": "Price rising while sales stay thin — likely hype, not demand.",
    "liquidity_surge": "Sales accelerating, often a leading indicator before a price move.",
    "fairly_priced_liquid": "Transacts readily at a market-consistent price.",
    "insufficient_data": "Not enough sold history to judge liquidity.",
}

# Defaults for "is this liquid?": either a healthy sell-through OR a steady sales
# cadence clears the bar. Callers can override per asset class.
_LIQUID_SELL_THROUGH = 0.5
_LIQUID_SALES_PER_WEEK = 1.0
_MIN_SOLD_FOR_JUDGEMENT = 3


@dataclass(frozen=True, slots=True)
class LiquiditySignal:
    signal: str
    description: str
    is_liquid: bool | None


def is_liquid(
    metrics: LiquidityMetrics,
    *,
    sell_through_threshold: float = _LIQUID_SELL_THROUGH,
    sales_per_week_threshold: float = _LIQUID_SALES_PER_WEEK,
) -> bool | None:
    """Liquidity verdict from metrics, or None when sold history is too thin to
    judge. Either a strong sell-through or a steady cadence qualifies."""
    if metrics.sold_count < _MIN_SOLD_FOR_JUDGEMENT:
        return None
    if metrics.sell_through_rate is not None and metrics.sell_through_rate >= sell_through_threshold:
        return True
    if (metrics.sales_per_week or 0.0) >= sales_per_week_threshold:
        return True
    return False


def classify_liquidity_signal(
    metrics: LiquidityMetrics,
    *,
    is_cheap: bool | None = None,
    price_rising: bool | None = None,
    liquidity_rising: bool | None = None,
    **liquid_kwargs,
) -> LiquiditySignal:
    """Map liquidity metrics + optional price/trend context to one signal. The
    price-context flags come from the caller (comparing against the #13 snapshot /
    a fair-value estimate); this layer only knows liquidity, so it stays None-safe
    when context is absent."""
    liquid = is_liquid(metrics, **liquid_kwargs)
    if liquid is None:
        return _signal("insufficient_data", None)

    # A price climbing without demand behind it is the clearest standalone warning.
    if price_rising and not liquid:
        return _signal("price_spike_without_liquidity", False)
    if liquidity_rising and liquid:
        return _signal("liquidity_surge", True)
    if is_cheap and liquid:
        return _signal("cheap_and_liquid", True)
    if is_cheap and not liquid:
        return _signal("cheap_but_illiquid", False)
    if liquid:
        return _signal("fairly_priced_liquid", True)
    return _signal("cheap_but_illiquid" if is_cheap else "insufficient_data",
                   False if is_cheap else None)


def _signal(name: str, liquid: bool | None) -> LiquiditySignal:
    return LiquiditySignal(name, LIQUIDITY_SIGNALS[name], liquid)


# ── small math helpers ────────────────────────────────────────────────────────
def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return Decimal(0)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / Decimal(2)


def _within_window(ts: str, window_days: int) -> bool:
    dt = _parse_iso(ts)
    if dt is None:
        return False
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    return age_days <= window_days


def _span_days(timestamps: Sequence[str]) -> float | None:
    parsed = [d for d in (_parse_iso(t) for t in timestamps) if d is not None]
    if len(parsed) < 2:
        return 0.0 if parsed else None
    return (max(parsed) - min(parsed)).total_seconds() / 86400.0


def _curve_ceilings(sorted_prices: list[Decimal], max_points: int) -> list[Decimal]:
    """Pick up to ``max_points`` price ceilings across the distribution. With few
    distinct prices, use them all; otherwise sample evenly so the curve stays
    readable on dense data."""
    n = len(sorted_prices)
    if n <= max_points or max_points <= 0:
        return sorted_prices
    step = (n - 1) / (max_points - 1)
    picked = {sorted_prices[round(i * step)] for i in range(max_points)}
    return sorted(picked)
