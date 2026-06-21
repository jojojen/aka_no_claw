"""Issue #12 review fix — entity_id is carried + persisted end to end.

entity_id is the canonical Market Entity join key (#12→#13→#14…). The review
asked that (a) OpportunityCandidate round-trips entity_id through its store, and
(b) CollectibleSignal carries entity_id as a first-class field, is forwarded by
candidate_to_signal(), and round-trips through CollectibleSignalStore. Legacy
DBs created before the column existed must migrate idempotently.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from openclaw_adapter.collectible_signal import candidate_to_signal, make_signal
from openclaw_adapter.collectible_signal_store import CollectibleSignalStore
from openclaw_adapter.opportunity_models import OpportunityCandidate
from openclaw_adapter.opportunity_store import OpportunityStore


def _candidate(**kw) -> OpportunityCandidate:
    base = dict(
        candidate_id="opp_e1",
        game="union_arena",
        product_type="sealed_box",
        title="UNION ARENA 鬼滅の刃 BOX",
        search_query="union arena 鬼滅",
        heat_score=75.0,
        reason="store preorder",
        source_kind="official_store_preorder",
        source_url="https://store.example/x",
        metadata={"ip_canonical": "鬼滅の刃", "official_price_jpy": 4400,
                  "source_confidence": 0.9, "product_code": "UA-KMT-01"},
    )
    base.update(kw)
    return OpportunityCandidate(**base)


# --- OpportunityStore round-trips entity_id -----------------------------------

def test_candidate_round_trips_entity_id(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    store.upsert_candidate(_candidate(entity_id="ent_kimetsu_box"))

    fetched = store.get_candidate("opp_e1")
    assert fetched is not None
    assert fetched.entity_id == "ent_kimetsu_box"


def test_candidate_entity_id_not_blanked_by_sparse_echo(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    store.upsert_candidate(_candidate(entity_id="ent_kimetsu_box"))
    # A later observation that doesn't resolve the entity must not wipe it.
    store.upsert_candidate(_candidate(entity_id=None, heat_score=80.0))

    fetched = store.get_candidate("opp_e1")
    assert fetched is not None
    assert fetched.entity_id == "ent_kimetsu_box"


def test_opportunity_store_migrates_legacy_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE opportunity_candidates (candidate_id TEXT PRIMARY KEY)"
        )
        conn.commit()
    OpportunityStore(db_path).bootstrap()
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunity_candidates)")}
    assert "entity_id" in cols


# --- CollectibleSignal carries + persists entity_id ---------------------------

def test_make_signal_carries_entity_id() -> None:
    sig = make_signal(
        source_kind="official_store", collectible_domain="tcg",
        ip_canonical="鬼滅の刃", title="BOX", product_type="sealed_box",
        entity_id="ent_kimetsu_box",
    )
    assert sig.entity_id == "ent_kimetsu_box"


def test_candidate_to_signal_forwards_entity_id() -> None:
    sig = candidate_to_signal(_candidate(entity_id="ent_kimetsu_box"))
    assert sig.entity_id == "ent_kimetsu_box"


def test_signal_round_trips_entity_id(tmp_path: Path) -> None:
    store = CollectibleSignalStore(tmp_path / "sig.db")
    store.bootstrap()
    sig = candidate_to_signal(_candidate(entity_id="ent_kimetsu_box"))
    store.upsert_signal(sig)

    fetched = store.get_signal(sig.signal_id)
    assert fetched is not None
    assert fetched.entity_id == "ent_kimetsu_box"


def test_signal_entity_id_not_blanked_by_sparse_echo(tmp_path: Path) -> None:
    store = CollectibleSignalStore(tmp_path / "sig.db")
    store.bootstrap()
    resolved = make_signal(
        source_kind="official_store", collectible_domain="tcg",
        ip_canonical="鬼滅の刃", title="BOX", product_type="sealed_box",
        official_code="UA-KMT-01", entity_id="ent_kimetsu_box",
    )
    store.upsert_signal(resolved)
    # Same derived signal_id, but this echo hasn't resolved the entity.
    echo = make_signal(
        source_kind="official_store", collectible_domain="tcg",
        ip_canonical="鬼滅の刃", title="BOX", product_type="sealed_box",
        official_code="UA-KMT-01", entity_id=None, heat_score=0.9,
    )
    assert echo.signal_id == resolved.signal_id
    store.upsert_signal(echo)

    fetched = store.get_signal(resolved.signal_id)
    assert fetched is not None
    assert fetched.entity_id == "ent_kimetsu_box"


def test_signal_store_migrates_legacy_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_sig.db"
    # Pre-entity_id schema (the columns the indexes reference must exist).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE collectible_signals (
                signal_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                collectible_domain TEXT NOT NULL,
                ip_canonical TEXT NOT NULL,
                actionability TEXT NOT NULL DEFAULT 'informational',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    CollectibleSignalStore(db_path).bootstrap()
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collectible_signals)")}
    assert "entity_id" in cols
