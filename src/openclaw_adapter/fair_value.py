"""Fair Value Engine & confidence intervals (issue #15).

Converts the market-data foundation — canonical entity (#12), price observation
ledger / market snapshot (#13), sold-comp harvesting & liquidity curves (#14),
and domain/source trust (#9/#11) — into an explainable *current fair value*
estimate with a confidence, a value range, and a mispricing assessment.

This is a deterministic V1: no ML, no forecasting. The baseline method prefers
demonstrated transactions (sold comps) over asking prices (listings), weights by
source trust and recency, widens its range when data is sparse or volatile, and
degrades safely to an ``insufficient_data`` result rather than guessing.

The engine is intentionally split into pure functions (``compute_fair_value`` /
``evaluate_mispricing``) plus a thin ``FairValueEngine`` that pulls inputs from
the #13/#14 ledgers, so valuation logic is unit-testable without a database.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Sequence

from .liquidity import LiquidityMetrics, SoldCompLedger, compute_liquidity_metrics
from .price_ledger import MarketSnapshot, PriceLedger

# ── vocabularies ──────────────────────────────────────────────────────────────

# Baseline methods, in descending order of evidential strength.
METHOD_SOLD_COMP = "sold_comp_median"      # demonstrated transactions (preferred)
METHOD_LISTING = "listing_median"          # asking prices only (weaker)
METHOD_INSUFFICIENT = "insufficient_data"  # nothing usable

# Mispricing recommendation bands (Deliverable 5).
BAND_UNDERVALUED = "undervalued"
BAND_FAIR = "fair"
BAND_PREMIUM = "premium"
BAND_OVERPRICED = "overpriced"
BAND_INSUFFICIENT = "insufficient_data"

# Tunables — coarse, deterministic, documented so they can be audited.
DEFAULT_WINDOW_DAYS = 30
# A sold-comp valuation needs at least this many transactions to be "supported";
# below it we still estimate but flag low confidence and widen the band.
MIN_SOLD_FOR_SUPPORT = 3
# Fraction trimmed from each tail before taking the median (robust to outliers).
TRIM_FRACTION = 0.10
# Confidence below this makes any mispricing call "insufficient_data".
MIN_CONFIDENCE_FOR_CALL = 0.35
# Mispricing thresholds (fraction of fair value).
UNDERVALUED_DISCOUNT = 0.15
PREMIUM_MARGIN = 0.10
OVERPRICED_MARGIN = 0.25
# How far liquidity can pull fair value (illiquid → discount, very liquid → small premium).
MAX_LIQUIDITY_ADJUSTMENT = 0.10


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _trimmed_median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    k = int(n * TRIM_FRACTION)
    core = ordered[k : n - k] if n - 2 * k >= 1 else ordered
    mid = len(core) // 2
    if len(core) % 2 == 1:
        return core[mid]
    return (core[mid - 1] + core[mid]) / Decimal(2)


# ── Deliverable 1: fair value result model ────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FairValueEstimate:
    entity_id: str
    valuation_at: str
    currency: str | None
    fair_value: Decimal | None        # None ⇔ insufficient data
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    confidence: float
    method: str
    evidence_count: int
    liquidity_adjustment: float | None
    explanation: tuple[str, ...] = ()

    @property
    def has_value(self) -> bool:
        return self.fair_value is not None and self.method != METHOD_INSUFFICIENT


# ── Deliverable 5: mispricing signal ──────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MispricingSignal:
    entity_id: str
    observed_price: Decimal
    fair_value: Decimal | None
    discount_to_fair_value: float     # >0 ⇔ cheaper than fair value
    premium_to_fair_value: float      # >0 ⇔ more expensive than fair value
    confidence: float
    recommendation_band: str
    reasons: tuple[str, ...] = ()


# ── Deliverable 3/4: baseline fair value method ───────────────────────────────
def compute_fair_value(
    entity_id: str,
    *,
    snapshot: MarketSnapshot | None,
    sold_comps: Sequence,
    liquidity: LiquidityMetrics | None = None,
    currency: str | None = None,
    valuation_at: str | None = None,
) -> FairValueEstimate:
    """Deterministically estimate fair value from market evidence.

    Preference order: recent sold comps (demonstrated demand) → listing snapshot
    (asking prices) → insufficient data. Confidence blends evidence volume,
    recency, source trust/agreement, and liquidity. The value range widens when
    evidence is thin or volatile so a sparse estimate never looks more certain
    than it is. ``liquidity`` (if given) nudges fair value: illiquid items get a
    small downward adjustment (harder to realize), highly liquid ones a small
    upward one."""
    eid = (entity_id or "").strip()
    at = valuation_at or _utc_now_iso()
    cur = currency or (snapshot.currency if snapshot else None)
    explanation: list[str] = []

    sold_prices = [
        p for p in (_to_decimal(getattr(sc, "sold_price", None)) for sc in sold_comps)
        if p is not None and p > 0
    ]

    if sold_prices:
        base = _trimmed_median(sold_prices)
        method = METHOD_SOLD_COMP
        evidence_count = len(sold_prices)
        lo, hi = _spread_bounds(sold_prices, supported=evidence_count >= MIN_SOLD_FOR_SUPPORT)
        explanation.append(
            f"{evidence_count} sold comp(s) → trimmed-median ¥{base}"
            + ("" if evidence_count >= MIN_SOLD_FOR_SUPPORT else " (sparse, range widened)")
        )
    elif snapshot is not None and snapshot.count > 0 and snapshot.median_price is not None:
        base = snapshot.median_price
        method = METHOD_LISTING
        evidence_count = snapshot.count
        listing_prices = [
            p for p in (snapshot.min_price, snapshot.median_price, snapshot.max_price)
            if p is not None
        ]
        lo, hi = _spread_bounds(listing_prices, supported=False)
        explanation.append(
            f"no sold comps; {evidence_count} listing(s) → median ¥{base} "
            "(asking prices, weaker evidence)"
        )
    else:
        explanation.append("no sold comps and no listings → insufficient data")
        return FairValueEstimate(
            entity_id=eid, valuation_at=at, currency=cur, fair_value=None,
            lower_bound=None, upper_bound=None, confidence=0.0,
            method=METHOD_INSUFFICIENT, evidence_count=0,
            liquidity_adjustment=None, explanation=tuple(explanation),
        )

    liq_adj = _liquidity_adjustment(liquidity)
    if liq_adj:
        adjusted = (base * (Decimal(1) + Decimal(str(liq_adj)))).quantize(Decimal(1))
        explanation.append(
            f"liquidity adjustment {liq_adj:+.0%} → ¥{adjusted}"
        )
        base = adjusted
        if lo is not None:
            lo = (lo * (Decimal(1) + Decimal(str(liq_adj)))).quantize(Decimal(1))
        if hi is not None:
            hi = (hi * (Decimal(1) + Decimal(str(liq_adj)))).quantize(Decimal(1))

    confidence = _confidence(
        method=method,
        evidence_count=evidence_count,
        snapshot=snapshot,
        liquidity=liquidity,
        explanation=explanation,
    )

    return FairValueEstimate(
        entity_id=eid, valuation_at=at, currency=cur, fair_value=base,
        lower_bound=lo, upper_bound=hi, confidence=confidence, method=method,
        evidence_count=evidence_count, liquidity_adjustment=liq_adj,
        explanation=tuple(explanation),
    )


def _spread_bounds(
    prices: Sequence[Decimal], *, supported: bool
) -> tuple[Decimal | None, Decimal | None]:
    """Lower/upper bound from observed spread. Sparse/unsupported evidence widens
    the band so a thin estimate doesn't masquerade as a tight one."""
    if not prices:
        return None, None
    lo, hi = min(prices), max(prices)
    if not supported:
        # widen ±15% around the observed span for thin/asking-only evidence
        pad = Decimal("0.15")
        lo = (lo * (Decimal(1) - pad)).quantize(Decimal(1))
        hi = (hi * (Decimal(1) + pad)).quantize(Decimal(1))
    return lo, hi


def _liquidity_adjustment(liquidity: LiquidityMetrics | None) -> float | None:
    """Map liquidity into a bounded multiplicative nudge. Illiquid (no/slow
    sales) → negative; brisk turnover → small positive. None when liquidity is
    unknown so the caller leaves fair value untouched."""
    if liquidity is None or liquidity.sold_count == 0:
        return None
    spw = liquidity.sales_per_week or 0.0
    # 0 sales/wk → -MAX; ~2+/wk → +MAX, linear in between.
    raw = (spw - 1.0) / 1.0 * MAX_LIQUIDITY_ADJUSTMENT
    return round(max(-MAX_LIQUIDITY_ADJUSTMENT, min(MAX_LIQUIDITY_ADJUSTMENT, raw)), 4)


def _confidence(
    *,
    method: str,
    evidence_count: int,
    snapshot: MarketSnapshot | None,
    liquidity: LiquidityMetrics | None,
    explanation: list[str],
) -> float:
    """Blend evidence volume, recency, source corroboration, and liquidity into a
    [0,1] confidence. Sold comps start higher than listing-only estimates.

    Source quality here is *corroboration* (how many distinct sources agree), not
    a per-source trust score: the snapshot carries opaque ``source_id`` provenance
    that isn't reliably resolvable to a domain trust prior at this layer, so
    weighting by it would be false precision. A source→trust resolver can be
    layered in later without changing this contract."""
    base = 0.40 if method == METHOD_SOLD_COMP else 0.15

    # evidence volume (diminishing returns)
    volume = min(0.20, 0.04 * evidence_count)

    # recency: newest observation freshness from the snapshot
    recency = 0.0
    if snapshot is not None and snapshot.freshness_seconds is not None:
        days = snapshot.freshness_seconds / 86400.0
        if days <= 7:
            recency = 0.10
        elif days <= 30:
            recency = 0.05
        elif days <= 90:
            recency = 0.02

    # source corroboration: independent agreement across distinct sources
    corroboration = 0.0
    if snapshot is not None:
        n_sources = len(snapshot.source_ids)
        if n_sources >= 3:
            corroboration = 0.10
        elif n_sources >= 2:
            corroboration = 0.05

    # liquidity: demonstrated, brisk turnover raises confidence
    liquidity_component = 0.0
    if liquidity is not None and liquidity.sold_count > 0:
        if (liquidity.sell_through_rate or 0.0) >= 0.5 or (liquidity.sales_per_week or 0.0) >= 1.0:
            liquidity_component = 0.10
            explanation.append("liquidity is medium/high → confidence raised")
        else:
            liquidity_component = 0.04

    return round(_clamp(base + volume + recency + corroboration + liquidity_component), 4)


def evaluate_mispricing(
    estimate: FairValueEstimate, observed_price
) -> MispricingSignal:
    """Compare an observed/target price against fair value (Deliverable 5).

    Insufficient data or low confidence never yields a confident buy/sell call —
    it returns the ``insufficient_data`` band so weak evidence can't masquerade
    as a recommendation."""
    obs = _to_decimal(observed_price) or Decimal(0)
    reasons: list[str] = []

    if not estimate.has_value or estimate.fair_value is None or estimate.fair_value <= 0:
        reasons.append("no fair value could be established")
        return MispricingSignal(
            entity_id=estimate.entity_id, observed_price=obs, fair_value=estimate.fair_value,
            discount_to_fair_value=0.0, premium_to_fair_value=0.0,
            confidence=estimate.confidence, recommendation_band=BAND_INSUFFICIENT,
            reasons=tuple(reasons),
        )

    fv = estimate.fair_value
    delta = float((fv - obs) / fv)          # >0 ⇔ cheaper than fair
    discount = max(0.0, delta)
    premium = max(0.0, -delta)

    if estimate.confidence < MIN_CONFIDENCE_FOR_CALL:
        band = BAND_INSUFFICIENT
        reasons.append(
            f"confidence {estimate.confidence:.2f} below {MIN_CONFIDENCE_FOR_CALL} "
            "→ no confident call"
        )
    elif discount >= UNDERVALUED_DISCOUNT:
        band = BAND_UNDERVALUED
        reasons.append(f"{discount:.0%} below fair value ¥{fv}")
    elif premium >= OVERPRICED_MARGIN:
        band = BAND_OVERPRICED
        reasons.append(f"{premium:.0%} above fair value ¥{fv}")
    elif premium >= PREMIUM_MARGIN:
        band = BAND_PREMIUM
        reasons.append(f"{premium:.0%} above fair value ¥{fv}")
    else:
        band = BAND_FAIR
        reasons.append(f"within ±{PREMIUM_MARGIN:.0%} of fair value ¥{fv}")

    return MispricingSignal(
        entity_id=estimate.entity_id, observed_price=obs, fair_value=fv,
        discount_to_fair_value=round(discount, 4), premium_to_fair_value=round(premium, 4),
        confidence=estimate.confidence, recommendation_band=band, reasons=tuple(reasons),
    )


# ── Deliverable 2: engine wiring over the #13/#14 ledgers ─────────────────────
class FairValueEngine:
    """Pulls market evidence for an ``entity_id`` from the price observation
    ledger (#13) and sold-comp ledger (#14), then runs the deterministic
    estimator. Either ledger may be omitted (e.g. sold-comp-only or listing-only
    deployments); the estimate degrades accordingly."""

    def __init__(
        self,
        *,
        price_ledger: PriceLedger | None = None,
        sold_comp_ledger: SoldCompLedger | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        self.price_ledger = price_ledger
        self.sold_comp_ledger = sold_comp_ledger
        self.window_days = max(1, int(window_days))

    def estimate(
        self, entity_id: str, *, currency: str | None = None
    ) -> FairValueEstimate:
        snapshot = (
            self.price_ledger.get_market_snapshot(entity_id, currency=currency)
            if self.price_ledger is not None
            else None
        )
        sold_comps = (
            self.sold_comp_ledger.sold_comparables_for(entity_id, currency=currency)
            if self.sold_comp_ledger is not None
            else []
        )
        liquidity = None
        if sold_comps:
            active_count = snapshot.count if snapshot is not None else None
            active_prices = (
                [o.price_amount for o in snapshot.latest_observations]
                if snapshot is not None
                else None
            )
            liquidity = compute_liquidity_metrics(
                entity_id, sold_comps, window_days=self.window_days,
                currency=currency, active_listing_count=active_count,
                active_listing_prices=active_prices,
            )
        return compute_fair_value(
            entity_id, snapshot=snapshot, sold_comps=sold_comps,
            liquidity=liquidity, currency=currency,
        )

    def evaluate_mispricing(
        self, entity_id: str, observed_price, *, currency: str | None = None
    ) -> MispricingSignal:
        return evaluate_mispricing(self.estimate(entity_id, currency=currency), observed_price)
