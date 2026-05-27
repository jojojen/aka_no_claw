"""Tests for the 👍/👎/💰 feedback loop on Opportunity recommendations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from openclaw_adapter.opportunity_feedback import (
    DEFAULT_DOWN_DISMISS_THRESHOLD,
    record_opportunity_feedback,
)
from openclaw_adapter.opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
)
from openclaw_adapter.opportunity_store import OpportunityStore


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _make_settings(tmp_path: Path) -> Any:
    @dataclass
    class _Settings:
        opportunity_db_path: str

    return _Settings(str(tmp_path / "opp.sqlite3"))


def _seed_recommendation(
    store: OpportunityStore,
    *,
    candidate_id: str = "opp_c1",
    rec_id: str = "rec_1",
    listing_url: str = "https://jp.mercari.com/item/m1",
) -> None:
    candidate = OpportunityCandidate(
        candidate_id=candidate_id,
        game="pokemon",
        product_type="single_card",
        title="ピカチュウex SAR",
        search_query="ピカチュウex SAR",
        heat_score=85.0,
        reason="t",
    )
    store.upsert_candidate(candidate)
    price = PriceCheck(candidate_id=candidate_id, fair_value_jpy=10000, confidence=0.8)
    listing = ListingOffer(
        listing_id="l1", title="ピカチュウex SAR", price_jpy=7500, url=listing_url
    )
    reputation = ReputationCheck(
        listing_url=listing_url, trusted=True, proof_url="proof",
        total_reviews=50, positive_rate=99.0, grade="A", status="ok", reason="ok",
    )
    rec = OpportunityRecommendation(
        recommendation_id=rec_id,
        candidate=candidate,
        price=price,
        listing=listing,
        reputation=reputation,
        discount_pct=25.0,
        score=88.0,
        reasons=("ok",),
    )
    store.record_recommendation(rec, accepted=True)


def _get_feedback_kind(store: OpportunityStore, rec_id: str) -> str | None:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT feedback_kind FROM opportunity_recommendations WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
    return row["feedback_kind"] if row else None


def _get_cooldown(store: OpportunityStore, candidate_id: str) -> str | None:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT cooldown_until FROM opportunity_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    return row["cooldown_until"] if row else None


def _get_status(store: OpportunityStore, candidate_id: str) -> str | None:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM opportunity_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    return row["status"] if row else None


# ─── Schema migration ─────────────────────────────────────────────────────────


def test_schema_includes_feedback_and_cooldown_columns(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    with sqlite3.connect(store.path) as conn:
        rec_cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunity_recommendations)")}
        cand_cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunity_candidates)")}
    assert "feedback_kind" in rec_cols
    assert "feedback_at" in rec_cols
    assert "cooldown_until" in cand_cols


# ─── 👍 up: promote to Target ─────────────────────────────────────────────────


def test_up_feedback_marks_candidate_is_target(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    _seed_recommendation(store)

    result = record_opportunity_feedback(
        recommendation_id="rec_1", kind="up", settings=settings,
    )
    assert result["status"] == "ok"
    assert "promoted_to_target" in result["side_effects"]
    assert _get_feedback_kind(store, "rec_1") == "up"
    assert store.has_any_target() is True


# ─── 💰 bought: promote + record ──────────────────────────────────────────────


def test_bought_feedback_promotes_to_target(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    _seed_recommendation(store)

    result = record_opportunity_feedback(
        recommendation_id="rec_1", kind="bought", settings=settings,
    )
    assert result["status"] == "ok"
    assert "promoted_to_target" in result["side_effects"]
    assert _get_feedback_kind(store, "rec_1") == "bought"
    assert store.has_any_target() is True


# ─── 👎 down: cooldown then auto-dismiss ──────────────────────────────────────


def test_down_feedback_starts_cooldown(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    _seed_recommendation(store)

    result = record_opportunity_feedback(
        recommendation_id="rec_1", kind="down", settings=settings,
    )
    assert result["status"] == "ok"
    assert "cooldown_started" in result["side_effects"]
    cooldown = _get_cooldown(store, "opp_c1")
    assert cooldown is not None
    # Cooldown should be ~24h in future (allow generous tolerance)
    cooldown_dt = datetime.fromisoformat(cooldown)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert timedelta(hours=23) < cooldown_dt - now < timedelta(hours=25)


def test_three_downs_dismiss_candidate(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    # Seed 3 separate recommendations for same candidate (different listing URLs)
    for i in range(DEFAULT_DOWN_DISMISS_THRESHOLD):
        _seed_recommendation(
            store, rec_id=f"rec_{i}", listing_url=f"https://x.test/item{i}"
        )

    # Reach the dismiss threshold on the third 👎
    for i in range(DEFAULT_DOWN_DISMISS_THRESHOLD - 1):
        record_opportunity_feedback(
            recommendation_id=f"rec_{i}", kind="down", settings=settings,
        )
        assert _get_status(store, "opp_c1") == "active"

    final = record_opportunity_feedback(
        recommendation_id=f"rec_{DEFAULT_DOWN_DISMISS_THRESHOLD - 1}",
        kind="down",
        settings=settings,
    )
    assert "auto_dismissed" in final["side_effects"]
    assert _get_status(store, "opp_c1") == "dismissed"


def test_unknown_kind_rejected(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    _seed_recommendation(store)

    result = record_opportunity_feedback(
        recommendation_id="rec_1", kind="meh", settings=settings,
    )
    assert result["status"] == "rejected"


def test_missing_recommendation_rejected(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()

    result = record_opportunity_feedback(
        recommendation_id="rec_does_not_exist", kind="up", settings=settings,
    )
    assert result["status"] == "rejected"


# ─── cooldown_until gates list_due_candidates ─────────────────────────────────


def test_list_due_candidates_skips_cooled_down(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    candidate = OpportunityCandidate(
        candidate_id="c1", game="pokemon", product_type="single_card",
        title="t", search_query="t", heat_score=99.0, reason="r",
    )
    store.upsert_candidate(candidate)
    # Sanity: not on cooldown → due
    due = store.list_due_candidates(limit=10, min_interval_seconds=0)
    assert [c.candidate_id for c in due] == ["c1"]

    # Cooled-down candidate must be excluded
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    store.set_cooldown("c1", future)
    due = store.list_due_candidates(limit=10, min_interval_seconds=0)
    assert due == []

    # Once cooldown expires (past) → due again
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.set_cooldown("c1", past)
    due = store.list_due_candidates(limit=10, min_interval_seconds=0)
    assert [c.candidate_id for c in due] == ["c1"]
