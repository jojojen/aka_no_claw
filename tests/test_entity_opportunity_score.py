"""Issue #16 — Opportunity Scoring Engine.

Tests map onto the deliverables:
- score model + normalized range + category (D1)
- missing inputs tolerated, sparse lowers confidence (D2)
- valuation gap contributes both directions (D3)
- liquidity raises/lowers + illiquidity risk visible (D4)
- demand interface consumed when present (D5)
- explanation is human-readable (D6)
- ranked feed sorts by score then confidence; low-confidence visible (D7)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from openclaw_adapter.entity_opportunity_score import (
    CAT_AVOID,
    CAT_INSUFFICIENT,
    CAT_STRONG_BUY,
    DemandSignal,
    OpportunityScorer,
    format_opportunity_score,
    score_opportunity,
)
from openclaw_adapter.fair_value import (
    FairValueEngine,
    compute_fair_value,
    evaluate_mispricing,
)
from openclaw_adapter.liquidity import SoldComparable, SoldCompLedger, compute_liquidity_metrics
from openclaw_adapter.price_ledger import MarketSnapshot, PriceLedger


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(days_ago: float = 0) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


def _sold(price, *, days_ago=1.0, eid="ent_x") -> SoldComparable:
    return SoldComparable(
        sold_comp_id=f"sc_{eid}_{price}_{days_ago}",
        entity_id=eid, source_id="S-mercari", sold_price=Decimal(str(price)),
        currency="JPY", sold_at=_iso(days_ago),
    )


def _snapshot(*, count, median, lo, hi, sources=("S-mercari", "S-surugaya")) -> MarketSnapshot:
    return MarketSnapshot(
        entity_id="ent_x", currency="JPY", count=count,
        min_price=Decimal(str(lo)), max_price=Decimal(str(hi)),
        median_price=Decimal(str(median)), latest_price=Decimal(str(median)),
        latest_observed_at=_iso(2), freshness_seconds=2 * 86400.0,
        source_ids=tuple(sources), quote_type_mix={"listing": count},
        latest_observations=(),
    )


def _estimate(fv=10000, *, n_sold=5):
    sold = [_sold(fv, days_ago=i + 1) for i in range(n_sold)]
    snap = _snapshot(count=5, median=fv, lo=fv - 200, hi=fv + 200)
    liq = compute_liquidity_metrics("ent_x", sold, window_days=30, active_listing_count=2)
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold, liquidity=liq)
    return est, liq


# --- D1/D3: undervalued + liquid → strong buy ---------------------------------

def test_undervalued_liquid_is_strong_buy():
    est, liq = _estimate(10000)
    mis = evaluate_mispricing(est, 7000)  # ~30% below fair
    s = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    assert 0.0 <= s.score <= 100.0
    assert s.category == CAT_STRONG_BUY
    assert any("below fair value" in r for r in s.reasons)
    assert s.confidence > 0.0


# --- D3/D4: overpriced → avoid, premium is a visible risk ----------------------

def test_overpriced_is_avoid():
    est, liq = _estimate(10000)
    mis = evaluate_mispricing(est, est.fair_value * Decimal("1.5"))
    s = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    assert s.category == CAT_AVOID
    assert s.score < 50.0
    assert any("above fair value" in r for r in s.risks)


# --- D4: illiquid undervalued is penalized + flagged --------------------------

def test_cheap_but_illiquid_is_penalized():
    sold = [_sold(10000, days_ago=25)]  # one sale all month → illiquid
    snap = _snapshot(count=3, median=10000, lo=10000, hi=10500)
    liq = compute_liquidity_metrics("ent_x", sold, window_days=30, active_listing_count=3)
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold, liquidity=liq)
    mis = evaluate_mispricing(est, 8000)

    liquid_est, liquid_liq = _estimate(10000)
    liquid_mis = evaluate_mispricing(liquid_est, 8000)

    illiquid = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    liquid = score_opportunity("ent_x", estimate=liquid_est, mispricing=liquid_mis,
                               liquidity=liquid_liq)
    assert illiquid.score < liquid.score
    assert any("illiquid" in r for r in illiquid.risks)


# --- D1: insufficient data is safe --------------------------------------------

def test_insufficient_data_category():
    est = compute_fair_value("ent_x", snapshot=None, sold_comps=[])
    s = score_opportunity("ent_x", estimate=est, mispricing=None)
    assert s.category == CAT_INSUFFICIENT
    assert s.confidence == 0.0


# --- D2/D5: demand component consumed, missing inputs tolerated ----------------

def test_demand_signal_raises_score():
    est, liq = _estimate(10000)
    mis = evaluate_mispricing(est, 9500)  # roughly fair
    without = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    with_demand = score_opportunity(
        "ent_x", estimate=est, mispricing=mis, liquidity=liq,
        demand=DemandSignal(entity_id="ent_x", demand_score=0.9, reasons=("burst on X",)),
    )
    assert with_demand.score > without.score
    assert any("rising demand" in r or "burst" in r for r in with_demand.reasons)


def test_missing_liquidity_is_tolerated():
    est, _ = _estimate(10000)
    mis = evaluate_mispricing(est, 8000)
    s = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=None)
    assert 0.0 <= s.score <= 100.0
    assert any("liquidity unknown" in r for r in s.risks)


# --- D6: explanation human-readable -------------------------------------------

def test_format_is_human_readable():
    est, liq = _estimate(10000)
    mis = evaluate_mispricing(est, 7000)
    s = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    text = format_opportunity_score(s)
    assert "Score:" in text and "Confidence:" in text and "Reasons:" in text


# --- D7: ranked feed over real ledgers ----------------------------------------

def _seed(pl: PriceLedger, scl: SoldCompLedger, eid, *, fair, ask, n_sold=5, days=1):
    for i in range(n_sold):
        scl.record_sold_comp(entity_id=eid, source_id="S-mercari", sold_price=fair,
                             sold_at=_iso(i + days), currency="JPY")
    pl.record_observation(entity_id=eid, source_id="S-mercari", price_amount=ask,
                          currency="JPY", quote_type="listing")
    pl.record_observation(entity_id=eid, source_id="S-surugaya", price_amount=ask + 100,
                          currency="JPY", quote_type="listing")


def test_ranked_feed(tmp_path):
    pl = PriceLedger(tmp_path / "p.db")
    scl = SoldCompLedger(tmp_path / "s.db")
    scl.bootstrap()
    _seed(pl, scl, "ent_deal", fair=10000, ask=7000)     # deep discount
    _seed(pl, scl, "ent_fair", fair=10000, ask=9800)     # ~fair
    _seed(pl, scl, "ent_bad", fair=10000, ask=14000)     # overpriced

    scorer = OpportunityScorer(FairValueEngine(price_ledger=pl, sold_comp_ledger=scl))
    ranked = scorer.get_top_opportunities(["ent_fair", "ent_bad", "ent_deal"])
    ids = [s.entity_id for s in ranked]
    assert ids[0] == "ent_deal"  # best opportunity ranked first
    # sorted by score descending
    assert all(ranked[i].score >= ranked[i + 1].score for i in range(len(ranked) - 1))
