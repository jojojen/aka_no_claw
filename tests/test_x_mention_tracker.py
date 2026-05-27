"""Tests for XMentionTracker (C2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from openclaw_adapter.ip_heat_store import IpHeatStore
from openclaw_adapter.x_mention_tracker import XMentionTracker, _count_items_in_window


def _make_rss(*, items: list[datetime | None]) -> str:
    """Build minimal RSS XML with items at the given pubDates (None → no pubDate)."""
    item_xml = ""
    for dt in items:
        if dt is not None:
            pub = format_datetime(dt, usegmt=True)
            item_xml += f"<item><title>tweet</title><pubDate>{pub}</pubDate></item>\n"
        else:
            item_xml += "<item><title>tweet no date</title></item>\n"
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{item_xml}</channel></rss>'


@pytest.fixture
def store(tmp_path):
    return IpHeatStore(tmp_path / "heat.sqlite3")


# ── _count_items_in_window (pure parsing logic) ────────────────────────────


def test_count_items_recent_only():
    now = datetime.now(timezone.utc)
    rss = _make_rss(items=[
        now - timedelta(days=1),
        now - timedelta(days=3),
        now - timedelta(days=8),   # outside 7-day window
    ])
    assert _count_items_in_window(rss, days=7) == 2


def test_count_items_all_old():
    now = datetime.now(timezone.utc)
    rss = _make_rss(items=[now - timedelta(days=10), now - timedelta(days=20)])
    assert _count_items_in_window(rss, days=7) == 0


def test_count_items_no_date_counts_as_recent():
    rss = _make_rss(items=[None, None])
    assert _count_items_in_window(rss, days=7) == 2


def test_count_items_empty_feed():
    rss = _make_rss(items=[])
    assert _count_items_in_window(rss, days=7) == 0


def test_count_items_invalid_xml():
    assert _count_items_in_window("not xml at all", days=7) == 0


def test_count_items_boundary_included():
    now = datetime.now(timezone.utc)
    # exactly at the cutoff boundary
    rss = _make_rss(items=[now - timedelta(days=7, seconds=-1)])
    assert _count_items_in_window(rss, days=7) == 1


# ── XMentionTracker ───────────────────────────────────────────────────────


def test_track_ip_records_heat_signal(store, monkeypatch):
    now = datetime.now(timezone.utc)
    rss = _make_rss(items=[now - timedelta(hours=2), now - timedelta(hours=5)])
    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: rss)

    sig = tracker.track_ip(ip_canonical="test_ip", hashtags=["テスト"])
    assert sig is not None
    assert sig.source == "x_mention"
    assert sig.value == 2.0
    assert sig.ip_canonical == "test_ip"


def test_track_ip_sums_across_hashtags(store, monkeypatch):
    now = datetime.now(timezone.utc)
    rss2 = _make_rss(items=[now - timedelta(hours=1), now - timedelta(hours=2)])
    rss3 = _make_rss(items=[now - timedelta(hours=3), now - timedelta(hours=4), now - timedelta(hours=5)])

    def fake_fetch(tag):
        return rss2 if tag == "tag1" else rss3

    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", fake_fetch)

    sig = tracker.track_ip(ip_canonical="multi_ip", hashtags=["tag1", "tag2"])
    assert sig is not None
    assert sig.value == 5.0   # 2 + 3


def test_track_ip_partial_failure_uses_available(store, monkeypatch):
    now = datetime.now(timezone.utc)
    rss = _make_rss(items=[now - timedelta(hours=1)])

    def fake_fetch(tag):
        return rss if tag == "good_tag" else None

    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", fake_fetch)

    sig = tracker.track_ip(ip_canonical="partial_ip", hashtags=["good_tag", "bad_tag"])
    assert sig is not None
    assert sig.value == 1.0


def test_track_ip_all_fail_returns_none(store, monkeypatch):
    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: None)
    sig = tracker.track_ip(ip_canonical="fail_ip", hashtags=["#fail1", "#fail2"])
    assert sig is None


def test_track_ip_normalises_canonical(store, monkeypatch):
    now = datetime.now(timezone.utc)
    rss = _make_rss(items=[now - timedelta(hours=1)])
    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: rss)

    sig = tracker.track_ip(ip_canonical="  CHAINSAW MAN  ", hashtags=["チェンソーマン"])
    assert sig is not None
    assert sig.ip_canonical == "chainsaw man"


def test_count_hashtag_mentions_delegates_to_fetch(store, monkeypatch):
    now = datetime.now(timezone.utc)
    rss = _make_rss(items=[now - timedelta(hours=i) for i in range(1, 6)])
    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: rss)
    assert tracker.count_hashtag_mentions("テスト", days=7) == 5


def test_count_hashtag_mentions_no_rss_returns_zero(store, monkeypatch):
    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: None)
    assert tracker.count_hashtag_mentions("テスト", days=7) == 0


def test_track_ip_percentile_increases_with_higher_value(store, monkeypatch):
    """Second record with higher value should have percentile > 50%."""
    now = datetime.now(timezone.utc)
    rss_low = _make_rss(items=[now - timedelta(hours=1)])   # count=1
    rss_high = _make_rss(items=[now - timedelta(hours=i) for i in range(1, 10)])  # count=9

    tracker = XMentionTracker(store)
    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: rss_low)
    tracker.track_ip(ip_canonical="ip_pct", hashtags=["tag"])

    monkeypatch.setattr(tracker, "_fetch_hashtag_rss", lambda tag: rss_high)
    sig2 = tracker.track_ip(ip_canonical="ip_pct", hashtags=["tag"])
    assert sig2 is not None
    assert sig2.percentile is not None
    assert sig2.percentile > 50
