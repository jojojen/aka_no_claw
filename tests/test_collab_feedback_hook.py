"""Tests for the 💰 bought feedback → CollabProfitBackfiller hook.

Covers:
- test_bought_feedback_schedules_backfill
- test_bought_feedback_no_backfill_when_no_case_id
- test_bought_feedback_non_collab_source_skips
- test_up_feedback_never_triggers_backfill
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openclaw_adapter.collab_outcomes_store import CollabOutcome, CollabOutcomesStore, make_case_id
from openclaw_adapter.collab_profit_backfiller import CollabProfitBackfiller
from openclaw_adapter.opportunity_feedback import record_opportunity_feedback
from openclaw_adapter.opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
)
from openclaw_adapter.opportunity_store import OpportunityStore


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_settings(tmp_path: Path) -> Any:
    @dataclass
    class _Settings:
        opportunity_db_path: str

    return _Settings(str(tmp_path / "opp.sqlite3"))


def _seed_candidate_and_recommendation(
    store: OpportunityStore,
    *,
    candidate_id: str = "opp_c1",
    rec_id: str = "rec_1",
    listing_url: str = "https://jp.mercari.com/item/m1",
    source_kind: str = "official_store_preorder",
    metadata: dict | None = None,
) -> None:
    candidate = OpportunityCandidate(
        candidate_id=candidate_id,
        game="union_arena",
        product_type="sealed_box",
        title="チェンソーマン × UNION ARENA BOX",
        search_query="チェンソーマン union arena",
        heat_score=85.0,
        reason="lottery_open at animate",
        source_kind=source_kind,
        source_url="https://www.animate-onlineshop.jp/pn/detail.html?id=12345",
        metadata=metadata or {},
    )
    store.upsert_candidate(candidate)
    price = PriceCheck(candidate_id=candidate_id, fair_value_jpy=10000, confidence=0.8)
    listing = ListingOffer(
        listing_id="l1",
        title="チェンソーマン UA BOX",
        price_jpy=4400,
        url=listing_url,
    )
    reputation = ReputationCheck(
        listing_url=listing_url,
        trusted=True,
        proof_url="proof",
        total_reviews=100,
        positive_rate=99.5,
        grade="A",
        status="ok",
        reason="ok",
    )
    rec = OpportunityRecommendation(
        recommendation_id=rec_id,
        candidate=candidate,
        price=price,
        listing=listing,
        reputation=reputation,
        discount_pct=30.0,
        score=90.0,
        reasons=("official_store_preorder",),
    )
    store.record_recommendation(rec, accepted=True)


def _make_collab_outcome(tmp_path: Path, case_id: str) -> CollabOutcomesStore:
    """Create a CollabOutcomesStore with one outcome seeded."""
    collab_store = CollabOutcomesStore(tmp_path / "collab.sqlite3")
    outcome = CollabOutcome(
        case_id=case_id,
        ip_canonical="chainsaw man",
        tcg_game="union_arena",
        product_name="chainsaw man × union_arena",
        announce_date="2024-06-01",
        lottery_open_date=None,
        release_date="2024-09-27",
        lottery_price_jpy=4400.0,
        secondary_30d_ratio=None,
        secondary_180d_ratio=None,
        profit_pct_30d=None,
        profit_pct_180d=None,
        ip_heat_at_announce=80.0,
        confidence=0.7,
        source_urls=[],
        notes=None,
    )
    collab_store.upsert(outcome)
    return collab_store


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_bought_feedback_schedules_backfill(tmp_path: Path) -> None:
    """💰 bought on official_store_preorder with collab_case_id → backfill scheduled."""
    settings = _make_settings(tmp_path)
    opp_store = OpportunityStore(settings.opportunity_db_path)
    opp_store.bootstrap()

    case_id = make_case_id("chainsaw man", "union_arena", "2024-06-01")
    collab_store = _make_collab_outcome(tmp_path, case_id)

    backfiller = CollabProfitBackfiller(
        store=collab_store,
        db_path=tmp_path / "backfill.sqlite3",
        price_fetcher=None,
    )

    metadata = {
        "collab_case_id": case_id,
        "source_store": "animate",
        "listing_status": "lottery_open",
        "listing_url": "https://www.animate-onlineshop.jp/pn/detail.html?id=12345",
        "release_date": "2024-09-27",
    }
    _seed_candidate_and_recommendation(
        opp_store, source_kind="official_store_preorder", metadata=metadata
    )

    result = record_opportunity_feedback(
        recommendation_id="rec_1",
        kind="bought",
        settings=settings,
        collab_backfiller=backfiller,
    )

    assert result["status"] == "ok"
    assert "promoted_to_target" in result["side_effects"]
    assert "collab_backfill_scheduled" in result["side_effects"]
    # Two tasks (30d + 180d) should be queued
    assert backfiller.pending_count() == 2


def test_bought_feedback_no_backfill_when_no_case_id(tmp_path: Path) -> None:
    """💰 bought on official_store_preorder WITHOUT collab_case_id → no backfill."""
    settings = _make_settings(tmp_path)
    opp_store = OpportunityStore(settings.opportunity_db_path)
    opp_store.bootstrap()

    case_id = make_case_id("chainsaw man", "union_arena", "2024-06-01")
    collab_store = _make_collab_outcome(tmp_path, case_id)

    backfiller = CollabProfitBackfiller(
        store=collab_store,
        db_path=tmp_path / "backfill.sqlite3",
        price_fetcher=None,
    )
    # Metadata without collab_case_id
    metadata = {
        "source_store": "animate",
        "listing_status": "lottery_open",
    }
    _seed_candidate_and_recommendation(
        opp_store, source_kind="official_store_preorder", metadata=metadata
    )

    result = record_opportunity_feedback(
        recommendation_id="rec_1",
        kind="bought",
        settings=settings,
        collab_backfiller=backfiller,
    )

    assert result["status"] == "ok"
    assert "collab_backfill_scheduled" not in result["side_effects"]
    assert backfiller.pending_count() == 0


def test_bought_feedback_non_collab_source_skips(tmp_path: Path) -> None:
    """💰 bought on a non-official_store_preorder source → no backfill."""
    settings = _make_settings(tmp_path)
    opp_store = OpportunityStore(settings.opportunity_db_path)
    opp_store.bootstrap()

    case_id = make_case_id("chainsaw man", "union_arena", "2024-06-01")
    collab_store = _make_collab_outcome(tmp_path, case_id)

    backfiller = CollabProfitBackfiller(
        store=collab_store,
        db_path=tmp_path / "backfill.sqlite3",
        price_fetcher=None,
    )
    # Even with collab_case_id, source_kind is NOT official_store_preorder
    metadata = {"collab_case_id": case_id}
    _seed_candidate_and_recommendation(
        opp_store,
        source_kind="mercari_watchlist",  # different source kind
        metadata=metadata,
    )

    result = record_opportunity_feedback(
        recommendation_id="rec_1",
        kind="bought",
        settings=settings,
        collab_backfiller=backfiller,
    )

    assert result["status"] == "ok"
    assert "collab_backfill_scheduled" not in result["side_effects"]
    assert backfiller.pending_count() == 0


def test_up_feedback_never_triggers_backfill(tmp_path: Path) -> None:
    """👍 up feedback NEVER triggers collab backfill, even with collab_case_id."""
    settings = _make_settings(tmp_path)
    opp_store = OpportunityStore(settings.opportunity_db_path)
    opp_store.bootstrap()

    case_id = make_case_id("chainsaw man", "union_arena", "2024-06-01")
    collab_store = _make_collab_outcome(tmp_path, case_id)

    backfiller = CollabProfitBackfiller(
        store=collab_store,
        db_path=tmp_path / "backfill.sqlite3",
        price_fetcher=None,
    )
    metadata = {
        "collab_case_id": case_id,
        "release_date": "2024-09-27",
    }
    _seed_candidate_and_recommendation(
        opp_store, source_kind="official_store_preorder", metadata=metadata
    )

    result = record_opportunity_feedback(
        recommendation_id="rec_1",
        kind="up",
        settings=settings,
        collab_backfiller=backfiller,
    )

    assert result["status"] == "ok"
    assert "collab_backfill_scheduled" not in result["side_effects"]
    assert backfiller.pending_count() == 0
