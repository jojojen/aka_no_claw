"""Tests for B6: 🎫 official store notification formatter + pipeline integration."""

from __future__ import annotations

from openclaw_adapter.opportunity_agent import (
    _format_official_store_recommendation,
    format_opportunity_recommendation,
)
from openclaw_adapter.opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    build_candidate_id,
    build_listing_key,
    utc_now_iso,
)
from openclaw_adapter.opportunity_store import recommendation_id_for


def _make_official_candidate(**kw) -> OpportunityCandidate:
    defaults = dict(
        candidate_id="test_id",
        game="union_arena",
        product_type="sealed_box",
        title="UNION ARENA エクストラブースター チェンソーマン 1BOX",
        search_query="UNION ARENA チェンソーマン",
        heat_score=0.85,
        reason="joshinに抽選申込受付中, 締切 2026-06-15, 定価 ¥4,180",
        source_kind="official_store_preorder",
        source_url="https://joshinweb.jp/tcg/ua-csm",
        metadata={
            "source_store": "joshin",
            "listing_status": "lottery_open",
            "listing_url": "https://joshinweb.jp/tcg/ua-csm",
            "official_price_jpy": 4180,
            "deadline_iso": "2026-06-15T23:59:00+09:00",
            "open_date_iso": "2026-06-01T10:00:00+09:00",
        },
    )
    defaults.update(kw)
    return OpportunityCandidate(**defaults)


def _make_recommendation(candidate: OpportunityCandidate) -> OpportunityRecommendation:
    listing = ListingOffer(
        listing_id=build_listing_key(candidate.source_url),
        title=candidate.title,
        price_jpy=4180,
        url=candidate.source_url,
    )
    price = PriceCheck(
        candidate_id=candidate.candidate_id,
        fair_value_jpy=5434,  # 1.3x retail
        confidence=0.9,
        sample_count=0,
    )
    rep = ReputationCheck(
        listing_url=candidate.source_url,
        trusted=True,
        status="official_store",
        reason="公式店舗",
    )
    return OpportunityRecommendation(
        recommendation_id=recommendation_id_for(listing),
        candidate=candidate,
        price=price,
        listing=listing,
        reputation=rep,
        discount_pct=0.0,
        score=85.0,
        reasons=("official_store_preorder",),
    )


# ── _format_official_store_recommendation ────────────────────────────────────


def test_official_store_format_headline():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "🎫" in text
    assert "チェンソーマン" in text


def test_official_store_format_store_display():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "Joshin" in text


def test_official_store_format_status_japanese():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "抽選申込受付中" in text


def test_official_store_format_price():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "4,180" in text


def test_official_store_format_deadline():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "06-15" in text


def test_official_store_format_open_date():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "06-01" in text


def test_official_store_format_url():
    rec = _make_recommendation(_make_official_candidate())
    text = _format_official_store_recommendation(rec)
    assert "joshinweb.jp" in text


def test_official_store_format_missing_price_skipped():
    cand = _make_official_candidate(metadata={
        "source_store": "animate",
        "listing_status": "preorder_open",
        "listing_url": "https://www.animate-onlineshop.jp/product/001",
    })
    rec = _make_recommendation(cand)
    text = _format_official_store_recommendation(rec)
    assert "🎫" in text
    assert "定価" not in text


def test_official_store_format_yodobashi_display():
    cand = _make_official_candidate(metadata={
        "source_store": "yodobashi",
        "listing_status": "preorder_open",
        "listing_url": "https://yodobashi.com/product/123/",
        "official_price_jpy": 4180,
    })
    rec = _make_recommendation(cand)
    text = _format_official_store_recommendation(rec)
    assert "ヨドバシ" in text


# ── format_opportunity_recommendation dispatch ───────────────────────────────


def test_dispatch_to_official_store_formatter():
    """format_opportunity_recommendation dispatches to 🎫 format for official_store_preorder."""
    rec = _make_recommendation(_make_official_candidate())
    text = format_opportunity_recommendation(rec)
    assert "🎫" in text
    assert "チェンソーマン" in text


def test_dispatch_to_regular_formatter_for_sns():
    """Regular source_kind still gets the standard 🎯/🔍 formatter."""
    cand = _make_official_candidate(source_kind="sns")
    rec = _make_recommendation(cand)
    text = format_opportunity_recommendation(rec)
    assert "🎫" not in text
    assert ("🎯" in text or "🔍" in text)


# ── Pipeline integration: _run_official_store_candidate ──────────────────────


def test_pipeline_notifies_official_store_once(tmp_path):
    """Official store candidates are notified exactly once (dedup via listing_seen)."""
    from unittest.mock import MagicMock
    from openclaw_adapter.opportunity_pipeline import OpportunityPipeline
    from openclaw_adapter.opportunity_store import OpportunityStore
    from openclaw_adapter.opportunity_scoring import OpportunityThresholds

    db_path = tmp_path / "test_opp.sqlite3"
    store = OpportunityStore(db_path)
    store.bootstrap()

    notified: list[str] = []

    class _MockNotifier:
        def notify(self, recommendation):
            notified.append(recommendation.candidate.title)

    mock_price_checker = MagicMock()
    mock_price_checker.check.return_value = None  # no market data → fallback 1.3×
    pipeline = OpportunityPipeline(
        store=store,
        candidate_provider=MagicMock(),
        price_checker=mock_price_checker,
        listing_finder=MagicMock(),
        reputation_checker=MagicMock(),
        notifier=_MockNotifier(),
        thresholds=OpportunityThresholds(),
    )

    cand = _make_official_candidate()
    store.upsert_candidate(cand)

    from openclaw_adapter.opportunity_pipeline import _MutableStats
    stats = _MutableStats()

    # First call: should notify
    pipeline._run_official_store_candidate(cand, stats)
    assert len(notified) == 1
    assert stats.recommendations_sent == 1

    # Second call: listing already seen, should NOT notify again
    pipeline._run_official_store_candidate(cand, stats)
    assert len(notified) == 1  # still 1
