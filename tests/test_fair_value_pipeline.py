"""Issue #15 (reopened) Deliverable 7 — fair value wired into the opportunity
candidate pipeline.

The reopened review required that fair value not stay a standalone calculation:
``attach_fair_value`` must compute an estimate via the #15 engine and stamp it
onto an ``OpportunityCandidate`` (fair value, discount-to-fair-value, liquidity
adjustment, valuation reasons), and those fields must persist + round-trip
through ``OpportunityStore`` (incl. legacy-DB migration + sparse-echo merge).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlite3

from openclaw_adapter.fair_value import FairValueEngine
from openclaw_adapter.liquidity import SoldCompLedger
from openclaw_adapter.opportunity_models import OpportunityCandidate, attach_fair_value
from openclaw_adapter.opportunity_store import OpportunityStore
from openclaw_adapter.price_ledger import PriceLedger


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _candidate(**kw) -> OpportunityCandidate:
    base = dict(
        candidate_id="opp_v1",
        game="union_arena",
        product_type="sealed_box",
        title="UNION ARENA 鬼滅の刃 BOX",
        search_query="union arena 鬼滅",
        heat_score=75.0,
        reason="store preorder",
        source_kind="official_store_preorder",
        source_url="https://store.example/x",
        metadata={},
    )
    base.update(kw)
    return OpportunityCandidate(**base)


def _seed_engine(tmp_path: Path, eid: str, *, fair: int, ask: int) -> FairValueEngine:
    pl = PriceLedger(tmp_path / "p.db")
    scl = SoldCompLedger(tmp_path / "s.db")
    scl.bootstrap()
    for i in range(5):
        scl.record_sold_comp(entity_id=eid, source_id="S-mercari", sold_price=fair,
                             sold_at=_iso(i + 1), currency="JPY")
    pl.record_observation(entity_id=eid, source_id="S-mercari", price_amount=ask,
                          currency="JPY", quote_type="listing")
    pl.record_observation(entity_id=eid, source_id="S-surugaya", price_amount=ask + 100,
                          currency="JPY", quote_type="listing")
    return FairValueEngine(price_ledger=pl, sold_comp_ledger=scl)


# --- attach_fair_value stamps the snapshot onto the candidate ------------------

def test_attach_fair_value_populates_fields(tmp_path: Path) -> None:
    engine = _seed_engine(tmp_path, "ent_kimetsu_box", fair=10000, ask=7000)
    candidate = _candidate(entity_id="ent_kimetsu_box")
    valued = attach_fair_value(candidate, engine)

    # ~10000 sold-comp median, nudged slightly by the liquidity adjustment.
    assert valued.fair_value_jpy is not None and 9500 <= valued.fair_value_jpy <= 10500
    assert valued.fair_value_confidence is not None and valued.fair_value_confidence > 0
    assert valued.discount_to_fair_value is not None and valued.discount_to_fair_value > 0
    assert valued.liquidity_adjustment is not None
    assert valued.valuation_reasons  # explainable


def test_attach_fair_value_without_entity_is_noop(tmp_path: Path) -> None:
    engine = _seed_engine(tmp_path, "ent_kimetsu_box", fair=10000, ask=7000)
    candidate = _candidate(entity_id=None)
    valued = attach_fair_value(candidate, engine)
    assert valued.fair_value_jpy is None
    assert valued.valuation_reasons == ()


def test_attach_fair_value_insufficient_evidence_notes_reason(tmp_path: Path) -> None:
    pl = PriceLedger(tmp_path / "p.db")
    scl = SoldCompLedger(tmp_path / "s.db")
    scl.bootstrap()
    engine = FairValueEngine(price_ledger=pl, sold_comp_ledger=scl)
    valued = attach_fair_value(_candidate(entity_id="ent_unknown"), engine)
    assert valued.fair_value_jpy is None
    assert valued.valuation_reasons  # degrades to a reason, not a fabricated number


# --- persistence round-trip ---------------------------------------------------

def test_valuation_round_trips_through_store(tmp_path: Path) -> None:
    engine = _seed_engine(tmp_path, "ent_kimetsu_box", fair=10000, ask=7000)
    valued = attach_fair_value(_candidate(entity_id="ent_kimetsu_box"), engine)

    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    store.upsert_candidate(valued)

    fetched = store.get_candidate("opp_v1")
    assert fetched is not None
    assert fetched.fair_value_jpy == valued.fair_value_jpy
    assert fetched.discount_to_fair_value == valued.discount_to_fair_value
    assert fetched.fair_value_confidence == valued.fair_value_confidence
    assert fetched.valuation_reasons == valued.valuation_reasons


def test_valuation_not_blanked_by_sparse_echo(tmp_path: Path) -> None:
    engine = _seed_engine(tmp_path, "ent_kimetsu_box", fair=10000, ask=7000)
    valued = attach_fair_value(_candidate(entity_id="ent_kimetsu_box"), engine)

    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    store.upsert_candidate(valued)
    # A later raw extraction with no valuation must not wipe the stored snapshot.
    store.upsert_candidate(_candidate(entity_id="ent_kimetsu_box", heat_score=90.0))

    fetched = store.get_candidate("opp_v1")
    assert fetched is not None
    assert fetched.fair_value_jpy == valued.fair_value_jpy
    assert fetched.valuation_reasons  # reasons preserved too


def test_store_migrates_legacy_db_adds_valuation_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE opportunity_candidates (candidate_id TEXT PRIMARY KEY)")
        conn.commit()
    OpportunityStore(db_path).bootstrap()
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunity_candidates)")}
    for col in (
        "fair_value_jpy", "fair_value_confidence", "discount_to_fair_value",
        "liquidity_adjustment", "valuation_reasons_json",
    ):
        assert col in cols
