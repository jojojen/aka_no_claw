"""Tests for CollabOutcomesStore (D1)."""

from __future__ import annotations

import pytest

from openclaw_adapter.collab_outcomes_store import (
    CollabOutcome,
    CollabOutcomesStore,
    make_case_id,
)


@pytest.fixture
def store(tmp_path):
    return CollabOutcomesStore(tmp_path / "collab.sqlite3")


def _make_outcome(**kw) -> CollabOutcome:
    explicit_case_id = kw.pop("case_id", None)
    defaults = dict(
        ip_canonical="chainsaw man",
        tcg_game="union_arena",
        product_name="UNION ARENA EX チェンソーマン",
        announce_date="2024-06-01",
        lottery_open_date="2024-07-01",
        release_date="2024-09-01",
        lottery_price_jpy=4180.0,
        secondary_30d_ratio=1.8,
        secondary_180d_ratio=2.4,
        profit_pct_30d=80.0,
        profit_pct_180d=140.0,
        ip_heat_at_announce=85.0,
        confidence=0.8,
        source_urls=["https://example.com/ua-csm"],
        notes="初回限定 SAR 多め",
    )
    defaults.update(kw)
    case_id = explicit_case_id or make_case_id(
        defaults["ip_canonical"], defaults["tcg_game"], defaults["announce_date"]
    )
    return CollabOutcome(case_id=case_id, **defaults)


# ── make_case_id ──────────────────────────────────────────────────────────


def test_make_case_id_deterministic():
    a = make_case_id("Chainsaw Man", "union_arena", "2024-06-01")
    b = make_case_id("chainsaw man ", "UNION_ARENA", "2024-06-01")
    assert a == b  # normalised


def test_make_case_id_differs_by_date():
    a = make_case_id("chainsaw man", "union_arena", "2024-06-01")
    b = make_case_id("chainsaw man", "union_arena", "2025-01-01")
    assert a != b


# ── bootstrap ─────────────────────────────────────────────────────────────


def test_bootstrap_creates_table(store):
    assert store.count() == 0


# ── upsert + get ──────────────────────────────────────────────────────────


def test_upsert_inserts_and_get_retrieves(store):
    outcome = _make_outcome()
    store.upsert(outcome)
    fetched = store.get(outcome.case_id)
    assert fetched is not None
    assert fetched.ip_canonical == "chainsaw man"
    assert fetched.profit_pct_30d == 80.0
    assert fetched.source_urls == ["https://example.com/ua-csm"]


def test_upsert_normalises_canonical_and_game(store):
    outcome = _make_outcome(ip_canonical=" Demon Slayer ", tcg_game=" Weiss_Schwarz ")
    store.upsert(outcome)
    fetched = store.get(outcome.case_id)
    assert fetched is not None
    assert fetched.ip_canonical == "demon slayer"
    assert fetched.tcg_game == "weiss_schwarz"


def test_upsert_updates_existing(store):
    outcome = _make_outcome()
    store.upsert(outcome)
    updated = _make_outcome(product_name="Updated product", confidence=0.9)
    store.upsert(updated)
    fetched = store.get(outcome.case_id)
    assert fetched is not None
    assert fetched.product_name == "Updated product"
    assert fetched.confidence == 0.9
    assert store.count() == 1  # still just one row


def test_upsert_with_null_optionals(store):
    outcome = _make_outcome(
        lottery_open_date=None,
        release_date=None,
        lottery_price_jpy=None,
        profit_pct_30d=None,
        profit_pct_180d=None,
        notes=None,
    )
    store.upsert(outcome)
    fetched = store.get(outcome.case_id)
    assert fetched is not None
    assert fetched.lottery_open_date is None
    assert fetched.profit_pct_30d is None


# ── backfill_profit ────────────────────────────────────────────────────────


def test_backfill_profit_updates_existing(store):
    outcome = _make_outcome(profit_pct_30d=None, profit_pct_180d=None)
    store.upsert(outcome)
    ok = store.backfill_profit(
        outcome.case_id,
        profit_pct_30d=55.0,
        profit_pct_180d=120.0,
        confidence=0.9,
    )
    assert ok is True
    fetched = store.get(outcome.case_id)
    assert fetched is not None
    assert fetched.profit_pct_30d == 55.0
    assert fetched.profit_pct_180d == 120.0
    assert fetched.confidence == 0.9


def test_backfill_profit_returns_false_when_not_found(store):
    assert store.backfill_profit("nonexistent_id", profit_pct_30d=50.0) is False


# ── delete ─────────────────────────────────────────────────────────────────


def test_delete_removes_row(store):
    outcome = _make_outcome()
    store.upsert(outcome)
    assert store.delete(outcome.case_id) is True
    assert store.get(outcome.case_id) is None


def test_delete_returns_false_when_not_found(store):
    assert store.delete("no_such_id") is False


# ── list_by_ip ─────────────────────────────────────────────────────────────


def test_list_by_ip_returns_matching(store):
    store.upsert(_make_outcome())
    store.upsert(_make_outcome(ip_canonical="demon slayer", tcg_game="weiss_schwarz",
                                announce_date="2023-01-01",
                                case_id=make_case_id("demon slayer", "weiss_schwarz", "2023-01-01")))
    results = store.list_by_ip("chainsaw man")
    assert len(results) == 1
    assert results[0].ip_canonical == "chainsaw man"


def test_list_by_ip_filters_by_confidence(store):
    store.upsert(_make_outcome(confidence=0.3))
    store.upsert(_make_outcome(announce_date="2024-07-01",
                                case_id=make_case_id("chainsaw man", "union_arena", "2024-07-01"),
                                confidence=0.8))
    results = store.list_by_ip("chainsaw man", min_confidence=0.5)
    assert len(results) == 1
    assert results[0].confidence == 0.8


# ── list_by_tcg ────────────────────────────────────────────────────────────


def test_list_by_tcg(store):
    store.upsert(_make_outcome())
    store.upsert(_make_outcome(tcg_game="weiss_schwarz",
                                case_id=make_case_id("chainsaw man", "weiss_schwarz", "2024-06-01")))
    results = store.list_by_tcg("union_arena")
    assert len(results) == 1
    assert results[0].tcg_game == "union_arena"


# ── list_all ───────────────────────────────────────────────────────────────


def test_list_all_returns_all(store):
    for i in range(3):
        store.upsert(_make_outcome(
            announce_date=f"2024-0{i+1}-01",
            case_id=make_case_id("chainsaw man", "union_arena", f"2024-0{i+1}-01"),
        ))
    assert len(store.list_all()) == 3


def test_list_all_has_profit_data_filters(store):
    store.upsert(_make_outcome(profit_pct_180d=100.0))
    store.upsert(_make_outcome(announce_date="2024-02-01",
                                case_id=make_case_id("chainsaw man", "union_arena", "2024-02-01"),
                                profit_pct_180d=None))
    results = store.list_all(has_profit_data=True)
    assert len(results) == 1
    assert results[0].profit_pct_180d == 100.0
