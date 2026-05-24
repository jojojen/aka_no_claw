"""Tests for CollabProfitBackfiller (D4)."""

from __future__ import annotations

from datetime import date

import pytest

from openclaw_adapter.collab_outcomes_store import CollabOutcome, CollabOutcomesStore, make_case_id
from openclaw_adapter.collab_profit_backfiller import CollabProfitBackfiller


@pytest.fixture
def store(tmp_path):
    return CollabOutcomesStore(tmp_path / "collab.sqlite3")


@pytest.fixture
def backfiller(store, tmp_path):
    return CollabProfitBackfiller(
        store,
        db_path=tmp_path / "backfill.sqlite3",
        price_fetcher=None,
    )


def _outcome(ip, tcg, announce, *, release="2024-09-27", price=4400.0) -> CollabOutcome:
    return CollabOutcome(
        case_id=make_case_id(ip, tcg, announce),
        ip_canonical=ip,
        tcg_game=tcg,
        product_name=f"{ip} × {tcg}",
        announce_date=announce,
        lottery_open_date=None,
        release_date=release,
        lottery_price_jpy=price,
        secondary_30d_ratio=None,
        secondary_180d_ratio=None,
        profit_pct_30d=None,
        profit_pct_180d=None,
        ip_heat_at_announce=80.0,
        confidence=0.7,
        source_urls=[],
        notes=None,
    )


# ── record_purchase ────────────────────────────────────────────────────────────


def test_record_purchase_schedules_two_tasks(store, backfiller):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01")
    store.upsert(outcome)
    ok = backfiller.record_purchase(outcome.case_id)
    assert ok is True
    assert backfiller.pending_count() == 2


def test_record_purchase_uses_explicit_release_date(store, tmp_path):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01", release=None)
    store.upsert(outcome)
    bf = CollabProfitBackfiller(store, db_path=tmp_path / "bf2.sqlite3")
    ok = bf.record_purchase(outcome.case_id, release_date="2024-09-27")
    assert ok is True


def test_record_purchase_returns_false_for_unknown_case(store, backfiller):
    assert backfiller.record_purchase("nonexistent_id") is False


def test_record_purchase_returns_false_when_no_release_date(store, backfiller):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01", release=None)
    store.upsert(outcome)
    assert backfiller.record_purchase(outcome.case_id) is False


def test_record_purchase_idempotent(store, backfiller):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01")
    store.upsert(outcome)
    backfiller.record_purchase(outcome.case_id)
    backfiller.record_purchase(outcome.case_id)  # second call
    assert backfiller.pending_count() == 2  # no duplicates


# ── run_pending ────────────────────────────────────────────────────────────────


def test_run_pending_skips_when_no_price_fetcher(store, backfiller):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01")
    store.upsert(outcome)
    backfiller.record_purchase(outcome.case_id)
    results = backfiller.run_pending(as_of=date(2030, 1, 1))
    assert all(not r.ok for r in results)
    assert backfiller.pending_count() == 0  # tasks marked skipped


def test_run_pending_updates_30d_profit(store, tmp_path):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01", release="2024-09-27")
    store.upsert(outcome)

    def fake_fetcher(product_name, lottery_price):
        return 8800.0  # 2× the lottery price → +100%

    bf = CollabProfitBackfiller(
        store, db_path=tmp_path / "bf30.sqlite3", price_fetcher=fake_fetcher
    )
    bf.record_purchase(outcome.case_id)
    # Only 30d task is due (30 days after 2024-09-27 = 2024-10-27)
    results = bf.run_pending(as_of=date(2024, 10, 27))
    assert any(r.window_days == 30 and r.ok for r in results)

    updated = store.get(outcome.case_id)
    assert updated is not None
    assert updated.profit_pct_30d == pytest.approx(100.0)
    assert updated.secondary_30d_ratio == pytest.approx(2.0)


def test_run_pending_updates_180d_profit(store, tmp_path):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01", release="2024-09-27")
    store.upsert(outcome)

    def fake_fetcher(product_name, lottery_price):
        return 6600.0  # 1.5× → +50%

    bf = CollabProfitBackfiller(
        store, db_path=tmp_path / "bf180.sqlite3", price_fetcher=fake_fetcher
    )
    bf.record_purchase(outcome.case_id)
    # 180 days after 2024-09-27 = 2025-03-26
    results = bf.run_pending(as_of=date(2025, 3, 26))
    r180 = next((r for r in results if r.window_days == 180), None)
    assert r180 is not None and r180.ok

    updated = store.get(outcome.case_id)
    assert updated is not None
    assert updated.profit_pct_180d == pytest.approx(50.0)


def test_run_pending_not_due_yet_stays_pending(store, tmp_path):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01", release="2024-09-27")
    store.upsert(outcome)

    def fake_fetcher(pn, lp):
        return 8800.0

    bf = CollabProfitBackfiller(
        store, db_path=tmp_path / "bfnotdue.sqlite3", price_fetcher=fake_fetcher
    )
    bf.record_purchase(outcome.case_id)
    # run as of release date itself — nothing due yet
    results = bf.run_pending(as_of=date(2024, 9, 27))
    assert results == []
    assert bf.pending_count() == 2


def test_run_pending_price_fetcher_exception_marks_skipped(store, tmp_path):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01")
    store.upsert(outcome)

    def bad_fetcher(pn, lp):
        raise RuntimeError("network error")

    bf = CollabProfitBackfiller(
        store, db_path=tmp_path / "bferr.sqlite3", price_fetcher=bad_fetcher
    )
    bf.record_purchase(outcome.case_id)
    results = bf.run_pending(as_of=date(2030, 1, 1))
    assert all(not r.ok for r in results)
    assert bf.pending_count() == 0


def test_run_pending_price_fetcher_returns_none_marks_skipped(store, tmp_path):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01")
    store.upsert(outcome)

    bf = CollabProfitBackfiller(
        store, db_path=tmp_path / "bfnone.sqlite3", price_fetcher=lambda pn, lp: None
    )
    bf.record_purchase(outcome.case_id)
    results = bf.run_pending(as_of=date(2030, 1, 1))
    assert all(not r.ok for r in results)


# ── list_pending ───────────────────────────────────────────────────────────────


def test_list_pending_returns_queued_tasks(store, backfiller):
    outcome = _outcome("chainsaw man", "union_arena", "2024-06-01")
    store.upsert(outcome)
    backfiller.record_purchase(outcome.case_id)
    pending = backfiller.list_pending()
    assert len(pending) == 2
    windows = {p["window_days"] for p in pending}
    assert windows == {30, 180}
