"""Issue #18 — Supply & Scarcity Intelligence Layer.

Tests map onto the deliverables:
- entity-centric scarcity signal model retaining provenance, idempotent (D1)
- scarcity metrics vocabulary; unknown metrics stored but not composited (D2)
- reprint/restock/EOL lifecycle events stored; scarcity impact attached (D3)
- inventory metrics stored; availability trend + contraction detectable (D4)
- snapshot API: scarcity + availability + reprint/EOL + confidence, sparse-safe (D5)
- snapshot adapts to a SupplySignal the #16 scorer consumes, explainable (D6)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openclaw_adapter.supply_store import (
    EOL_ENDED,
    EOL_RETIRED,
    EOL_UNKNOWN,
    REPRINT_ANNOUNCED,
    REPRINT_NONE,
    SCARCITY_METRICS,
    SUPPLY_SOURCE_MAPPINGS,
    TREND_FALLING,
    ScarcitySignal,
    SupplyScarcityStore,
    compute_inventory_contraction,
    compute_supply_shock_score,
    compute_supply_snapshot,
    normalize_scarcity_value,
    snapshot_to_supply_signal,
)
from openclaw_adapter.entity_opportunity_score import (
    SupplySignal,
    score_opportunity,
)
from openclaw_adapter.fair_value import compute_fair_value, evaluate_mispricing
from openclaw_adapter.liquidity import SoldComparable, compute_liquidity_metrics
from openclaw_adapter.price_ledger import MarketSnapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(days_ago: float = 0) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


# --- D1: entity-centric signal model + provenance + idempotency ----------------

def test_record_signal_is_entity_centric_with_provenance(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    sig = store.record_signal(
        entity_id="ent_x", scarcity_type="listing_count", value=12,
        observed_at=_iso(0), source_id="S-mercari", confidence=0.9,
    )
    assert sig.entity_id == "ent_x"
    assert sig.scarcity_type == "listing_count"
    assert sig.source_id == "S-mercari"  # provenance retained
    assert len(store.signals_for("ent_x")) == 1


def test_record_signal_is_idempotent(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    when = _iso(0)
    store.record_signal(entity_id="ent_x", scarcity_type="listing_count",
                        value=12, observed_at=when, source_id="S-a")
    store.record_signal(entity_id="ent_x", scarcity_type="listing_count",
                        value=12, observed_at=when, source_id="S-a")
    assert len(store.signals_for("ent_x")) == 1


def test_record_signal_requires_entity_id(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    try:
        store.record_signal(entity_id="", scarcity_type="listing_count", value=1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for missing entity_id")


# --- D2: vocabulary; unknown metrics handled safely ---------------------------

def test_vocabulary_defined():
    for m in ("inventory_depth", "listing_count", "reprint_risk",
              "supply_shock_score", "population_score"):
        assert m in SCARCITY_METRICS


def test_unknown_metric_stored_but_not_composited(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    store.record_signal(entity_id="ent_x", scarcity_type="some_future_metric",
                        value=0.9, observed_at=_iso(0))
    snap = store.get_supply_snapshot("ent_x")
    assert "some_future_metric" in snap.metrics   # stored
    assert snap.scarcity_score == 0.0             # but not composited


def test_normalize_scarcity_value_scales_and_clamps():
    assert normalize_scarcity_value("listing_count", 50) == 0.5    # 50/100
    assert normalize_scarcity_value("listing_count", 9999) == 1.0  # clamped
    assert normalize_scarcity_value("reprint_risk", 0.4) == 0.4    # already 0-1


def test_scarcity_composite_high_when_supply_thin(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    store.record_signal(entity_id="ent_x", scarcity_type="supply_shock_score",
                        value=0.9, observed_at=_iso(0))
    store.record_signal(entity_id="ent_x", scarcity_type="availability_score",
                        value=0.1, observed_at=_iso(0))
    store.record_signal(entity_id="ent_x", scarcity_type="inventory_depth",
                        value=2, observed_at=_iso(0))
    snap = store.get_supply_snapshot("ent_x")
    assert snap.scarcity_score > 0.6


# --- D3: reprint / restock / EOL lifecycle ------------------------------------

def test_lifecycle_event_sets_eol_status(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    store.record_lifecycle_event(entity_id="ent_x", event="production_ended",
                                 observed_at=_iso(1), source_id="S-official")
    snap = store.get_supply_snapshot("ent_x")
    assert snap.eol_status == EOL_ENDED
    assert snap.scarcity_score >= 0.45  # EOL tightens scarcity even alone


def test_retired_entity_is_scarcest(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    store.record_lifecycle_event(entity_id="ent_x", event="retired", observed_at=_iso(0))
    snap = store.get_supply_snapshot("ent_x")
    assert snap.eol_status == EOL_RETIRED
    assert snap.scarcity_score >= 0.6


def test_reprint_announcement_dampens_scarcity(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    store.record_signal(entity_id="ent_x", scarcity_type="supply_shock_score",
                        value=0.9, observed_at=_iso(0))
    before = store.get_supply_snapshot("ent_x").scarcity_score
    store.record_lifecycle_event(entity_id="ent_x", event="reprint_announced",
                                 observed_at=_iso(0))
    snap = store.get_supply_snapshot("ent_x")
    assert snap.reprint_status == REPRINT_ANNOUNCED
    assert snap.reprint_risk >= 0.5
    assert snap.scarcity_score < before  # supply may expand → less scarce


def test_record_lifecycle_event_rejects_unknown(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    try:
        store.record_lifecycle_event(entity_id="ent_x", event="totally_made_up")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown lifecycle event")


# --- D4: inventory tracking + availability trend + contraction ----------------

def test_supply_shock_detects_recent_collapse():
    series = (
        [ScarcitySignal(signal_id=f"b{i}", entity_id="ent_x", observed_at=_iso(2 + i),
                        scarcity_type="listing_count", value=40.0)
         for i in range(5)]
        + [ScarcitySignal(signal_id="r1", entity_id="ent_x", observed_at=_iso(0.2),
                          scarcity_type="listing_count", value=3.0)]
    )
    assert compute_supply_shock_score(series) > 0.5


def test_inventory_contraction_detected():
    # listing_count falling steadily over the window
    series = [
        ScarcitySignal(signal_id=f"c{i}", entity_id="ent_x", observed_at=_iso(10 - i),
                       scarcity_type="listing_count", value=float(40 - i * 5))
        for i in range(8)
    ]
    assert compute_inventory_contraction(series, window_days=30) > 0.3


def test_availability_trend_falling_when_inventory_shrinks(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    for i in range(8):
        store.record_signal(entity_id="ent_x", scarcity_type="listing_count",
                            value=float(40 - i * 5), observed_at=_iso(10 - i))
    snap = store.get_supply_snapshot("ent_x")
    assert snap.availability_trend == TREND_FALLING
    assert snap.inventory_contraction > 0.0


# --- D5: snapshot API, sparse-safe + source mappings --------------------------

def test_snapshot_handles_sparse_data(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    snap = store.get_supply_snapshot("ent_empty")
    assert snap.has_data is False
    assert snap.signal_count == 0
    assert snap.scarcity_score == 0.0
    assert snap.eol_status == EOL_UNKNOWN
    assert snap.reprint_status == REPRINT_NONE


def test_snapshot_exposes_metrics_freshness_confidence(tmp_path):
    store = SupplyScarcityStore(tmp_path / "s.db")
    store.record_signal(entity_id="ent_x", scarcity_type="listing_count",
                        value=8, observed_at=_iso(0), source_id="S-a")
    store.record_signal(entity_id="ent_x", scarcity_type="availability_score",
                        value=0.3, observed_at=_iso(0), source_id="S-b")
    snap = store.get_supply_snapshot("ent_x")
    assert snap.has_data is True
    assert "listing_count" in snap.metrics
    assert snap.freshness_seconds is not None and snap.freshness_seconds >= 0
    assert 0.0 <= snap.confidence <= 1.0
    assert set(snap.source_ids) == {"S-a", "S-b"}


def test_source_mappings_documented_and_entity_keyed():
    assert "marketplace" in SUPPLY_SOURCE_MAPPINGS
    assert "listing_count" in SUPPLY_SOURCE_MAPPINGS["marketplace"]
    assert "inventory_depth" in SUPPLY_SOURCE_MAPPINGS["official_store"]


# --- D6: opportunity integration ----------------------------------------------

def test_snapshot_to_supply_signal_is_explainable():
    sigs = (
        [ScarcitySignal(signal_id=f"a{i}", entity_id="ent_x", observed_at=_iso(10 - i),
                        scarcity_type="listing_count", value=float(40 - i * 5))
         for i in range(8)]
        + [ScarcitySignal(signal_id="eol", entity_id="ent_x", observed_at=_iso(0),
                          scarcity_type="production_ended", value=1.0)]
    )
    snap = compute_supply_snapshot("ent_x", sigs, window_days=30)
    signal = snapshot_to_supply_signal(snap)
    assert isinstance(signal, SupplySignal)
    assert 0.0 <= signal.scarcity_score <= 1.0
    assert signal.eol is True
    assert signal.reasons  # explainable


def test_supply_signal_raises_opportunity_score():
    sold = [SoldComparable(sold_comp_id=f"sc{i}", entity_id="ent_x", source_id="S-mercari",
                           sold_price=10000, currency="JPY", sold_at=_iso(i + 1))
            for i in range(5)]
    snap = MarketSnapshot(
        entity_id="ent_x", currency="JPY", count=5, min_price=9800, max_price=10200,
        median_price=10000, latest_price=10000, latest_observed_at=_iso(2),
        freshness_seconds=2 * 86400.0, source_ids=("S-mercari", "S-surugaya"),
        quote_type_mix={"listing": 5}, latest_observations=(),
    )
    liq = compute_liquidity_metrics("ent_x", sold, window_days=30, active_listing_count=2)
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold, liquidity=liq)
    mis = evaluate_mispricing(est, 9500)

    scarce = SupplySignal(entity_id="ent_x", scarcity_score=0.9, eol=True,
                          reasons=("production ended",))
    without = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    with_supply = score_opportunity("ent_x", estimate=est, mispricing=mis,
                                    liquidity=liq, supply=scarce)
    assert with_supply.score > without.score


def test_reprint_risk_surfaces_as_opportunity_risk():
    sold = [SoldComparable(sold_comp_id=f"sc{i}", entity_id="ent_x", source_id="S-mercari",
                           sold_price=10000, currency="JPY", sold_at=_iso(i + 1))
            for i in range(5)]
    snap = MarketSnapshot(
        entity_id="ent_x", currency="JPY", count=5, min_price=9800, max_price=10200,
        median_price=10000, latest_price=10000, latest_observed_at=_iso(2),
        freshness_seconds=2 * 86400.0, source_ids=("S-mercari",),
        quote_type_mix={"listing": 5}, latest_observations=(),
    )
    liq = compute_liquidity_metrics("ent_x", sold, window_days=30, active_listing_count=2)
    est = compute_fair_value("ent_x", snapshot=snap, sold_comps=sold, liquidity=liq)
    mis = evaluate_mispricing(est, 9500)
    supply = SupplySignal(entity_id="ent_x", scarcity_score=0.2, reprint_risk=0.8)
    scored = score_opportunity("ent_x", estimate=est, mispricing=mis,
                               liquidity=liq, supply=supply)
    assert any("reprint" in r for r in scored.risks)
