"""Tests for CollabSimilarityProvider wired into OfficialStoreCandidateProvider."""

from __future__ import annotations

import json

import pytest

from openclaw_adapter.collab_outcomes_store import CollabOutcome, CollabOutcomesStore, make_case_id
from openclaw_adapter.collab_similarity_provider import CollabSimilarityProvider
from openclaw_adapter.official_store_provider import (
    _infer_ip_canonical,
    _listing_to_candidate,
    OfficialStoreCandidateProvider,
)
from market_monitor.official_store_base import (
    LOTTERY_OPEN,
    OfficialStoreListing,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _listing(**kw) -> OfficialStoreListing:
    defaults = dict(
        store_name="joshin",
        item_key="joshinweb.jp/tcg/ws-dsm",
        title="ヴァイスシュヴァルツ 鬼滅の刃 ブースターパック",
        url="https://joshinweb.jp/tcg/ws-dsm",
        status=LOTTERY_OPEN,
        price_jpy=4180,
        deadline_iso="2026-07-01T23:59:00+09:00",
        open_date_iso="2026-06-10T10:00:00+09:00",
        categories=("tcg", "weiss_schwarz"),
    )
    defaults.update(kw)
    return OfficialStoreListing(**defaults)


def _outcome(
    ip: str,
    tcg: str,
    announce: str,
    *,
    p30: float = 50.0,
    p180: float = 80.0,
    conf: float = 0.8,
) -> CollabOutcome:
    return CollabOutcome(
        case_id=make_case_id(ip, tcg, announce),
        ip_canonical=ip,
        tcg_game=tcg,
        product_name=f"{ip} × {tcg}",
        announce_date=announce,
        lottery_open_date=None,
        release_date=None,
        lottery_price_jpy=4400.0,
        secondary_30d_ratio=1.0 + p30 / 100,
        secondary_180d_ratio=1.0 + p180 / 100,
        profit_pct_30d=p30,
        profit_pct_180d=p180,
        ip_heat_at_announce=75.0,
        confidence=conf,
        source_urls=[],
        notes=None,
    )


@pytest.fixture
def store_with_outcomes(tmp_path):
    store = CollabOutcomesStore(tmp_path / "collab.sqlite3")
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2021-05-01", p180=180.0))
    store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", "2022-03-01", p180=60.0))
    return store


@pytest.fixture
def empty_store(tmp_path):
    return CollabOutcomesStore(tmp_path / "collab_empty.sqlite3")


# ── _listing_to_candidate with collab_similarity ──────────────────────────────

def test_listing_to_candidate_attaches_inference_when_store_present(store_with_outcomes):
    """Metadata should contain collab_inference_json when similar cases exist."""
    similarity = CollabSimilarityProvider(store_with_outcomes, top_n=5)
    listing = _listing(
        title="ヴァイスシュヴァルツ 鬼滅の刃 ブースターパック",
        categories=("tcg", "weiss_schwarz"),
    )
    candidate = _listing_to_candidate(listing, collab_similarity=similarity)
    assert candidate is not None
    assert "collab_inference_json" in candidate.metadata

    ci = json.loads(str(candidate.metadata["collab_inference_json"]))
    assert ci["n_samples"] > 0
    assert ci["mean_profit_pct_180d"] is not None
    assert ci["win_rate_180d"] is not None
    assert ci["best_profit_pct_180d"] is not None
    assert ci["worst_profit_pct_180d"] is not None
    assert isinstance(ci["top_cases"], list)


def test_listing_to_candidate_no_inference_when_no_similar_cases(empty_store):
    """Metadata should NOT contain collab_inference_json when store is empty."""
    similarity = CollabSimilarityProvider(empty_store, top_n=5)
    listing = _listing(
        title="ヴァイスシュヴァルツ 鬼滅の刃 ブースターパック",
        categories=("tcg", "weiss_schwarz"),
    )
    candidate = _listing_to_candidate(listing, collab_similarity=similarity)
    assert candidate is not None
    assert "collab_inference_json" not in candidate.metadata


def test_listing_to_candidate_no_inference_when_no_provider():
    """Without collab_similarity, metadata should not contain collab_inference_json."""
    listing = _listing()
    candidate = _listing_to_candidate(listing, collab_similarity=None)
    assert candidate is not None
    assert "collab_inference_json" not in candidate.metadata


# ── _format_official_store_recommendation with collab block ───────────────────

def _make_recommendation_with_collab(collab_json: dict | None):
    """Build a minimal OpportunityRecommendation-like object for format testing."""
    from openclaw_adapter.opportunity_models import (
        OpportunityCandidate,
        build_candidate_id,
        utc_now_iso,
    )

    metadata: dict = {
        "source_store": "joshin",
        "listing_status": "lottery_open",
        "listing_url": "https://joshinweb.jp/tcg/ws-dsm",
        "official_price_jpy": 4180,
        "deadline_iso": "2026-07-01T23:59:00+09:00",
    }
    if collab_json is not None:
        metadata["collab_inference_json"] = json.dumps(collab_json, ensure_ascii=False)

    candidate = OpportunityCandidate(
        candidate_id="test-candidate-id",
        game="weiss_schwarz",
        product_type="booster_pack",
        title="ヴァイスシュヴァルツ 鬼滅の刃 ブースターパック",
        search_query="ヴァイスシュヴァルツ 鬼滅の刃",
        heat_score=0.85,
        reason="joshinにlottery_open",
        source_kind="official_store_preorder",
        source_url="https://joshinweb.jp/tcg/ws-dsm",
        metadata=metadata,
        created_at=utc_now_iso(),
    )

    # Build a minimal duck-typed recommendation object
    class _FakeRecommendation:
        pass

    rec = _FakeRecommendation()
    rec.candidate = candidate
    return rec


def test_format_recommendation_includes_collab_block():
    """Notification should include 📊 segment when collab_inference_json present."""
    from openclaw_adapter.opportunity_agent import _format_official_store_recommendation

    collab_json = {
        "n_samples": 7,
        "mean_profit_pct_180d": 42.0,
        "win_rate_180d": 0.857,
        "best_profit_pct_180d": 180.0,
        "worst_profit_pct_180d": -12.0,
        "top_cases": [
            {"ip": "demon slayer", "tcg": "weiss_schwarz", "date": "2021-05", "p180": 180.0},
            {"ip": "jujutsu kaisen", "tcg": "weiss_schwarz", "date": "2022-03", "p180": 60.0},
        ],
    }
    rec = _make_recommendation_with_collab(collab_json)
    output = _format_official_store_recommendation(rec)

    assert "📊" in output
    assert "歴史推理" in output
    assert "7 件" in output
    assert "+42.0%" in output
    assert "6/7" in output  # n_win = round(0.857 * 7) = 6
    assert "+180%" in output
    assert "-12%" in output
    assert "demon slayer" in output
    assert "2021-05" in output


def test_format_recommendation_no_collab_block_when_absent():
    """Notification should NOT include 📊 segment when collab_inference_json absent."""
    from openclaw_adapter.opportunity_agent import _format_official_store_recommendation

    rec = _make_recommendation_with_collab(None)
    output = _format_official_store_recommendation(rec)

    assert "📊" not in output
    assert "歴史推理" not in output


# ── _infer_ip_canonical ────────────────────────────────────────────────────────

def test_infer_ip_canonical_strips_weiss_schwarz_prefix():
    result = _infer_ip_canonical("ヴァイスシュヴァルツ 鬼滅の刃 ブースターパック", "weiss_schwarz")
    assert result == "鬼滅の刃"


def test_infer_ip_canonical_strips_union_arena_prefix():
    result = _infer_ip_canonical("UNION ARENA エクストラブースター チェンソーマン", "union_arena")
    assert result == "エクストラブースター チェンソーマン"


def test_infer_ip_canonical_no_known_prefix():
    result = _infer_ip_canonical("チェンソーマン", "weiss_schwarz")
    assert result == "チェンソーマン"


def test_infer_ip_canonical_strips_booster_suffix():
    result = _infer_ip_canonical("ヴァイスシュヴァルツ 鬼滅の刃 ブースターパック", "weiss_schwarz")
    assert "ブースターパック" not in result


def test_infer_ip_canonical_returns_lowercase():
    result = _infer_ip_canonical("ヴァイスシュヴァルツ ChainSaw Man ブースター", "weiss_schwarz")
    assert result == result.lower()
