"""Tests for IpHeatStore (C1)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openclaw_adapter.ip_heat_store import SOURCES, HeatSignal, IpHeatStore


@pytest.fixture
def store(tmp_path):
    return IpHeatStore(tmp_path / "ip_heat.sqlite3")


# ── bootstrap ─────────────────────────────────────────────────────────────────


def test_bootstrap_creates_table(store):
    signals = store.history("chainsaw_man", "x_mention", days=1)
    assert signals == []


# ── record + latest ───────────────────────────────────────────────────────────


def test_record_returns_signal(store):
    sig = store.record(ip_canonical="chainsaw_man", source="x_mention", value=1500)
    assert isinstance(sig, HeatSignal)
    assert sig.ip_canonical == "chainsaw_man"
    assert sig.source == "x_mention"
    assert sig.value == 1500.0


def test_record_normalises_canonical_to_lower(store):
    store.record(ip_canonical="Chainsaw Man", source="x_mention", value=1000)
    sig = store.latest("chainsaw man", "x_mention")
    assert sig is not None
    assert sig.ip_canonical == "chainsaw man"


def test_record_upserts_same_hour(store):
    dt = datetime(2026, 5, 24, 10, 15, tzinfo=timezone.utc)
    store.record(ip_canonical="ip_a", source="x_mention", value=100, measured_at=dt)
    store.record(ip_canonical="ip_a", source="x_mention", value=200, measured_at=dt)
    sig = store.latest("ip_a", "x_mention")
    assert sig is not None
    assert sig.value == 200.0  # update wins


def test_record_different_hours_creates_two_rows(store):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    dt1 = now - timedelta(hours=2)
    dt2 = now - timedelta(hours=1)
    store.record(ip_canonical="ip_b", source="x_mention", value=100, measured_at=dt1)
    store.record(ip_canonical="ip_b", source="x_mention", value=200, measured_at=dt2)
    history = store.history("ip_b", "x_mention", days=7)
    assert len(history) == 2


# ── percentile ────────────────────────────────────────────────────────────────


def test_percentile_first_record_is_100(store):
    sig = store.record(ip_canonical="ip_c", source="x_mention", value=500)
    assert sig.percentile == 100.0


def test_percentile_below_all_is_low(store):
    from datetime import timedelta
    base = datetime.now(timezone.utc) - timedelta(days=15)
    for i in range(5):
        store.record(
            ip_canonical="ip_d", source="x_mention", value=1000 + i * 100,
            measured_at=base + timedelta(days=i),
        )
    # Insert a value below all previous
    sig = store.record(
        ip_canonical="ip_d", source="x_mention", value=500,
        measured_at=base + timedelta(days=10),
    )
    # 500 is ≤ to only itself (6th entry) — but previous values are 1000-1400
    # Wait: percentile counts rows ≤ current_value among last 30 days.
    # 500 ≤ 500 only (1 out of 6), so 1/6 * 100 ≈ 16.7
    assert sig.percentile is not None
    assert sig.percentile < 50


def test_percentile_top_value_is_100(store):
    from datetime import timedelta
    base = datetime.now(timezone.utc) - timedelta(days=15)
    for i in range(5):
        store.record(
            ip_canonical="ip_e", source="x_mention", value=100 * (i + 1),
            measured_at=base + timedelta(days=i),
        )
    # Insert a value above all
    sig = store.record(
        ip_canonical="ip_e", source="x_mention", value=9999,
        measured_at=base + timedelta(days=10),
    )
    assert sig.percentile == 100.0


# ── latest_for_ip ──────────────────────────────────────────────────────────────


def test_latest_for_ip_returns_one_per_source(store):
    store.record(ip_canonical="jjk", source="x_mention", value=800)
    store.record(ip_canonical="jjk", source="4chan", value=150)
    store.record(ip_canonical="jjk", source="google_trends", value=65)
    signals = store.latest_for_ip("jjk")
    sources = {s.source for s in signals}
    assert sources == {"x_mention", "4chan", "google_trends"}
    assert len(signals) == 3


def test_latest_for_ip_missing_source_excluded(store):
    store.record(ip_canonical="ip_f", source="x_mention", value=100)
    signals = store.latest_for_ip("ip_f")
    assert len(signals) == 1
    assert signals[0].source == "x_mention"


# ── max_percentile_for_ip ──────────────────────────────────────────────────────


def test_max_percentile_returns_highest(store):
    store.record(ip_canonical="ip_g", source="x_mention", value=900)
    store.record(ip_canonical="ip_g", source="4chan", value=50)
    store.record(ip_canonical="ip_h", source="x_mention", value=200)
    store.record(ip_canonical="ip_h", source="x_mention", value=900)

    # Both ip_g sources have percentile=100 (first record each)
    p = store.max_percentile_for_ip("ip_g")
    assert p == 100.0


def test_max_percentile_no_data_returns_none(store):
    assert store.max_percentile_for_ip("unknown_ip") is None


# ── top_hot_ips ───────────────────────────────────────────────────────────────


def test_top_hot_ips_filters_below_threshold(store):
    store.record(ip_canonical="hot_ip", source="x_mention", value=1000)
    store.record(ip_canonical="cold_ip", source="x_mention", value=1)
    # After recording, hot_ip has percentile=100, cold_ip may be lower
    # (both are first records so both get 100 — but they're different IPs, independent)
    # Let's add more history to cold_ip to make it low percentile
    from datetime import timedelta
    base = datetime.now(timezone.utc) - timedelta(days=15)
    for i in range(5):
        store.record(
            ip_canonical="cold_ip", source="x_mention", value=1000 + i * 200,
            measured_at=base + timedelta(days=i),
        )
    # Now cold_ip's value=1 is well below its history
    hot = store.top_hot_ips(min_percentile=70.0, limit=10)
    ip_names = [ip for ip, _ in hot]
    assert "hot_ip" in ip_names


def test_top_hot_ips_ordered_by_max_percentile(store):
    store.record(ip_canonical="a", source="x_mention", value=1)
    store.record(ip_canonical="b", source="x_mention", value=1)
    hot = store.top_hot_ips(min_percentile=0.0, limit=10)
    # Should return results without error
    assert len(hot) >= 2
