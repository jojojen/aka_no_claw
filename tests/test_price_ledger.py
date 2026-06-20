"""Tests for the Canonical Price Observation Ledger & Market Snapshot API (#13)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from openclaw_adapter.price_ledger import (
    PriceLedger,
    build_observation_id,
)


@pytest.fixture()
def ledger(tmp_path) -> PriceLedger:
    return PriceLedger(tmp_path / "price.sqlite3")


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        microsecond=0).isoformat()


# ── Deliverable 1: model + immutability ──────────────────────────────────────
def test_record_and_get_observation(ledger):
    obs = ledger.record_observation(
        entity_id="ent_pokemon_sv3_201108_raw", source_id="S1",
        price_amount="12800", currency="jpy", quote_type="listing",
        condition="near_mint", quantity=1, observed_at=_iso(0),
    )
    assert obs.price_amount == Decimal("12800")
    assert obs.currency == "JPY"
    loaded = ledger.get_observation(obs.observation_id)
    assert loaded == obs


def test_observation_id_deterministic_dedups(ledger):
    kw = dict(entity_id="ent_a", source_id="S1", price_amount="100",
              quote_type="listing", observed_at="2026-06-20T00:00:00+00:00")
    a = ledger.record_observation(**kw)
    b = ledger.record_observation(**kw)  # identical → same immutable row
    assert a.observation_id == b.observation_id
    assert len(ledger.observations_for("ent_a")) == 1


def test_different_price_is_new_observation(ledger):
    base = dict(entity_id="ent_a", source_id="S1", quote_type="listing",
                observed_at="2026-06-20T00:00:00+00:00")
    ledger.record_observation(price_amount="100", **base)
    ledger.record_observation(price_amount="120", **base)
    assert len(ledger.observations_for("ent_a")) == 2


def test_decimal_precision_preserved(ledger):
    obs = ledger.record_observation(entity_id="ent_a", source_id="S1",
                                    price_amount="1234.56", currency="USD")
    loaded = ledger.get_observation(obs.observation_id)
    assert loaded.price_amount == Decimal("1234.56")


def test_requires_entity_and_source(ledger):
    with pytest.raises(ValueError):
        ledger.record_observation(entity_id="", source_id="S1", price_amount="1")
    with pytest.raises(ValueError):
        ledger.record_observation(entity_id="ent_a", source_id="", price_amount="1")


def test_invalid_price_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.record_observation(entity_id="ent_a", source_id="S1",
                                  price_amount="not-a-number")


def test_quote_type_vocab_snaps(ledger):
    obs = ledger.record_observation(entity_id="ent_a", source_id="S1",
                                    price_amount="1", quote_type="weird")
    assert obs.quote_type == "listing"


def test_multiple_quote_types_supported(ledger):
    for qt in ("listing", "sold", "official_retail", "auction_result"):
        ledger.record_observation(entity_id="ent_a", source_id="S1",
                                  price_amount="100", quote_type=qt,
                                  observed_at=_iso(0))
    mix = ledger.get_market_snapshot("ent_a").quote_type_mix
    assert set(mix) == {"listing", "sold", "official_retail", "auction_result"}


# ── Deliverable 2: snapshot ──────────────────────────────────────────────────
def test_snapshot_stats(ledger):
    for p, d in (("100", 3), ("200", 2), ("300", 1)):
        ledger.record_observation(entity_id="ent_a", source_id="S1",
                                  price_amount=p, observed_at=_iso(d))
    snap = ledger.get_market_snapshot("ent_a")
    assert snap.count == 3
    assert snap.min_price == Decimal("100")
    assert snap.max_price == Decimal("300")
    assert snap.median_price == Decimal("200")
    assert snap.latest_price == Decimal("300")        # newest (1 day ago)
    assert snap.freshness_seconds is not None and snap.freshness_seconds >= 0


def test_snapshot_median_even_count(ledger):
    for p in ("100", "200", "300", "400"):
        ledger.record_observation(entity_id="ent_a", source_id="S1",
                                  price_amount=p, observed_at=_iso(float(p)))
    snap = ledger.get_market_snapshot("ent_a")
    assert snap.median_price == Decimal("250")        # (200+300)/2


def test_snapshot_sparse_and_empty(ledger):
    empty = ledger.get_market_snapshot("ent_unknown")
    assert empty.count == 0
    assert empty.min_price is None and empty.median_price is None
    assert empty.latest_observations == ()

    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="50")
    one = ledger.get_market_snapshot("ent_a")
    assert one.count == 1
    assert one.min_price == one.max_price == one.median_price == Decimal("50")


def test_currency_is_part_of_observation_identity(ledger):
    # Same entity/source/time/price/quote but different currency → distinct rows.
    base = dict(entity_id="ent_a", source_id="S1", price_amount="100",
                quote_type="listing", observed_at="2026-06-20T00:00:00+00:00")
    jpy = ledger.record_observation(currency="JPY", **base)
    usd = ledger.record_observation(currency="USD", **base)
    assert jpy.observation_id != usd.observation_id
    assert {o.currency for o in ledger.observations_for("ent_a")} == {"JPY", "USD"}
    assert len(ledger.observations_for("ent_a")) == 2


def test_observed_at_ordering_across_timezone_offsets(ledger):
    # +09:00 00:30 == 15:30 UTC, which is EARLIER than 16:00 UTC. Raw-string
    # ordering would wrongly call the +09:00 row newest.
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                              observed_at="2026-06-21T00:30:00+09:00")
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="200",
                              observed_at="2026-06-20T16:00:00+00:00")
    snap = ledger.get_market_snapshot("ent_a")
    assert snap.latest_price == Decimal("200")
    assert snap.latest_observed_at == "2026-06-20T16:00:00+00:00"
    # newest-first history reflects true chronology too
    assert [o.price_amount for o in ledger.observations_for("ent_a")] == [
        Decimal("200"), Decimal("100"),
    ]


def test_same_instant_different_offsets_dedups(ledger):
    # 00:30+09:00 and 15:30+00:00 are the same instant → one immutable row.
    a = ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                                  observed_at="2026-06-21T00:30:00+09:00")
    b = ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                                  observed_at="2026-06-20T15:30:00+00:00")
    assert a.observation_id == b.observation_id
    assert len(ledger.observations_for("ent_a")) == 1


def test_snapshot_currency_scope(ledger):
    ledger.record_observation(entity_id="ent_a", source_id="S1",
                              price_amount="100", currency="JPY")
    ledger.record_observation(entity_id="ent_a", source_id="S2",
                              price_amount="5", currency="USD")
    mixed = ledger.get_market_snapshot("ent_a")
    assert mixed.currency is None                      # mixed currencies → unscoped
    scoped = ledger.get_market_snapshot("ent_a", currency="JPY")
    assert scoped.currency == "JPY" and scoped.count == 1


# ── Deliverable 3: provenance ────────────────────────────────────────────────
def test_snapshot_shows_contributing_sources(ledger):
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                              observed_at=_iso(2))
    ledger.record_observation(entity_id="ent_a", source_id="S2", price_amount="110",
                              observed_at=_iso(1))
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="105",
                              observed_at=_iso(0))
    snap = ledger.get_market_snapshot("ent_a")
    assert set(snap.source_ids) == {"S1", "S2"}
    # provenance retained on every observation
    assert all(o.source_id for o in snap.latest_observations)


# ── Deliverable 4: time-series ───────────────────────────────────────────────
def test_history_queryable_with_since_filter(ledger):
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                              observed_at="2026-06-01T00:00:00+00:00")
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="200",
                              observed_at="2026-06-15T00:00:00+00:00")
    recent = ledger.observations_for("ent_a", since="2026-06-10T00:00:00+00:00")
    assert len(recent) == 1 and recent[0].price_amount == Decimal("200")


def test_daily_aggregation(ledger):
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                              observed_at="2026-06-20T01:00:00+00:00")
    ledger.record_observation(entity_id="ent_a", source_id="S2", price_amount="300",
                              observed_at="2026-06-20T20:00:00+00:00")
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="150",
                              observed_at="2026-06-21T05:00:00+00:00")
    buckets = ledger.aggregate_series("ent_a", bucket="day")
    assert [b.bucket for b in buckets] == ["2026-06-20", "2026-06-21"]
    day0 = buckets[0]
    assert day0.count == 2 and day0.min_price == Decimal("100") and day0.max_price == Decimal("300")
    assert day0.median_price == Decimal("200") and day0.avg_price == Decimal("200")


def test_weekly_aggregation(ledger):
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="100",
                              observed_at="2026-06-15T00:00:00+00:00")  # ISO week 25
    ledger.record_observation(entity_id="ent_a", source_id="S1", price_amount="200",
                              observed_at="2026-06-22T00:00:00+00:00")  # ISO week 26
    buckets = ledger.aggregate_series("ent_a", bucket="week")
    assert [b.bucket for b in buckets] == ["2026-W25", "2026-W26"]


def test_aggregate_rejects_bad_bucket(ledger):
    with pytest.raises(ValueError):
        ledger.aggregate_series("ent_a", bucket="month")


# ── persistence ──────────────────────────────────────────────────────────────
def test_persistence_across_instances(tmp_path):
    path = tmp_path / "persist.sqlite3"
    l1 = PriceLedger(path)
    obs = l1.record_observation(entity_id="ent_a", source_id="S1", price_amount="999")
    l2 = PriceLedger(path)
    assert l2.get_observation(obs.observation_id).price_amount == Decimal("999")


def test_build_observation_id_stable():
    a = build_observation_id(entity_id="e", source_id="S1", observed_at="t",
                             price_amount="1", quote_type="listing")
    b = build_observation_id(entity_id="e", source_id="S1", observed_at="t",
                             price_amount="1", quote_type="listing")
    assert a == b and a.startswith("obs_")
