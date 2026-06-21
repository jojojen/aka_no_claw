"""Issue #15 — Fair Value Engine & confidence intervals.

Tests map onto the issue deliverables:
- baseline method prefers sold comps over listings (D3)
- listing-only fallback is weaker, range wider (D3/D4)
- insufficient data degrades safely (D1)
- confidence clamped, bounds widen when sparse (D4)
- mispricing bands incl. insufficient-data guard (D5)
- liquidity adjustment nudges fair value (D2)
- FairValueEngine wires #13/#14 ledgers end to end (D2)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from openclaw_adapter.fair_value import (
    BAND_FAIR,
    BAND_INSUFFICIENT,
    BAND_OVERPRICED,
    BAND_UNDERVALUED,
    METHOD_INSUFFICIENT,
    METHOD_LISTING,
    METHOD_SOLD_COMP,
    FairValueEngine,
    compute_fair_value,
    evaluate_mispricing,
    make_source_trust_resolver,
)
from openclaw_adapter.liquidity import SoldComparable, SoldCompLedger, compute_liquidity_metrics
from openclaw_adapter.price_ledger import MarketSnapshot, PriceLedger


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(days_ago: float = 0) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


def _sold(price, *, days_ago=1.0, eid="ent_x", src="S-mercari") -> SoldComparable:
    return SoldComparable(
        sold_comp_id=f"sc_{price}_{days_ago}_{src}",
        entity_id=eid, source_id=src, sold_price=Decimal(str(price)),
        currency="JPY", sold_at=_iso(days_ago),
    )


def _snapshot(*, count, median, lo, hi, sources=("S-mercari",), fresh_days=2.0) -> MarketSnapshot:
    return MarketSnapshot(
        entity_id="ent_x", currency="JPY", count=count,
        min_price=Decimal(str(lo)), max_price=Decimal(str(hi)),
        median_price=Decimal(str(median)), latest_price=Decimal(str(median)),
        latest_observed_at=_iso(fresh_days), freshness_seconds=fresh_days * 86400.0,
        source_ids=tuple(sources), quote_type_mix={"listing": count},
        latest_observations=(),
    )


# --- D3: sold comps preferred over listings -----------------------------------

def test_sold_comps_preferred_over_listings():
    sold = [_sold(12000, days_ago=3), _sold(12500, days_ago=5), _sold(11800, days_ago=7)]
    snap = _snapshot(count=8, median=15000, lo=14000, hi=16000)  # asking prices higher
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold)
    assert est.method == METHOD_SOLD_COMP
    assert est.fair_value == Decimal("12000")  # trimmed median of sold, not listings
    assert est.evidence_count == 3
    assert est.lower_bound is not None and est.upper_bound is not None
    assert 0.0 <= est.confidence <= 1.0


# --- D3/D4: listing-only fallback is weaker, range wider -----------------------

def test_listing_fallback_when_no_sold_comps():
    snap = _snapshot(count=4, median=10000, lo=9000, hi=11000)
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=[])
    assert est.method == METHOD_LISTING
    assert est.fair_value == Decimal("10000")
    # unsupported evidence widens the band beyond the raw min/max
    assert est.lower_bound < Decimal("9000")
    assert est.upper_bound > Decimal("11000")
    # listing-only confidence is lower than a comparable sold-comp estimate
    sold_est = compute_fair_value(
        "ent_x", snapshot=snap, sold_comps=[_sold(10000), _sold(10100), _sold(9900)]
    )
    assert sold_est.confidence > est.confidence


# --- D1: insufficient data degrades safely ------------------------------------

def test_insufficient_data_is_safe():
    est = compute_fair_value("ent_x", snapshot=None, sold_comps=[])
    assert est.method == METHOD_INSUFFICIENT
    assert est.fair_value is None
    assert est.confidence == 0.0
    assert est.has_value is False
    # empty snapshot (count 0) also degrades
    empty = _snapshot(count=0, median=0, lo=0, hi=0)
    empty = MarketSnapshot(
        entity_id="ent_x", currency="JPY", count=0, min_price=None, max_price=None,
        median_price=None, latest_price=None, latest_observed_at=None,
        freshness_seconds=None, source_ids=(), quote_type_mix={}, latest_observations=(),
    )
    assert compute_fair_value("ent_x", snapshot=empty, sold_comps=[]).method == METHOD_INSUFFICIENT


# --- D4: confidence clamped, sparse band wider --------------------------------

def test_sparse_sold_comps_widen_band():
    many = [_sold(10000 + i * 10, days_ago=i + 1) for i in range(6)]
    few = [_sold(10000), _sold(10050)]
    supported = compute_fair_value("ent_x", snapshot=None, sold_comps=many)
    sparse = compute_fair_value("ent_x", snapshot=None, sold_comps=few)
    sup_width = supported.upper_bound - supported.lower_bound
    spr_width = sparse.upper_bound - sparse.lower_bound
    # sparse evidence yields a (relatively) wider, padded band and lower confidence
    assert spr_width >= (sparse.fair_value * Decimal("0.2"))
    assert sparse.confidence <= supported.confidence
    assert 0.0 <= supported.confidence <= 1.0


# --- D5: mispricing bands ------------------------------------------------------

def _confident_estimate(fv=10000):
    sold = [_sold(fv, days_ago=i + 1) for i in range(5)]
    snap = _snapshot(count=5, median=fv, lo=fv - 200, hi=fv + 200,
                     sources=("S-mercari", "S-surugaya"))
    liq = compute_liquidity_metrics("ent_x", sold, window_days=30,
                                    active_listing_count=2)
    return compute_fair_value("ent_x", snapshot=snap, sold_comps=sold, liquidity=liq)


def test_mispricing_bands():
    est = _confident_estimate(10000)
    assert est.confidence >= 0.35  # confident enough to make a call

    under = evaluate_mispricing(est, 8000)   # 20% below fair (after any liq adj)
    assert under.recommendation_band == BAND_UNDERVALUED
    assert under.discount_to_fair_value > 0

    fair = evaluate_mispricing(est, est.fair_value)
    assert fair.recommendation_band == BAND_FAIR

    over = evaluate_mispricing(est, est.fair_value * Decimal("1.4"))
    assert over.recommendation_band == BAND_OVERPRICED
    assert over.premium_to_fair_value > 0


def test_low_confidence_blocks_recommendation():
    # single asking-price listing → very low confidence → no confident call
    snap = _snapshot(count=1, median=10000, lo=10000, hi=10000)
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=[])
    assert est.confidence < 0.35
    signal = evaluate_mispricing(est, 5000)  # nominally a huge discount
    assert signal.recommendation_band == BAND_INSUFFICIENT


def test_insufficient_estimate_mispricing_is_insufficient():
    est = compute_fair_value("ent_x", snapshot=None, sold_comps=[])
    signal = evaluate_mispricing(est, 9999)
    assert signal.recommendation_band == BAND_INSUFFICIENT
    assert signal.fair_value is None


# --- D2: liquidity adjustment --------------------------------------------------

def test_liquidity_adjustment_nudges_value():
    sold_recent = [_sold(10000, days_ago=i * 0.5 + 0.5) for i in range(20)]  # brisk
    liq_high = compute_liquidity_metrics("ent_x", sold_recent, window_days=30)
    est_high = compute_fair_value("ent_x", snapshot=None, sold_comps=sold_recent,
                                  liquidity=liq_high)

    sold_slow = [_sold(10000, days_ago=25)]  # one sale in the window → illiquid
    liq_low = compute_liquidity_metrics("ent_x", sold_slow, window_days=30)
    est_low = compute_fair_value("ent_x", snapshot=None, sold_comps=sold_slow,
                                 liquidity=liq_low)

    assert est_high.liquidity_adjustment is not None
    assert est_low.liquidity_adjustment is not None
    # brisk turnover lifts value relative to illiquid
    assert est_high.liquidity_adjustment > est_low.liquidity_adjustment


# --- D2: engine end to end over real ledgers ----------------------------------

def test_engine_end_to_end(tmp_path):
    pl = PriceLedger(tmp_path / "prices.db")
    scl = SoldCompLedger(tmp_path / "sold.db")
    scl.bootstrap()
    eid = "ent_kimetsu_box"

    for p, d in ((6000, 2), (6200, 5), (5900, 9), (6100, 12)):
        scl.record_sold_comp(entity_id=eid, source_id="S-mercari",
                             sold_price=p, sold_at=_iso(d), currency="JPY")
    for p in (6800, 7000, 6900):
        pl.record_observation(entity_id=eid, source_id="S-mercari",
                              price_amount=p, currency="JPY", quote_type="listing")

    engine = FairValueEngine(price_ledger=pl, sold_comp_ledger=scl)
    est = engine.estimate(eid, currency="JPY")
    assert est.method == METHOD_SOLD_COMP
    assert est.fair_value is not None
    assert est.explanation  # explainable
    assert 0.0 <= est.confidence <= 1.0

    signal = engine.evaluate_mispricing(eid, 4400, currency="JPY")  # retail well below
    assert signal.recommendation_band in (BAND_UNDERVALUED, BAND_INSUFFICIENT)


# --- D (reopened): source-trust weighting -------------------------------------

def test_source_trust_weights_the_median():
    """A cluster of low-trust sources quoting a cheap price must not drag fair
    value down as far as it would under an unweighted median."""
    sold = (
        [_sold(5000, days_ago=i + 1, src="spam") for i in range(3)]   # cheap, untrusted
        + [_sold(10000, days_ago=i + 1, src="trusted") for i in range(2)]  # real, trusted
    )

    def trust(sid: str) -> float:
        return 0.05 if sid == "spam" else 0.95

    weighted = compute_fair_value("ent_x", snapshot=None, sold_comps=sold,
                                  source_trust_fn=trust)
    plain = compute_fair_value("ent_x", snapshot=None, sold_comps=sold)
    # unweighted median sits at the cheap spam cluster; trust pulls it up
    assert plain.fair_value == Decimal("5000")
    assert weighted.fair_value > plain.fair_value


def test_low_trust_sources_lower_confidence():
    """Agreement among low-trust sources earns less corroboration credit than the
    same breadth of high-trust sources."""
    snap = _snapshot(count=5, median=10000, lo=9800, hi=10200,
                     sources=("S-a", "S-b", "S-c"))
    sold = [_sold(10000, days_ago=i + 1, src=f"S-{i}") for i in range(5)]

    high = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold,
                              source_trust_fn=lambda sid: 0.95)
    low = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold,
                             source_trust_fn=lambda sid: 0.10)
    assert high.confidence > low.confidence


def test_engine_uses_registry_trust_resolver(tmp_path):
    """The engine wires a Source-Registry/Domain-Registry-backed resolver by
    default so production valuations down-weight low-trust provenance."""
    from openclaw_adapter.knowledge_db import KnowledgeDatabase

    kdb = KnowledgeDatabase(tmp_path / "k.db")
    kdb.bootstrap()
    # Seed two sources: a high-trust marketplace and an unseeded/low-trust host.
    trusted_id = kdb.intern_source("https://www.suruga-ya.jp/product/12345")
    spam_id = kdb.intern_source("https://random-spam-host.example/listing/9")
    assert trusted_id and spam_id

    resolver = make_source_trust_resolver(kdb)
    assert resolver(trusted_id) > resolver(spam_id)

    scl = SoldCompLedger(tmp_path / "sold.db")
    scl.bootstrap()
    eid = "ent_trust"
    for i in range(3):
        scl.record_sold_comp(entity_id=eid, source_id=spam_id, sold_price=5000,
                             sold_at=_iso(i + 1), currency="JPY")
    for i in range(2):
        scl.record_sold_comp(entity_id=eid, source_id=trusted_id, sold_price=10000,
                             sold_at=_iso(i + 1), currency="JPY")

    engine = FairValueEngine(sold_comp_ledger=scl, knowledge_db=kdb)
    est = engine.estimate(eid, currency="JPY")
    # trusted ¥10000 comps outweigh the spam ¥5000 cluster despite being fewer
    assert est.fair_value is not None and est.fair_value > Decimal("5000")
