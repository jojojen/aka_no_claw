"""Tests for 🎯 Target machinery: schema, scoring, pin/unpin commands,
and the Mercari watchlist bridge."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from market_monitor.storage import MercariWatch, MonitorDatabase
from openclaw_adapter.opportunity_agent import (
    MercariWatchlistCandidateProvider,
    UserTargetCandidateProvider,
    _normalize_target_query,
    pin_opportunity_target,
    unpin_opportunity_target,
)
from openclaw_adapter.opportunity_models import OpportunityCandidate
from openclaw_adapter.opportunity_scoring import (
    OpportunityThresholds,
    evaluate_opportunity,
)
from openclaw_adapter.opportunity_models import (
    ListingOffer,
    PriceCheck,
    ReputationCheck,
)
from openclaw_adapter.opportunity_store import OpportunityStore


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _make_store(tmp_path: Path) -> OpportunityStore:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    return store


def _make_candidate(
    *,
    candidate_id: str = "opp_test_001",
    title: str = "アビスアイ box",
    heat: float = 50.0,
    is_target: bool = False,
    source_kind: str = "sns",
) -> OpportunityCandidate:
    return OpportunityCandidate(
        candidate_id=candidate_id,
        game="pokemon",
        product_type="sealed_box",
        title=title,
        search_query=title,
        heat_score=heat,
        reason="test",
        source_kind=source_kind,
        is_target=is_target,
    )


def _make_settings(tmp_path: Path) -> Any:
    """A duck-typed AssistantSettings just exposing the attrs pin/unpin touch."""

    @dataclass
    class _Settings:
        opportunity_db_path: str

    return _Settings(str(tmp_path / "opp.sqlite3"))


# ─── _normalize_target_query (rule-based fallback) ────────────────────────────


def test_normalize_target_query_infers_pokemon_sealed_box() -> None:
    result = _normalize_target_query("ポケモン アビスアイ 1BOX 未開封")
    assert result["game"] == "pokemon"
    assert result["product_type"] == "sealed_box"
    assert result["title"] == "ポケモン アビスアイ 1BOX 未開封"


def test_normalize_target_query_infers_yugioh_single_card() -> None:
    result = _normalize_target_query("遊戯王 青眼の白龍 SR 234/193")
    assert result["game"] == "yugioh"
    assert result["product_type"] == "single_card"


def test_normalize_target_query_falls_back_to_pokemon_other() -> None:
    result = _normalize_target_query("アビスアイ")  # no signals
    assert result["game"] == "pokemon"
    assert result["product_type"] == "other"


def test_normalize_target_query_handles_empty_query() -> None:
    result = _normalize_target_query("")
    assert result["title"] == ""
    assert result["game"] == "pokemon"
    assert result["product_type"] == "other"


def test_normalize_target_query_skips_llm_when_rule_confident() -> None:
    call_count = {"n": 0}

    def fake_llm(prompt: str) -> str:
        call_count["n"] += 1
        return "{}"

    _normalize_target_query("ポケモン アビスアイ 1BOX 未開封", llm_fn=fake_llm)
    assert call_count["n"] == 0  # rule-confident → LLM skipped


# ─── Schema migration + is_target persistence ─────────────────────────────────


def test_schema_includes_is_target_column(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunity_candidates)")}
    assert "is_target" in cols


def test_upsert_persists_is_target(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(is_target=True))
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT is_target FROM opportunity_candidates WHERE candidate_id = ?",
            ("opp_test_001",),
        ).fetchone()
    assert row["is_target"] == 1


def test_upsert_preserves_is_target_on_max(tmp_path: Path) -> None:
    """SNS re-discovering an existing 🎯 Target must not clear the flag."""
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(is_target=True))
    # Now SNS sends the same candidate without is_target set
    store.upsert_candidate(_make_candidate(is_target=False))
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT is_target FROM opportunity_candidates WHERE candidate_id = ?",
            ("opp_test_001",),
        ).fetchone()
    assert row["is_target"] == 1


def test_list_target_candidates_returns_only_targets(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(candidate_id="a", is_target=True))
    store.upsert_candidate(_make_candidate(candidate_id="b", is_target=False))
    targets = store.list_target_candidates(limit=10)
    assert [t.candidate_id for t in targets] == ["a"]


def test_set_is_target_flips_flag(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(is_target=False))
    assert store.set_is_target("opp_test_001", True) is True
    assert store.has_any_target() is True
    assert store.set_is_target("opp_test_001", False) is True
    assert store.has_any_target() is False


def test_has_any_target_false_when_only_dismissed(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(is_target=True))
    store.dismiss_candidate("opp_test_001")
    assert store.has_any_target() is False


# ─── /hunt pin & /hunt unpin ──────────────────────────────────────────────────


def test_pin_flips_existing_candidate(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(is_target=False))
    reply = pin_opportunity_target(settings, "アビスアイ box")
    assert "已加入目標清單" in reply
    assert store.has_any_target() is True


def test_pin_creates_new_candidate_when_no_match(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = _make_store(tmp_path)  # ensure path exists
    reply = pin_opportunity_target(settings, "ポケモン アビスアイ 1BOX")
    assert "已加入目標清單" in reply
    assert store.has_any_target() is True
    # Newly-created candidate should be tagged source_kind=user_pin
    targets = store.list_target_candidates(limit=10)
    assert len(targets) == 1
    assert targets[0].source_kind == "user_pin"
    assert targets[0].is_target is True


def test_pin_rejects_empty_name(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    _make_store(tmp_path)
    reply = pin_opportunity_target(settings, "")
    assert "請提供" in reply


def test_unpin_preserves_candidate_active_status(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(is_target=True))
    reply = unpin_opportunity_target(settings, "アビスアイ")
    assert "已從目標清單移除" in reply
    assert store.has_any_target() is False
    # Candidate must remain active
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, is_target FROM opportunity_candidates WHERE candidate_id = ?",
            ("opp_test_001",),
        ).fetchone()
    assert row["status"] == "active"
    assert row["is_target"] == 0


# ─── evaluate_opportunity dual-threshold ──────────────────────────────────────


def _good_listing_with_discount(discount_pct: float, fair_value: int = 10000) -> tuple[
    PriceCheck, ListingOffer, ReputationCheck
]:
    price = PriceCheck(
        candidate_id="c", fair_value_jpy=fair_value, confidence=0.80, sample_count=5
    )
    listing_price = int(fair_value * (1 - discount_pct / 100))
    listing = ListingOffer(
        listing_id="L", title="t", price_jpy=listing_price, url="https://x/y"
    )
    reputation = ReputationCheck(
        listing_url="https://x/y", trusted=True, proof_url="p",
        total_reviews=50, positive_rate=99.0, grade="A", status="ok", reason="ok",
    )
    return price, listing, reputation


def test_target_candidate_passes_lenient_threshold() -> None:
    candidate = _make_candidate(is_target=True, heat=0.0)
    price, listing, reputation = _good_listing_with_discount(8.0)
    thresholds = OpportunityThresholds()
    decision = evaluate_opportunity(
        candidate=candidate, price=price, listing=listing,
        reputation=reputation, thresholds=thresholds,
    )
    assert decision.accepted is True


def test_non_target_candidate_rejects_small_discount() -> None:
    candidate = _make_candidate(is_target=False, heat=80.0)  # heat passes strict
    price, listing, reputation = _good_listing_with_discount(8.0)
    thresholds = OpportunityThresholds()
    decision = evaluate_opportunity(
        candidate=candidate, price=price, listing=listing,
        reputation=reputation, thresholds=thresholds,
    )
    # 8% discount < 15% strict threshold → reject
    assert decision.accepted is False


def test_auto_discovered_threshold_tightens_when_target_active() -> None:
    candidate = _make_candidate(is_target=False, heat=75.0)
    price, listing, reputation = _good_listing_with_discount(20.0)
    thresholds = OpportunityThresholds()
    # Without target: heat 75 >= 70 → pass
    decision_relaxed = evaluate_opportunity(
        candidate=candidate, price=price, listing=listing,
        reputation=reputation, thresholds=thresholds, has_any_target=False,
    )
    assert decision_relaxed.accepted is True
    # With target: heat threshold rises to 85 → reject
    decision_strict = evaluate_opportunity(
        candidate=candidate, price=price, listing=listing,
        reputation=reputation, thresholds=thresholds, has_any_target=True,
    )
    assert decision_strict.accepted is False


# ─── UserTargetCandidateProvider ──────────────────────────────────────────────


def test_user_target_provider_yields_targets(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_candidate(_make_candidate(candidate_id="a", is_target=True))
    store.upsert_candidate(_make_candidate(candidate_id="b", is_target=False))
    provider = UserTargetCandidateProvider(store=store)
    discovered = list(provider.discover(limit=10))
    assert [c.candidate_id for c in discovered] == ["a"]
    assert discovered[0].is_target is True


def test_user_target_provider_returns_empty_on_storage_error(tmp_path: Path) -> None:
    class BoomStore:
        def list_target_candidates(self, *, limit: int):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

    provider = UserTargetCandidateProvider(store=BoomStore())  # type: ignore[arg-type]
    assert provider.discover(limit=3) == ()


# ─── MercariWatchlistCandidateProvider ────────────────────────────────────────


def _seed_mercari_watch(
    db: MonitorDatabase,
    *,
    watch_id: str,
    query: str,
    threshold: int = 8000,
    enabled: bool = True,
) -> None:
    watch = MercariWatch(
        watch_id=watch_id,
        query=query,
        price_threshold_jpy=threshold,
        enabled=enabled,
        chat_id="123",
        last_checked_at=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    db.add_mercari_watch(watch)


def test_mercari_watchlist_provider_emits_is_target_candidate(tmp_path: Path) -> None:
    db = MonitorDatabase(tmp_path / "monitor.sqlite3")
    db.bootstrap()
    _seed_mercari_watch(db, watch_id="w-001", query="ポケモン アビスアイ 1BOX")
    provider = MercariWatchlistCandidateProvider(market_db=db)
    candidates = list(provider.discover(limit=10))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.is_target is True
    assert c.source_kind == "mercari_watchlist"
    assert c.candidate_id.startswith("opp_mw_")
    assert c.metadata["mercari_watch_id"] == "w-001"
    assert c.metadata["price_threshold_jpy"] == 8000
    # Rule-based normalization should classify this as pokemon sealed_box
    assert c.game == "pokemon"
    assert c.product_type == "sealed_box"


def test_mercari_watchlist_disabled_watches_are_skipped(tmp_path: Path) -> None:
    db = MonitorDatabase(tmp_path / "monitor.sqlite3")
    db.bootstrap()
    _seed_mercari_watch(db, watch_id="w-on", query="enabled", enabled=True)
    _seed_mercari_watch(db, watch_id="w-off", query="disabled", enabled=False)
    provider = MercariWatchlistCandidateProvider(market_db=db)
    candidates = list(provider.discover(limit=10))
    assert len(candidates) == 1
    assert candidates[0].metadata["mercari_watch_id"] == "w-on"


def test_mercari_watchlist_candidate_id_stable_across_ticks(tmp_path: Path) -> None:
    db = MonitorDatabase(tmp_path / "monitor.sqlite3")
    db.bootstrap()
    _seed_mercari_watch(db, watch_id="w-stable", query="ピカチュウ ex SAR")
    provider = MercariWatchlistCandidateProvider(market_db=db)
    first = list(provider.discover(limit=10))
    second = list(provider.discover(limit=10))
    assert first[0].candidate_id == second[0].candidate_id


def test_mercari_watchlist_llm_normalization_cached(tmp_path: Path) -> None:
    db = MonitorDatabase(tmp_path / "monitor.sqlite3")
    db.bootstrap()
    _seed_mercari_watch(db, watch_id="w-cache", query="アビスアイ")  # ambiguous → would call LLM
    call_count = {"n": 0}

    def counting_normalize(query: str, *, llm_fn=None) -> dict[str, str]:
        call_count["n"] += 1
        return {
            "game": "pokemon",
            "product_type": "other",
            "title": query,
            "search_query": query,
        }

    provider = MercariWatchlistCandidateProvider(
        market_db=db, normalize_fn=counting_normalize,
    )
    list(provider.discover(limit=5))
    list(provider.discover(limit=5))
    assert call_count["n"] == 1  # only once across two ticks
