"""Opportunity Scoring Engine (issue #16).

Combines the market-intelligence foundation into a single, explainable, ranked
*opportunity score* per canonical entity (#12):

    fair value & mispricing (#15)  →  valuation-gap component
    liquidity metrics (#14)        →  liquidity component
    demand signals (future SNS)    →  demand component

This is a deterministic V1: no ML, no forecasting, no automated buying. It is
distinct from ``opportunity_scoring.py`` — that module is the candidate-level
recommendation *gate* (heat / price-ratio / reputation) for the existing TCG
pipeline. This module scores *entities* on liquidity-adjusted undervaluation so
opportunities can be ranked for the dashboard / Telegram digest.

Weighting (documented so it can be audited; all on a 0–100 scale around a
neutral 50):
    valuation gap : ±35   (cheap vs fair → +,  premium → −)
    liquidity     : +15 / −10   (liquid → +,  illiquid → − and a visible risk)
    demand        : +15   (rising demand → +; absent → no contribution)
    supply        : +10   (scarce/shrinking supply → +; reprint risk → visible risk)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .fair_value import (
    BAND_INSUFFICIENT,
    BAND_OVERPRICED,
    BAND_PREMIUM,
    BAND_UNDERVALUED,
    FairValueEngine,
    FairValueEstimate,
    MispricingSignal,
)
from .fair_value import MIN_SOLD_FOR_SUPPORT
from .liquidity import LiquidityMetrics

# ── categories (Deliverable 1) ────────────────────────────────────────────────
CAT_STRONG_BUY = "strong_buy"
CAT_WATCHLIST = "watchlist"
CAT_NEUTRAL = "neutral"
CAT_SPECULATIVE = "speculative"
CAT_AVOID = "avoid"
CAT_INSUFFICIENT = "insufficient_data"

# Score is normalized to [0, 100] around a neutral midpoint.
NEUTRAL_SCORE = 50.0
MAX_VALUATION_CONTRIB = 35.0
MAX_LIQUIDITY_BONUS = 15.0
MAX_LIQUIDITY_PENALTY = 10.0
MAX_DEMAND_CONTRIB = 15.0
MAX_SUPPLY_CONTRIB = 10.0

# A score needs at least this confidence to earn an actionable (buy) category;
# below it, a good-looking score is "speculative" rather than "strong_buy".
MIN_CONFIDENCE_FOR_BUY = 0.55
# Below this we don't make a directional call at all.
MIN_CONFIDENCE_FOR_CALL = 0.35


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


# ── Deliverable 5: demand interface (future SNS / trend integration) ──────────
@dataclass(frozen=True, slots=True)
class DemandSignal:
    """Normalized demand snapshot for an entity. ``demand_score`` ∈ [0,1] is the
    single value the scorer consumes; the optional breakdown fields preserve the
    raw signals (mention growth, burst, official announcement, search growth) for
    explanation/audit. Kept deliberately small so SNS/trend pipelines can populate
    it later without changing the scoring contract."""
    entity_id: str
    demand_score: float
    mention_growth: float | None = None
    burst_score: float | None = None
    official_announcement: bool = False
    search_growth: float | None = None
    reasons: tuple[str, ...] = ()


# ── supply interface (#18 Supply & Scarcity Intelligence Layer) ───────────────
@dataclass(frozen=True, slots=True)
class SupplySignal:
    """Normalized supply/scarcity snapshot for an entity. ``scarcity_score`` ∈
    [0,1] is the single value the scorer consumes (higher ⇒ scarcer ⇒ stronger
    opportunity when demand holds); the optional breakdown (reprint risk, EOL,
    availability trend) preserves the raw signals for explanation/audit. Kept
    small so the #18 supply pipeline can populate it without changing the scoring
    contract."""
    entity_id: str
    scarcity_score: float
    reprint_risk: float | None = None
    eol: bool = False
    availability_trend: str | None = None
    reasons: tuple[str, ...] = ()


# ── Deliverable 1: opportunity score model ────────────────────────────────────
@dataclass(frozen=True, slots=True)
class OpportunityScore:
    entity_id: str
    scored_at: str
    score: float                  # 0–100
    confidence: float             # 0–1
    category: str
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


def score_opportunity(
    entity_id: str,
    *,
    estimate: FairValueEstimate,
    mispricing: MispricingSignal | None,
    liquidity: LiquidityMetrics | None = None,
    demand: DemandSignal | None = None,
    supply: SupplySignal | None = None,
    scored_at: str | None = None,
) -> OpportunityScore:
    """Deterministically score an entity opportunity from valuation, liquidity,
    and (optional) demand evidence. Missing inputs are tolerated: each absent
    component simply contributes nothing and lowers confidence rather than
    raising. Low confidence never yields an actionable buy category."""
    eid = (entity_id or "").strip()
    at = scored_at or _utc_now_iso()
    reasons: list[str] = []
    risks: list[str] = []

    # No fair value → nothing to compare against.
    if not estimate.has_value:
        risks.append("no fair value could be established")
        return OpportunityScore(
            entity_id=eid, scored_at=at, score=NEUTRAL_SCORE, confidence=0.0,
            category=CAT_INSUFFICIENT, reasons=("insufficient market evidence",),
            risks=tuple(risks),
        )

    score = NEUTRAL_SCORE

    # ── Deliverable 3: valuation gap component ────────────────────────────────
    if mispricing is not None and mispricing.recommendation_band != BAND_INSUFFICIENT:
        if mispricing.discount_to_fair_value > 0:
            contrib = min(MAX_VALUATION_CONTRIB,
                          MAX_VALUATION_CONTRIB * (mispricing.discount_to_fair_value / 0.30))
            score += contrib
            reasons.append(
                f"{mispricing.discount_to_fair_value:.0%} below fair value "
                f"¥{estimate.fair_value}"
            )
        elif mispricing.premium_to_fair_value > 0:
            contrib = min(MAX_VALUATION_CONTRIB,
                          MAX_VALUATION_CONTRIB * (mispricing.premium_to_fair_value / 0.30))
            score -= contrib
            note = f"{mispricing.premium_to_fair_value:.0%} above fair value ¥{estimate.fair_value}"
            reasons.append(note)
            if mispricing.recommendation_band in (BAND_OVERPRICED, BAND_PREMIUM):
                risks.append(note)
        else:
            reasons.append(f"priced near fair value ¥{estimate.fair_value}")
    else:
        risks.append("no buyable price to compare against fair value")

    # ── Deliverable 4: liquidity component ────────────────────────────────────
    if liquidity is not None and liquidity.sold_count > 0:
        spw = liquidity.sales_per_week or 0.0
        sell_through = liquidity.sell_through_rate
        if spw >= 1.0 or (sell_through is not None and sell_through >= 0.5):
            score += MAX_LIQUIDITY_BONUS
            reasons.append(
                f"high liquidity ({spw:.1f} sales/wk"
                + (f", {sell_through:.0%} sell-through" if sell_through is not None else "")
                + ")"
            )
        elif spw >= 0.25:
            score += MAX_LIQUIDITY_BONUS * 0.4
            reasons.append(f"moderate liquidity ({spw:.1f} sales/wk)")
        else:
            score -= MAX_LIQUIDITY_PENALTY
            risks.append("cheap but illiquid — slow to sell")
    else:
        score -= MAX_LIQUIDITY_PENALTY * 0.5
        risks.append("no demonstrated sales (liquidity unknown)")

    # ── Deliverable 5: demand component ───────────────────────────────────────
    if demand is not None:
        d = _clamp(demand.demand_score, 0.0, 1.0)
        score += MAX_DEMAND_CONTRIB * d
        if d >= 0.6:
            reasons.append("rising demand")
        elif d <= 0.2:
            risks.append("weak demand signal")
        reasons.extend(demand.reasons)
    else:
        reasons.append("demand signals not yet available")

    # ── supply / scarcity component (#18) ─────────────────────────────────────
    if supply is not None:
        sc = _clamp(supply.scarcity_score, 0.0, 1.0)
        score += MAX_SUPPLY_CONTRIB * sc
        if sc >= 0.6:
            reasons.append("scarce / shrinking supply")
        if supply.eol:
            reasons.append("end-of-life — no fresh supply")
        if supply.reprint_risk is not None and supply.reprint_risk >= 0.5:
            risks.append("reprint/restock risk could expand supply")
        reasons.extend(supply.reasons)

    # ── Deliverable 6: thin-evidence risks ───────────────────────────────────
    if estimate.evidence_count < MIN_SOLD_FOR_SUPPORT:
        risks.append("limited sold-comp history")

    score = _clamp(round(score, 1), 0.0, 100.0)
    confidence = estimate.confidence
    category = _categorize(score, confidence, mispricing)

    return OpportunityScore(
        entity_id=eid, scored_at=at, score=score, confidence=round(confidence, 4),
        category=category, reasons=tuple(reasons), risks=tuple(risks),
    )


def _categorize(
    score: float, confidence: float, mispricing: MispricingSignal | None
) -> str:
    if confidence < MIN_CONFIDENCE_FOR_CALL:
        return CAT_INSUFFICIENT
    if mispricing is not None and mispricing.recommendation_band == BAND_OVERPRICED:
        return CAT_AVOID
    if score <= 35.0:
        return CAT_AVOID
    if score >= 70.0:
        return CAT_STRONG_BUY if confidence >= MIN_CONFIDENCE_FOR_BUY else CAT_SPECULATIVE
    if score >= 55.0:
        return CAT_WATCHLIST
    return CAT_NEUTRAL


# ── Deliverable 2 + 7: engine wiring & ranked feed ────────────────────────────
class OpportunityScorer:
    """Scores entities by pulling fair value (#15) — which itself pulls the
    #13/#14 ledgers — and liquidity, then comparing the cheapest available
    listing (the buyable price) against fair value. Optional demand signals are
    injected per entity."""

    def __init__(
        self,
        engine: FairValueEngine,
        *,
        currency: str | None = "JPY",
    ) -> None:
        self.engine = engine
        self.currency = currency

    def _liquidity_for(self, entity_id: str) -> LiquidityMetrics | None:
        from .liquidity import compute_liquidity_metrics
        if self.engine.sold_comp_ledger is None:
            return None
        sold = self.engine.sold_comp_ledger.sold_comparables_for(
            entity_id, currency=self.currency
        )
        if not sold:
            return None
        active = None
        if self.engine.price_ledger is not None:
            snap = self.engine.price_ledger.get_market_snapshot(entity_id, currency=self.currency)
            active = snap.count
        return compute_liquidity_metrics(
            entity_id, sold, window_days=self.engine.window_days,
            currency=self.currency, active_listing_count=active,
        )

    def score(
        self, entity_id: str, *, demand: DemandSignal | None = None,
        supply: SupplySignal | None = None,
    ) -> OpportunityScore:
        estimate = self.engine.estimate(entity_id, currency=self.currency)
        observed = None
        if self.engine.price_ledger is not None:
            snap = self.engine.price_ledger.get_market_snapshot(entity_id, currency=self.currency)
            observed = snap.min_price  # cheapest ask = the price you could buy at
        mispricing = (
            self.engine.evaluate_mispricing(entity_id, observed, currency=self.currency)
            if observed is not None
            else None
        )
        liquidity = self._liquidity_for(entity_id)
        return score_opportunity(
            entity_id, estimate=estimate, mispricing=mispricing,
            liquidity=liquidity, demand=demand, supply=supply,
        )

    def get_top_opportunities(
        self,
        entity_ids: Iterable[str],
        *,
        demand_by_entity: dict[str, DemandSignal] | None = None,
        supply_by_entity: dict[str, SupplySignal] | None = None,
        limit: int | None = None,
        include_insufficient: bool = False,
    ) -> list[OpportunityScore]:
        """Rank entities by (score, confidence), highest first (Deliverable 7).

        Low-confidence opportunities stay visible but rank below confident ones
        (confidence is the tie-breaker). ``insufficient_data`` rows are excluded
        by default but can be surfaced with ``include_insufficient=True``."""
        demand_by_entity = demand_by_entity or {}
        supply_by_entity = supply_by_entity or {}
        scored = [
            self.score(eid, demand=demand_by_entity.get(eid),
                       supply=supply_by_entity.get(eid))
            for eid in entity_ids
        ]
        if not include_insufficient:
            scored = [s for s in scored if s.category != CAT_INSUFFICIENT]
        scored.sort(key=lambda s: (s.score, s.confidence), reverse=True)
        return scored[:limit] if limit is not None else scored


def format_opportunity_score(s: OpportunityScore) -> str:
    """Human-readable summary for dashboard / Telegram (Deliverable 6)."""
    lines = [
        f"Score: {s.score:.0f}  ({s.category})",
        f"Confidence: {s.confidence:.2f}",
    ]
    if s.reasons:
        lines.append("Reasons:")
        lines.extend(f"- {r}" for r in s.reasons)
    if s.risks:
        lines.append("Risks:")
        lines.extend(f"- {r}" for r in s.risks)
    return "\n".join(lines)
