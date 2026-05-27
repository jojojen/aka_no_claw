"""Tests for GoogleTrendsTracker (C4)."""

from __future__ import annotations

import pytest

from openclaw_adapter.ip_heat_store import IpHeatStore
from openclaw_adapter.google_trends_tracker import GoogleTrendsTracker


@pytest.fixture
def store(tmp_path):
    return IpHeatStore(tmp_path / "heat.sqlite3")


# ── GoogleTrendsTracker (all network calls mocked via _fetch_interest) ─────


def test_track_ip_records_signal(store, monkeypatch):
    tracker = GoogleTrendsTracker(store)
    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {"チェンソーマン": 75.0})

    sig = tracker.track_ip(ip_canonical="chainsaw man", keywords=["チェンソーマン"])
    assert sig is not None
    assert sig.source == "google_trends"
    assert sig.value == 75.0
    assert sig.ip_canonical == "chainsaw man"
    assert sig.window_days == 7


def test_track_ip_uses_max_across_keywords(store, monkeypatch):
    tracker = GoogleTrendsTracker(store)
    monkeypatch.setattr(
        tracker, "_fetch_interest",
        lambda kws: {"チェンソーマン": 60.0, "chainsawman": 80.0},
    )
    sig = tracker.track_ip(ip_canonical="chainsaw man", keywords=["チェンソーマン", "chainsawman"])
    assert sig is not None
    assert sig.value == 80.0   # max, not sum


def test_track_ip_all_fail_returns_none(store, monkeypatch):
    tracker = GoogleTrendsTracker(store)
    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {})
    sig = tracker.track_ip(ip_canonical="fail_ip", keywords=["keyword"])
    assert sig is None


def test_track_ip_normalises_canonical(store, monkeypatch):
    tracker = GoogleTrendsTracker(store)
    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {"k": 50.0})
    sig = tracker.track_ip(ip_canonical="  DEMON SLAYER  ", keywords=["k"])
    assert sig is not None
    assert sig.ip_canonical == "demon slayer"


def test_get_interest_returns_value(store, monkeypatch):
    tracker = GoogleTrendsTracker(store)
    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {"テスト": 42.5})
    assert tracker.get_interest("テスト") == 42.5


def test_get_interest_missing_key_returns_none(store, monkeypatch):
    tracker = GoogleTrendsTracker(store)
    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {})
    assert tracker.get_interest("テスト") is None


def test_track_ip_percentile_computed(store, monkeypatch):
    """Two sequential records — second one should have a non-None percentile."""
    tracker = GoogleTrendsTracker(store)

    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {"k": 30.0})
    tracker.track_ip(ip_canonical="ip_pct", keywords=["k"])

    monkeypatch.setattr(tracker, "_fetch_interest", lambda kws: {"k": 80.0})
    sig2 = tracker.track_ip(ip_canonical="ip_pct", keywords=["k"])
    assert sig2 is not None
    assert sig2.percentile == 100.0   # 80 is the highest seen


def test_track_ip_geo_override(store, monkeypatch):
    """geo= override is respected for the call duration then reverted."""
    calls: list[str] = []
    orig_geo = "JP"

    def fake_fetch(kws):
        calls.append(tracker._geo)
        return {"k": 50.0}

    tracker = GoogleTrendsTracker(store, geo=orig_geo)
    monkeypatch.setattr(tracker, "_fetch_interest", fake_fetch)

    tracker.track_ip(ip_canonical="ip", keywords=["k"], geo="US")
    assert calls == ["US"]
    assert tracker._geo == orig_geo   # restored after call
