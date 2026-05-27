"""Tests for RedditActivityTracker (C3)."""

from __future__ import annotations

import pytest

from openclaw_adapter.ip_heat_store import IpHeatStore
from openclaw_adapter.reddit_activity_tracker import (
    RedditActivityTracker,
    _Post,
    _activity_score,
    _parse_posts,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _fake_listing(posts: list[dict]) -> str:
    import json
    children = [{"kind": "t3", "data": p} for p in posts]
    return json.dumps({"data": {"children": children}})


def _posts(*scores: int) -> list[_Post]:
    return [_Post(score=s, num_comments=0, title=f"post{i}") for i, s in enumerate(scores)]


@pytest.fixture
def store(tmp_path):
    return IpHeatStore(tmp_path / "heat.sqlite3")


# ── _parse_posts ─────────────────────────────────────────────────────────


def test_parse_posts_basic():
    body = _fake_listing([
        {"score": 100, "num_comments": 5, "title": "great post", "stickied": False},
        {"score": 50, "num_comments": 2, "title": "ok post", "stickied": False},
    ])
    posts = _parse_posts(body)
    assert len(posts) == 2
    assert posts[0]["score"] == 100
    assert posts[1]["title"] == "ok post"


def test_parse_posts_skips_stickied():
    body = _fake_listing([
        {"score": 1000, "num_comments": 0, "title": "pinned rules", "stickied": True},
        {"score": 50, "num_comments": 3, "title": "real post", "stickied": False},
    ])
    posts = _parse_posts(body)
    assert len(posts) == 1
    assert posts[0]["score"] == 50


def test_parse_posts_empty_listing():
    import json
    body = json.dumps({"data": {"children": []}})
    assert _parse_posts(body) == []


def test_parse_posts_bad_json():
    assert _parse_posts("not json") == []


# ── _activity_score ────────────────────────────────────────────────────────


def test_activity_score_sums_post_scores():
    assert _activity_score(_posts(100, 200, 50)) == 350.0


def test_activity_score_empty():
    assert _activity_score([]) == 0.0


# ── RedditActivityTracker ──────────────────────────────────────────────────


def test_track_ip_via_search(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": _posts(100, 200))
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: None)

    sig = tracker.track_ip(ip_canonical="chainsaw man", queries=["チェンソーマン"])
    assert sig is not None
    assert sig.source == "reddit"
    assert sig.value == 300.0
    assert sig.ip_canonical == "chainsaw man"


def test_track_ip_via_subreddit(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": None)
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: _posts(500, 300))

    sig = tracker.track_ip(ip_canonical="jujutsu kaisen", subreddits=["jujutsushi"])
    assert sig is not None
    assert sig.value == 800.0


def test_track_ip_sums_queries_and_subreddits(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": _posts(100))
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: _posts(200, 300))

    sig = tracker.track_ip(
        ip_canonical="test_ip",
        queries=["query1", "query2"],
        subreddits=["sub1"],
    )
    assert sig is not None
    # 2 queries × 100 + 1 subreddit × 500
    assert sig.value == 700.0


def test_track_ip_all_fail_returns_none(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": None)
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: None)

    sig = tracker.track_ip(ip_canonical="fail_ip", queries=["q1"], subreddits=["sub1"])
    assert sig is None


def test_track_ip_partial_failure_uses_available(store, monkeypatch):
    tracker = RedditActivityTracker(store)

    def fake_search(q, window="week"):
        return _posts(100) if q == "good" else None

    monkeypatch.setattr(tracker, "_fetch_search", fake_search)
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: None)

    sig = tracker.track_ip(ip_canonical="partial", queries=["good", "bad"])
    assert sig is not None
    assert sig.value == 100.0


def test_track_ip_normalises_canonical(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": _posts(50))
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: None)

    sig = tracker.track_ip(ip_canonical="  Demon Slayer  ", queries=["鬼滅の刃"])
    assert sig is not None
    assert sig.ip_canonical == "demon slayer"


def test_track_ip_window_days_mapping(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": _posts(1))
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: None)

    sig = tracker.track_ip(ip_canonical="ip_w", queries=["q"], window="month")
    assert sig is not None
    assert sig.window_days == 30


def test_measure_search_activity(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": _posts(10, 20, 30))
    assert tracker.measure_search_activity("test") == 60.0


def test_measure_search_activity_failure(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_search", lambda q, window="week": None)
    assert tracker.measure_search_activity("test") == 0.0


def test_measure_subreddit_activity(store, monkeypatch):
    tracker = RedditActivityTracker(store)
    monkeypatch.setattr(tracker, "_fetch_subreddit", lambda s: _posts(100, 200))
    assert tracker.measure_subreddit_activity("test_sub") == 300.0
