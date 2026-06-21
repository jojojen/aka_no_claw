"""Issue #17 — Demand Signal Feature Store & market-attention metrics.

Tests map onto the deliverables:
- entity-centric feature model retaining provenance, idempotent (D1)
- snapshot API: features + freshness + confidence + trend, sparse-safe (D2)
- metrics vocabulary; unknown metrics stored but not composited (D3)
- burst (24h spike) + momentum (acceleration), both persistable (D4)
- documented source mappings, entity linkage via #12 entity_id (D5)
- snapshot adapts to a DemandSignal the #16 scorer consumes (D6)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openclaw_adapter.demand_store import (
    DEMAND_METRICS,
    DEMAND_SOURCE_MAPPINGS,
    TREND_RISING,
    TREND_UNKNOWN,
    DemandFeatureStore,
    compute_burst_score,
    compute_demand_snapshot,
    compute_momentum_score,
    normalize_feature_value,
    snapshot_to_demand_signal,
)
from openclaw_adapter.entity_opportunity_score import DemandSignal, score_opportunity
from openclaw_adapter.fair_value import compute_fair_value, evaluate_mispricing
from openclaw_adapter.liquidity import SoldComparable, compute_liquidity_metrics
from openclaw_adapter.price_ledger import MarketSnapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(days_ago: float = 0) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


# --- D1: entity-centric feature model + provenance + idempotency --------------

def test_record_feature_is_entity_centric_with_provenance(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    f = store.record_feature(
        entity_id="ent_x", feature_name="mention_count", feature_value=42,
        observed_at=_iso(0), source_id="S-twitter", confidence=0.8,
    )
    assert f.entity_id == "ent_x"
    assert f.feature_name == "mention_count"
    assert f.source_id == "S-twitter"  # provenance retained
    rows = store.features_for("ent_x")
    assert len(rows) == 1


def test_record_feature_is_idempotent(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    when = _iso(0)
    store.record_feature(entity_id="ent_x", feature_name="mention_count",
                         feature_value=10, observed_at=when, source_id="S-a")
    store.record_feature(entity_id="ent_x", feature_name="mention_count",
                         feature_value=10, observed_at=when, source_id="S-a")
    assert len(store.features_for("ent_x")) == 1


def test_record_feature_requires_entity_id(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    try:
        store.record_feature(entity_id="", feature_name="mention_count", feature_value=1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for missing entity_id")


# --- D2: snapshot API, sparse-safe -------------------------------------------

def test_snapshot_handles_sparse_data(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    snap = store.get_demand_snapshot("ent_empty")
    assert snap.has_data is False
    assert snap.feature_count == 0
    assert snap.confidence == 0.0
    assert snap.trend_direction == TREND_UNKNOWN


def test_snapshot_exposes_features_freshness_confidence(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    store.record_feature(entity_id="ent_x", feature_name="mention_count",
                         feature_value=80, observed_at=_iso(0), source_id="S-a")
    store.record_feature(entity_id="ent_x", feature_name="market_attention",
                         feature_value=0.7, observed_at=_iso(0), source_id="S-b")
    snap = store.get_demand_snapshot("ent_x")
    assert snap.has_data is True
    assert "mention_count" in snap.features
    assert snap.freshness_seconds is not None and snap.freshness_seconds >= 0
    assert 0.0 <= snap.confidence <= 1.0
    assert set(snap.source_ids) == {"S-a", "S-b"}


# --- D3: vocabulary; unknown metrics handled safely --------------------------

def test_vocabulary_defined():
    for m in ("mention_count", "burst_score", "market_attention", "search_interest"):
        assert m in DEMAND_METRICS


def test_unknown_metric_stored_but_not_composited(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    store.record_feature(entity_id="ent_x", feature_name="some_future_metric",
                         feature_value=0.9, observed_at=_iso(0))
    snap = store.get_demand_snapshot("ent_x")
    assert "some_future_metric" in snap.features   # stored
    assert snap.market_attention == 0.0            # but not composited


def test_normalize_feature_value_scales_and_clamps():
    assert normalize_feature_value("mention_count", 50) == 0.5   # 50/100
    assert normalize_feature_value("mention_count", 9999) == 1.0  # clamped
    assert normalize_feature_value("engagement_score", 0.4) == 0.4  # already 0-1


# --- D4: burst + momentum -----------------------------------------------------

def test_burst_score_detects_recent_spike():
    # build a mention series: quiet baseline then a 24h spike
    from openclaw_adapter.demand_store import DemandFeature

    feats = (
        [DemandFeature(feature_id=f"b{i}", entity_id="ent_x", observed_at=_iso(2 + i),
                       feature_name="mention_count", feature_value=2.0)
         for i in range(5)]
        + [DemandFeature(feature_id="r1", entity_id="ent_x", observed_at=_iso(0.2),
                         feature_name="mention_count", feature_value=80.0)]
    )
    assert compute_burst_score(feats) > 0.5


def test_momentum_positive_when_accelerating():
    from openclaw_adapter.demand_store import DemandFeature

    feats = [
        DemandFeature(feature_id=f"m{i}", entity_id="ent_x", observed_at=_iso(10 - i),
                      feature_name="mention_count", feature_value=float(i * i))
        for i in range(8)
    ]
    assert compute_momentum_score(feats, window_days=14) > 0.1


def test_burst_and_momentum_persisted_and_surfaced(tmp_path):
    store = DemandFeatureStore(tmp_path / "d.db")
    store.record_feature(entity_id="ent_x", feature_name="burst_score",
                         feature_value=0.9, observed_at=_iso(0))
    store.record_feature(entity_id="ent_x", feature_name="momentum_score",
                         feature_value=0.6, observed_at=_iso(0))
    snap = store.get_demand_snapshot("ent_x")
    assert snap.burst_score >= 0.9
    assert snap.momentum_score >= 0.6
    assert snap.trend_direction == TREND_RISING


# --- D5: source mappings + entity linkage ------------------------------------

def test_source_mappings_documented_and_entity_keyed():
    assert "sns_monitor_bot" in DEMAND_SOURCE_MAPPINGS
    assert "mention_count" in DEMAND_SOURCE_MAPPINGS["sns_monitor_bot"]
    assert "announcement_score" in DEMAND_SOURCE_MAPPINGS["official_announcement"]


# --- D6: opportunity integration ---------------------------------------------

def test_snapshot_to_demand_signal_is_explainable():
    from openclaw_adapter.demand_store import DemandFeature

    feats = (
        [DemandFeature(feature_id=f"a{i}", entity_id="ent_x", observed_at=_iso(5 - i * 0.5),
                       feature_name="mention_count", feature_value=float(10 + i * 30))
         for i in range(6)]
        + [DemandFeature(feature_id="att", entity_id="ent_x", observed_at=_iso(0),
                         feature_name="market_attention", feature_value=0.8)]
    )
    snap = compute_demand_snapshot("ent_x", feats, window_days=14)
    signal = snapshot_to_demand_signal(snap)
    assert isinstance(signal, DemandSignal)
    assert 0.0 <= signal.demand_score <= 1.0
    assert signal.reasons  # explainable


def test_demand_signal_raises_opportunity_score():
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

    high_demand = DemandSignal(entity_id="ent_x", demand_score=0.95, reasons=("burst",))
    without = score_opportunity("ent_x", estimate=est, mispricing=mis, liquidity=liq)
    with_demand = score_opportunity("ent_x", estimate=est, mispricing=mis,
                                    liquidity=liq, demand=high_demand)
    assert with_demand.score > without.score
