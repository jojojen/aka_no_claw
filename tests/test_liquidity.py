"""Tests for Sold-Comp Harvesting & Liquidity Curves (#14)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from openclaw_adapter.liquidity import (
    LIQUIDITY_METRICS,
    LIQUIDITY_SIGNALS,
    MARKETPLACES,
    SoldCompLedger,
    SoldComparable,
    build_liquidity_curve,
    build_sold_comp_id,
    classify_liquidity_signal,
    compute_liquidity_metrics,
    is_liquid,
    normalize_sold_event,
    resolve_marketplace,
)


@pytest.fixture()
def ledger(tmp_path) -> SoldCompLedger:
    return SoldCompLedger(tmp_path / "sold.sqlite3")


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        microsecond=0).isoformat()


# ── Deliverable 1: model, persistence, immutability, entity linkage ───────────
def test_record_and_get_sold_comp(ledger):
    sc = ledger.record_sold_comp(
        entity_id="ent_a", source_id="S1", sold_price="12800",
        currency="jpy", sold_at=_iso(0), condition="near_mint", quantity=1,
    )
    assert sc.sold_price == Decimal("12800")
    assert sc.currency == "JPY"
    assert sc.sold_comp_id.startswith("sc_")
    loaded = ledger.get_sold_comp(sc.sold_comp_id)
    assert loaded == sc


def test_sold_comp_id_deterministic_dedups(ledger):
    kw = dict(entity_id="ent_a", source_id="S1", sold_price="100",
              sold_at="2026-06-20T00:00:00+00:00")
    a = ledger.record_sold_comp(**kw)
    b = ledger.record_sold_comp(**kw)  # identical → one immutable row
    assert a.sold_comp_id == b.sold_comp_id
    assert len(ledger.sold_comparables_for("ent_a")) == 1


def test_different_listing_id_is_new_comp(ledger):
    base = dict(entity_id="ent_a", source_id="S1", sold_price="100",
                sold_at="2026-06-20T00:00:00+00:00")
    ledger.record_sold_comp(listing_id="L1", **base)
    ledger.record_sold_comp(listing_id="L2", **base)
    assert len(ledger.sold_comparables_for("ent_a")) == 2


def test_currency_is_part_of_identity(ledger):
    base = dict(entity_id="ent_a", source_id="S1", sold_price="100",
                sold_at="2026-06-20T00:00:00+00:00")
    jpy = ledger.record_sold_comp(currency="JPY", **base)
    usd = ledger.record_sold_comp(currency="USD", **base)
    assert jpy.sold_comp_id != usd.sold_comp_id
    assert len(ledger.sold_comparables_for("ent_a")) == 2


def test_requires_entity_and_source(ledger):
    with pytest.raises(ValueError):
        ledger.record_sold_comp(entity_id="", source_id="S1", sold_price="1")
    with pytest.raises(ValueError):
        ledger.record_sold_comp(entity_id="ent_a", source_id="", sold_price="1")


def test_invalid_price_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="not-a-number")


def test_decimal_precision_preserved(ledger):
    sc = ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                 sold_price="1234.56", currency="USD")
    assert ledger.get_sold_comp(sc.sold_comp_id).sold_price == Decimal("1234.56")


def test_same_instant_different_offsets_dedups(ledger):
    a = ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                                sold_at="2026-06-21T00:30:00+09:00")
    b = ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                                sold_at="2026-06-20T15:30:00+00:00")
    assert a.sold_comp_id == b.sold_comp_id
    assert len(ledger.sold_comparables_for("ent_a")) == 1


def test_history_newest_first_and_since_filter(ledger):
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            sold_at="2026-06-01T00:00:00+00:00")
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="200",
                            sold_at="2026-06-15T00:00:00+00:00")
    history = ledger.sold_comparables_for("ent_a")
    assert [s.sold_price for s in history] == [Decimal("200"), Decimal("100")]
    recent = ledger.sold_comparables_for("ent_a", since="2026-06-10T00:00:00+00:00")
    assert len(recent) == 1 and recent[0].sold_price == Decimal("200")


def test_history_currency_scope(ledger):
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            currency="JPY")
    ledger.record_sold_comp(entity_id="ent_a", source_id="S2", sold_price="5",
                            currency="USD")
    assert len(ledger.sold_comparables_for("ent_a")) == 2
    assert len(ledger.sold_comparables_for("ent_a", currency="JPY")) == 1


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "persist.sqlite3"
    l1 = SoldCompLedger(path)
    sc = l1.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="999")
    l2 = SoldCompLedger(path)
    assert l2.get_sold_comp(sc.sold_comp_id).sold_price == Decimal("999")


def test_time_to_sale_days_property():
    sc = SoldComparable(
        sold_comp_id="sc_x", entity_id="ent_a", source_id="S1",
        sold_price=Decimal("100"), currency="JPY",
        sold_at="2026-06-10T00:00:00+00:00",
        listed_at="2026-06-05T00:00:00+00:00",
    )
    assert sc.time_to_sale_days == pytest.approx(5.0)
    no_listed = SoldComparable(
        sold_comp_id="sc_y", entity_id="ent_a", source_id="S1",
        sold_price=Decimal("100"), currency="JPY",
        sold_at="2026-06-10T00:00:00+00:00",
    )
    assert no_listed.time_to_sale_days is None


def test_build_sold_comp_id_stable():
    a = build_sold_comp_id(entity_id="e", source_id="S1", sold_at="t",
                           sold_price="1", currency="JPY")
    b = build_sold_comp_id(entity_id="e", source_id="S1", sold_at="t",
                           sold_price="1", currency="JPY")
    assert a == b and a.startswith("sc_")


# ── Deliverable 4: marketplace integration ────────────────────────────────────
def test_resolve_marketplace_from_host():
    assert resolve_marketplace("jp.mercari.com") == "mercari"
    assert resolve_marketplace("https://auctions.yahoo.co.jp/x") == "yahoo_auctions"
    assert resolve_marketplace("suruga-ya.jp") == "surugaya_buyback"
    assert resolve_marketplace("mercari") == "mercari"  # already canonical
    assert resolve_marketplace("totally-unknown.example") is None
    assert resolve_marketplace("") is None


def test_marketplace_vocabulary_documented():
    for mk in MARKETPLACES:
        assert resolve_marketplace(mk) == mk


def test_normalize_sold_event_mercari():
    raw = {"price": 12800, "soldDate": "2026-06-20T00:00:00+00:00",
           "created": "2026-06-10T00:00:00+00:00", "id": "m123",
           "item_condition": "near_mint", "entity_id": "ent_a", "source_id": "S1"}
    out = normalize_sold_event(raw, marketplace="jp.mercari.com")
    assert out["marketplace"] == "mercari"
    assert out["sold_price"] == 12800
    assert out["sold_at"] == "2026-06-20T00:00:00+00:00"
    assert out["listed_at"] == "2026-06-10T00:00:00+00:00"
    assert out["listing_id"] == "m123"
    assert out["condition"] == "near_mint"
    assert out["entity_id"] == "ent_a" and out["source_id"] == "S1"


def test_normalize_sold_event_yahoo_auctions():
    raw = {"winning_bid": 9000, "end_time": "2026-06-20T00:00:00+00:00",
           "auction_id": "y9"}
    out = normalize_sold_event(raw, marketplace="auctions.yahoo.co.jp")
    assert out["marketplace"] == "yahoo_auctions"
    assert out["sold_price"] == 9000
    assert out["listing_id"] == "y9"


def test_normalize_then_record_buyback_counts_as_sold(ledger):
    # Suruga-ya buyback quote is a transaction the shop honors → a sold comp.
    raw = {"buyback_price": 5000, "quoted_at": "2026-06-20T00:00:00+00:00",
           "product_id": "p1", "entity_id": "ent_a", "source_id": "S_suruga"}
    out = normalize_sold_event(raw, marketplace="suruga-ya.jp")
    sc = ledger.record_sold_comp(**out)
    assert sc.marketplace == "surugaya_buyback"
    assert sc.sold_price == Decimal("5000")


# ── Deliverable 2: liquidity metrics ──────────────────────────────────────────
def test_metrics_vocabulary_present():
    assert set(LIQUIDITY_METRICS) >= {
        "sales_per_day", "sales_per_week", "inventory_turnover",
        "median_time_to_sale_days", "sell_through_rate", "listing_to_sale_ratio",
    }


def test_compute_metrics_basic_cadence(ledger):
    for d in (1, 5, 10):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(d))
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30)
    assert m.sold_count == 3
    assert m.sales_per_day == pytest.approx(3 / 30)
    assert m.sales_per_week == pytest.approx(3 / 30 * 7)
    assert m.currency == "JPY"


def test_compute_metrics_sell_through_and_turnover(ledger):
    for d in (1, 2, 3):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(d))
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30,
                                  active_listing_count=1)
    assert m.sell_through_rate == pytest.approx(3 / 4)
    assert m.inventory_turnover == pytest.approx(3 / 1)


def test_compute_metrics_listing_to_sale_ratio(ledger):
    for d in (1, 2):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(d))
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30,
                                  active_listing_prices=["150", "150"])
    assert m.listing_to_sale_ratio == Decimal("1.5")


def test_compute_metrics_median_time_to_sale(ledger):
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            sold_at=_iso(1), listed_at=_iso(3))   # 2 days
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            sold_at=_iso(2), listed_at=_iso(10))  # 8 days
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30)
    assert m.median_time_to_sale_days == pytest.approx(5.0)


def test_compute_metrics_sparse_safe():
    m = compute_liquidity_metrics("ent_unknown", [], window_days=30)
    assert m.sold_count == 0
    assert m.sales_per_day == 0.0
    assert m.median_time_to_sale_days is None
    assert m.sell_through_rate is None
    assert m.inventory_turnover is None
    assert m.listing_to_sale_ratio is None


def test_compute_metrics_window_excludes_old(ledger):
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            sold_at=_iso(5))
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            sold_at=_iso(60))
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30)
    assert m.sold_count == 1


# ── Deliverable 3: liquidity curve ────────────────────────────────────────────
def test_build_curve_basic(ledger):
    for p in ("100", "200", "300"):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price=p,
                                sold_at=_iso(1))
    comps = ledger.sold_comparables_for("ent_a")
    curve = build_liquidity_curve("ent_a", comps, active_listing_prices=["400"])
    assert curve.has_data
    assert curve.sample_count == 4  # 3 sold + 1 active
    # at the lowest ceiling, only sold observations exist → prob 1.0
    assert curve.points[0].probability_of_sale == pytest.approx(1.0)
    # the top ceiling includes the unsold active listing → prob < 1.0
    assert curve.points[-1].probability_of_sale < 1.0
    # ceilings are monotonically increasing
    ceilings = [p.price_ceiling for p in curve.points]
    assert ceilings == sorted(ceilings)


def test_build_curve_sparse_safe():
    curve = build_liquidity_curve("ent_unknown", [])
    assert not curve.has_data
    assert curve.points == ()
    assert curve.sample_count == 0


def test_build_curve_expected_days(ledger):
    ledger.record_sold_comp(entity_id="ent_a", source_id="S1", sold_price="100",
                            sold_at=_iso(1), listed_at=_iso(4))  # 3 days
    comps = ledger.sold_comparables_for("ent_a")
    curve = build_liquidity_curve("ent_a", comps)
    assert curve.points[0].expected_days_to_sale == pytest.approx(3.0)


def test_build_curve_respects_max_points(ledger):
    for i in range(20):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price=str(100 + i), sold_at=_iso(1),
                                listing_id=f"L{i}")
    comps = ledger.sold_comparables_for("ent_a")
    curve = build_liquidity_curve("ent_a", comps, max_points=5)
    assert len(curve.points) <= 5


# ── Deliverable 5: liquidity-aware opportunity signals ────────────────────────
def test_signal_vocabulary_documented():
    assert set(LIQUIDITY_SIGNALS) >= {
        "cheap_and_liquid", "cheap_but_illiquid",
        "price_spike_without_liquidity", "liquidity_surge",
        "fairly_priced_liquid", "insufficient_data",
    }


def _make_metrics(ledger, n, *, days, active=None):
    for i in range(n):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(days),
                                listing_id=f"L{i}")
    comps = ledger.sold_comparables_for("ent_a")
    return compute_liquidity_metrics("ent_a", comps, window_days=30,
                                     active_listing_count=active)


def test_is_liquid_insufficient_data(ledger):
    m = _make_metrics(ledger, 2, days=1)
    assert is_liquid(m) is None


def test_is_liquid_true_on_sell_through(ledger):
    m = _make_metrics(ledger, 5, days=1, active=1)
    assert is_liquid(m) is True


def test_is_liquid_false_when_illiquid(ledger):
    # 3 sales but spread over the window with many active listings → low cadence
    # and low sell-through.
    for i in range(3):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(20),
                                listing_id=f"L{i}")
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30,
                                  active_listing_count=100)
    assert is_liquid(m) is False


def test_classify_insufficient_data(ledger):
    m = _make_metrics(ledger, 1, days=1)
    sig = classify_liquidity_signal(m)
    assert sig.signal == "insufficient_data"
    assert sig.is_liquid is None


def test_classify_cheap_and_liquid(ledger):
    m = _make_metrics(ledger, 5, days=1, active=1)
    sig = classify_liquidity_signal(m, is_cheap=True)
    assert sig.signal == "cheap_and_liquid"
    assert sig.is_liquid is True


def test_classify_cheap_but_illiquid(ledger):
    for i in range(3):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(20),
                                listing_id=f"L{i}")
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30,
                                  active_listing_count=100)
    sig = classify_liquidity_signal(m, is_cheap=True)
    assert sig.signal == "cheap_but_illiquid"
    assert sig.is_liquid is False


def test_classify_price_spike_without_liquidity(ledger):
    for i in range(3):
        ledger.record_sold_comp(entity_id="ent_a", source_id="S1",
                                sold_price="100", sold_at=_iso(20),
                                listing_id=f"L{i}")
    comps = ledger.sold_comparables_for("ent_a")
    m = compute_liquidity_metrics("ent_a", comps, window_days=30,
                                  active_listing_count=100)
    sig = classify_liquidity_signal(m, price_rising=True)
    assert sig.signal == "price_spike_without_liquidity"


def test_classify_liquidity_surge(ledger):
    m = _make_metrics(ledger, 5, days=1, active=1)
    sig = classify_liquidity_signal(m, liquidity_rising=True)
    assert sig.signal == "liquidity_surge"


def test_classify_fairly_priced_liquid(ledger):
    m = _make_metrics(ledger, 5, days=1, active=1)
    sig = classify_liquidity_signal(m)
    assert sig.signal == "fairly_priced_liquid"
    assert sig.is_liquid is True
