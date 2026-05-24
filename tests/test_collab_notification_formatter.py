"""Tests for collab_notification_formatter (D5)."""

from __future__ import annotations

import pytest

from openclaw_adapter.collab_notification_formatter import (
    CollabNotification,
    StoreListingInfo,
    collab_notification_from_dual_signal,
    format_collab_notification,
)
from openclaw_adapter.collab_similarity_provider import (
    CollabInference,
    SimilarCase,
)


def _minimal_notification(**kw) -> CollabNotification:
    defaults = dict(
        ip_canonical="chainsaw man",
        tcg_game="union_arena",
        product_name="UNION ARENA EX チェンソーマン",
    )
    defaults.update(kw)
    return CollabNotification(**defaults)


def _make_inference(n: int = 3, mean_180: float = 80.0, win_rate: float = 1.0) -> CollabInference:
    cases = tuple(
        SimilarCase(
            case_id=f"case{i}",
            ip_canonical="demon slayer",
            tcg_game="weiss_schwarz",
            product_name="WS DS",
            announce_date="2021-05-01",
            profit_pct_30d=50.0,
            profit_pct_180d=80.0,
            similarity_score=3.0,
        )
        for i in range(n)
    )
    return CollabInference(
        query_ip="chainsaw man",
        query_tcg="union_arena",
        n_samples=n,
        similar_cases=cases,
        mean_profit_pct_180d=mean_180,
        win_rate_180d=win_rate,
        best_profit_pct_180d=180.0,
        worst_profit_pct_180d=-12.0,
        mean_profit_pct_30d=50.0,
        win_rate_30d=1.0,
    )


# ── headline ───────────────────────────────────────────────────────────────────


def test_headline_dual_signal_when_heat_above_70():
    n = _minimal_notification(ip_heat_percentile=87.0)
    out = format_collab_notification(n)
    assert "🔥" in out
    assert "雙重訊號" in out


def test_headline_announcement_only_below_70():
    n = _minimal_notification(ip_heat_percentile=50.0)
    out = format_collab_notification(n)
    assert "🔥" not in out.split("\n")[0]
    assert "公告通知" in out


def test_headline_contains_ip_and_tcg():
    n = _minimal_notification()
    out = format_collab_notification(n)
    assert "Chainsaw Man" in out
    assert "Union Arena" in out


# ── scores ─────────────────────────────────────────────────────────────────────


def test_scores_shown_when_present():
    n = _minimal_notification(long_term_score=95.0, arbitrage_score=88.0)
    out = format_collab_notification(n)
    assert "95" in out
    assert "88" in out


def test_scores_absent_when_none():
    n = _minimal_notification()
    out = format_collab_notification(n)
    assert "📈 長期" not in out
    assert "⚡ 立即" not in out


# ── segment 1: ex-ante signals ─────────────────────────────────────────────────


def test_segment1_product_name_present():
    n = _minimal_notification()
    out = format_collab_notification(n)
    assert "UNION ARENA EX チェンソーマン" in out


def test_segment1_heat_percentile_shown():
    n = _minimal_notification(
        ip_heat_percentile=87.0,
        heat_sources=["x_mention", "reddit"],
    )
    out = format_collab_notification(n)
    assert "percentile=87" in out
    assert "x_mention" in out


def test_segment1_heat_fire_badge_at_80_plus():
    n = _minimal_notification(ip_heat_percentile=82.0)
    out = format_collab_notification(n)
    # 🔥 appears in heat line (not just headline)
    lines_with_heat = [l for l in out.splitlines() if "percentile" in l]
    assert any("🔥" in l for l in lines_with_heat)


def test_segment1_announcement_context_shown():
    n = _minimal_notification(announcement_context="Bushiroad 公式が今日公告")
    out = format_collab_notification(n)
    assert "Bushiroad 公式が今日公告" in out


# ── segment 2: historical inference ───────────────────────────────────────────


def test_segment2_inference_stats_shown():
    n = _minimal_notification(inference=_make_inference(n=7, mean_180=42.0, win_rate=6/7))
    out = format_collab_notification(n)
    assert "📊 歴史推理" in out
    assert "7" in out          # n_samples
    assert "+42.0%" in out


def test_segment2_no_inference_shows_placeholder():
    n = _minimal_notification(inference=None)
    out = format_collab_notification(n)
    assert "歴史推理" in out
    assert "類似事例なし" in out


def test_segment2_zero_samples_shows_placeholder():
    empty_inf = CollabInference(
        query_ip="chainsaw man", query_tcg="union_arena",
        n_samples=0, similar_cases=(),
    )
    n = _minimal_notification(inference=empty_inf)
    out = format_collab_notification(n)
    assert "類似事例なし" in out


def test_segment2_similar_cases_listed():
    n = _minimal_notification(inference=_make_inference(n=3))
    out = format_collab_notification(n)
    assert "demon slayer" in out


def test_segment2_best_worst_shown():
    n = _minimal_notification(inference=_make_inference())
    out = format_collab_notification(n)
    assert "+180%" in out
    assert "-12%" in out


# ── segment 3: store listings ──────────────────────────────────────────────────


def test_segment3_listing_shown():
    listing = StoreListingInfo(
        store_display="Joshin",
        title="UNION ARENA EX チェンソーマン",
        url="https://joshinweb.jp/tcg/chainsaw-ua",
        status_jp="抽選申込受付中",
        price_jpy=4400,
        open_date="2024-06-01",
        deadline="2024-06-07",
    )
    n = _minimal_notification(store_listings=[listing])
    out = format_collab_notification(n)
    assert "Joshin" in out
    assert "joshinweb.jp" in out
    assert "4,400" in out
    assert "2024-06-01" in out
    assert "2024-06-07" in out


def test_segment3_no_listings_shows_placeholder():
    n = _minimal_notification()
    out = format_collab_notification(n)
    assert "まだ公式予約" in out


def test_segment3_multiple_stores():
    listings = [
        StoreListingInfo("Joshin", "Product", "https://joshin.jp"),
        StoreListingInfo("Yodobashi", "Product", "https://yodobashi.com"),
    ]
    n = _minimal_notification(store_listings=listings)
    out = format_collab_notification(n)
    assert "Joshin" in out
    assert "Yodobashi" in out


# ── feedback buttons ───────────────────────────────────────────────────────────


def test_feedback_buttons_always_present():
    n = _minimal_notification()
    out = format_collab_notification(n)
    assert "👍" in out
    assert "👎" in out
    assert "💰" in out


# ── collab_notification_from_dual_signal ───────────────────────────────────────


def test_convenience_constructor():
    notif = collab_notification_from_dual_signal(
        ip_canonical="chainsaw man",
        tcg_game="union_arena",
        product_name="UNION ARENA EX チェンソーマン",
        heat_percentile=87.0,
        heat_sources=["x_mention"],
        inference=None,
        long_term_score=90.0,
    )
    assert notif.ip_canonical == "chainsaw man"
    assert notif.ip_heat_percentile == 87.0
    assert notif.long_term_score == 90.0

    out = format_collab_notification(notif)
    assert "Chainsaw Man" in out
