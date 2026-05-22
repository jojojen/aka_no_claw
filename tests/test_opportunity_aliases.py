"""Tests for the aliases / related_keywords extension on OpportunityCandidate."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from openclaw_adapter.opportunity_agent import (
    MercariOpportunityListingFinder,
    WebOpportunityAssessment,
    WebOpportunityResearcher,
    _build_opportunity_research_query,
    _classify_listing_product_type,
    _listing_matches_candidate_type,
    _parse_candidate_response,
    _parse_web_assessment,
    _resolve_candidate_selector,
    update_opportunity_string_list,
)
from openclaw_adapter.opportunity_agent import SnsPost
from openclaw_adapter.opportunity_models import (
    OpportunityCandidate,
    build_candidate_id,
    merge_string_list,
)
from openclaw_adapter.opportunity_store import OpportunityStore


# ── merge_string_list ────────────────────────────────────────────────────────


def test_merge_string_list_dedupes_case_fold() -> None:
    out = merge_string_list(("Pikachu",), ["pikachu", "PIKACHU", "Mew"])
    assert out == ("Pikachu", "Mew")


def test_merge_string_list_skips_title_self_reference() -> None:
    out = merge_string_list((), ["ピカチュウex SAR", "テラスタル"], skip=("ピカチュウex SAR",))
    assert out == ("テラスタル",)


def test_merge_string_list_caps_at_max_len() -> None:
    out = merge_string_list((), [f"name_{i}" for i in range(20)], max_len=5)
    assert len(out) == 5


def test_merge_string_list_drops_empty_and_non_strings() -> None:
    out = merge_string_list((), ["", "  ", 42, None, "real"])  # type: ignore[list-item]
    assert out == ("real",)


# ── build_candidate_id stability (aliases must NOT affect dedup) ─────────────


def test_candidate_id_unaffected_by_aliases_or_related() -> None:
    base = build_candidate_id(
        game="pokemon", product_type="single_card",
        title="ピカチュウex SAR", search_query="ピカチュウex SAR 234/193",
        product_identifier="234/193",
    )
    # Same inputs to build_candidate_id → same id regardless of dataclass aliases.
    same = build_candidate_id(
        game="pokemon", product_type="single_card",
        title="ピカチュウex SAR", search_query="ピカチュウex SAR 234/193",
        product_identifier="234/193",
    )
    assert base == same


# ── Storage migration + round-trip ───────────────────────────────────────────


def test_bootstrap_adds_alias_columns_to_legacy_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_opp.sqlite3"
    # Create a legacy schema by hand — no alias / related columns.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE opportunity_candidates (
                candidate_id TEXT PRIMARY KEY,
                game TEXT NOT NULL,
                product_type TEXT NOT NULL DEFAULT 'other',
                title TEXT NOT NULL,
                product_identifier TEXT,
                search_query TEXT NOT NULL,
                heat_score REAL NOT NULL,
                reason TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_url TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO opportunity_candidates VALUES (
                'opp_abc', 'pokemon', 'single_card', 'ピカチュウex',
                NULL, 'ピカチュウex', 80, 'test',
                'sns_llm', '', '{}', 'active', NULL,
                '2026-05-19T00:00:00+00:00', '2026-05-19T00:00:00+00:00'
            );
            """
        )
        conn.commit()

    store = OpportunityStore(db_path)
    store.bootstrap()
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunity_candidates)")}
    assert "aliases_json" in cols
    assert "related_keywords_json" in cols


def test_upsert_merges_aliases_not_overwrites(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    first = OpportunityCandidate(
        candidate_id="opp_test1",
        game="pokemon", product_type="single_card",
        title="ピカチュウex SAR", search_query="ピカチュウex SAR",
        heat_score=80.0, reason="r",
        aliases=("Pikachu SAR",),
    )
    store.upsert_candidate(first)
    # Second upsert from a fresh extraction that *doesn't* echo the existing alias.
    second = OpportunityCandidate(
        candidate_id="opp_test1",
        game="pokemon", product_type="single_card",
        title="ピカチュウex SAR", search_query="ピカチュウex SAR",
        heat_score=82.0, reason="r",
        aliases=("テラスタル ピカチュウ",),
    )
    store.upsert_candidate(second)
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT aliases_json FROM opportunity_candidates WHERE candidate_id = ?",
            ("opp_test1",),
        ).fetchone()
    import json
    aliases = json.loads(row["aliases_json"])
    assert "Pikachu SAR" in aliases
    assert "テラスタル ピカチュウ" in aliases


def test_update_candidate_aliases_add_and_remove(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    cand = OpportunityCandidate(
        candidate_id="opp_t",
        game="pokemon", product_type="single_card",
        title="X", search_query="X", heat_score=50.0, reason="r",
    )
    store.upsert_candidate(cand)

    after_add = store.update_candidate_aliases("opp_t", add=("alpha", "beta"))
    assert after_add == ("alpha", "beta")

    after_remove = store.update_candidate_aliases("opp_t", remove=("alpha",))
    assert after_remove == ("beta",)

    # Removing non-existent → idempotent, no error.
    after_noop = store.update_candidate_aliases("opp_t", remove=("not_there",))
    assert after_noop == ("beta",)

    # Unknown candidate → None.
    assert store.update_candidate_aliases("opp_missing", add=("z",)) is None


# ── Parser extracts new fields ──────────────────────────────────────────────


def test_parse_candidate_response_extracts_aliases_and_related() -> None:
    raw = (
        '{"candidates":[{'
        '"game":"pokemon","product_type":"single_card",'
        '"title":"ピカチュウex SAR","search_query":"ピカチュウex SAR 234",'
        '"heat_score":80,"reason":"r",'
        '"aliases":["Pikachu SAR","Terastal Pikachu","ピカチュウex SAR"],'
        '"related_keywords":["MEGAドリームex"]'
        '}]}'
    )
    candidates = _parse_candidate_response(raw, posts=(), limit=5)
    assert len(candidates) == 1
    c = candidates[0]
    # The title itself was in aliases — must be filtered out.
    assert "ピカチュウex SAR" not in c.aliases
    assert "Pikachu SAR" in c.aliases
    assert "Terastal Pikachu" in c.aliases
    assert c.related_keywords == ("MEGAドリームex",)


def test_parse_candidate_response_tolerates_missing_alias_fields() -> None:
    raw = (
        '{"candidates":[{'
        '"game":"pokemon","product_type":"single_card",'
        '"title":"X","search_query":"X","heat_score":10,"reason":"r"'
        '}]}'
    )
    candidates = _parse_candidate_response(raw, posts=(), limit=5)
    assert len(candidates) == 1
    assert candidates[0].aliases == ()
    assert candidates[0].related_keywords == ()


# ── Mercari finder parallel + fallback ──────────────────────────────────────


def _candidate_with_aliases(*aliases: str, search_query: str = "primary") -> OpportunityCandidate:
    return OpportunityCandidate(
        candidate_id="opp_x",
        game="pokemon", product_type="single_card",
        title=search_query, search_query=search_query,
        heat_score=80.0, reason="r",
        aliases=aliases,
    )


def test_mercari_finder_dedupes_parallel_results(monkeypatch) -> None:
    calls: list[str] = []

    def fake_search(query: str, *, price_max: int, max_results: int, condition_ids):
        calls.append(query)
        if query == "primary":
            return [
                {"item_id": "A", "url": "u/A", "price_jpy": 1000, "title": "A", "thumbnail_url": ""},
                {"item_id": "B", "url": "u/B", "price_jpy": 1000, "title": "B", "thumbnail_url": ""},
            ]
        if query == "alias_one":
            return [
                {"item_id": "B", "url": "u/B", "price_jpy": 1000, "title": "B_dup", "thumbnail_url": ""},
                {"item_id": "C", "url": "u/C", "price_jpy": 1000, "title": "C", "thumbnail_url": ""},
            ]
        return []

    monkeypatch.setattr(
        "openclaw_adapter.opportunity_agent.search_mercari", fake_search
    )
    finder = MercariOpportunityListingFinder()
    cand = _candidate_with_aliases("alias_one")
    offers = finder.find(cand, price_max_jpy=10000, limit=10)
    ids = [o.listing_id for o in offers]
    assert ids == ["A", "B", "C"]
    # Both queries ran in parallel — call order is non-deterministic but both are present.
    assert set(calls) == {"primary", "alias_one"}


def test_mercari_finder_falls_back_to_alias_one_plus_when_parallel_empty(monkeypatch) -> None:
    sequence: list[tuple[str, list[dict]]] = [
        ("primary", []),
        ("alias_one", []),
        ("alias_two", [
            {"item_id": "Z", "url": "u/Z", "price_jpy": 999, "title": "Z", "thumbnail_url": ""},
        ]),
    ]
    queue = {q: results for q, results in sequence}

    def fake_search(query: str, *, price_max: int, max_results: int, condition_ids):
        return queue.get(query, [])

    monkeypatch.setattr(
        "openclaw_adapter.opportunity_agent.search_mercari", fake_search
    )
    finder = MercariOpportunityListingFinder()
    cand = _candidate_with_aliases("alias_one", "alias_two")
    offers = finder.find(cand, price_max_jpy=10000, limit=10)
    assert [o.listing_id for o in offers] == ["Z"]


# ── Selector matches against aliases ────────────────────────────────────────


def test_selector_finds_candidate_by_alias(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    cand = OpportunityCandidate(
        candidate_id="opp_a",
        game="pokemon", product_type="single_card",
        title="ピカチュウex SAR", search_query="ピカチュウex SAR",
        heat_score=80, reason="r",
        aliases=("テラスタル ピカチュウ",),
    )
    store.upsert_candidate(cand)
    rows = store.list_recent_candidates(limit=10)
    resolved = _resolve_candidate_selector(rows, "テラスタル ピカチュウ")
    assert not isinstance(resolved, str)
    assert resolved["candidate_id"] == "opp_a"


def test_selector_does_not_match_against_related_keywords(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    cand = OpportunityCandidate(
        candidate_id="opp_b",
        game="pokemon", product_type="single_card",
        title="ピカチュウex", search_query="ピカチュウex",
        heat_score=80, reason="r",
        related_keywords=("MEGAドリームex",),
    )
    store.upsert_candidate(cand)
    rows = store.list_recent_candidates(limit=10)
    resolved = _resolve_candidate_selector(rows, "MEGAドリームex")
    # related_keywords are intentionally NOT in selector match set.
    assert isinstance(resolved, str)  # "找不到..." message


# ── Web research query embeds aliases ───────────────────────────────────────


def test_web_research_query_includes_aliases() -> None:
    cand = _candidate_with_aliases("テラスタル ピカチュウ", "Pikachu SAR")
    query = _build_opportunity_research_query(cand)
    assert "テラスタル ピカチュウ" in query
    assert "Pikachu SAR" in query
    assert " OR " in query


# ── Web enrich merges discovered_aliases ────────────────────────────────────


def test_parse_web_assessment_reads_discovered_lists() -> None:
    fallback = WebOpportunityAssessment(is_relevant=False, demand_score=0, reason="")
    raw = (
        '{"is_relevant":true,"demand_score":75,"reason":"good",'
        '"discovered_aliases":["alpha","beta"],"discovered_related":["gamma"]}'
    )
    result = _parse_web_assessment(raw, fallback=fallback)
    assert result.discovered_aliases == ("alpha", "beta")
    assert result.discovered_related == ("gamma",)


def test_parse_web_assessment_tolerates_missing_discovered_fields() -> None:
    fallback = WebOpportunityAssessment(is_relevant=False, demand_score=0, reason="")
    raw = '{"is_relevant":true,"demand_score":50,"reason":"ok"}'
    result = _parse_web_assessment(raw, fallback=fallback)
    assert result.discovered_aliases == ()
    assert result.discovered_related == ()


# ── update_opportunity_string_list end-to-end via store ─────────────────────


def test_update_opportunity_string_list_adds_alias(tmp_path: Path) -> None:
    # Stub Settings with the only field we need: opportunity_db_path.
    from types import SimpleNamespace

    settings = SimpleNamespace(opportunity_db_path=str(tmp_path / "opp.sqlite3"))
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    cand = OpportunityCandidate(
        candidate_id="opp_e",
        game="pokemon", product_type="single_card",
        title="ピカチュウex", search_query="ピカチュウex",
        heat_score=80, reason="r",
    )
    store.upsert_candidate(cand)

    reply = update_opportunity_string_list(
        settings, "opp_e", kind="aliases", action="add", names=("Pikachu SAR",),
    )
    assert "✓" in reply
    assert "Pikachu SAR" in reply

    # Confirm persisted.
    rows = store.list_recent_candidates(limit=10)
    assert rows[0]["aliases_json"] and "Pikachu SAR" in rows[0]["aliases_json"]


def test_update_opportunity_string_list_unknown_selector(tmp_path: Path) -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(opportunity_db_path=str(tmp_path / "opp.sqlite3"))
    OpportunityStore(settings.opportunity_db_path).bootstrap()
    reply = update_opportunity_string_list(
        settings, "doesnotexist", kind="aliases", action="add", names=("x",),
    )
    # Either empty-list or not-found message — both acceptable, but never crash.
    assert isinstance(reply, str) and reply


# ── _classify_listing_product_type — title heuristic ────────────────────────


@pytest.mark.parametrize("title,expected", [
    # Sealed box signals
    ("ポケモンカード アビスアイ 1BOX 未開封 シュリンク付き", "sealed_box"),
    ("Pokemon Abyss Eye 1 BOX Japanese", "sealed_box"),
    ("アビスアイ ボックス", "sealed_box"),
    # Single card signals
    ("ポケモンカード アビスアイ ピカチュウex SAR 234/193", "single_card"),
    ("グラジオの決戦 SAR アビスアイ", "single_card"),
    ("メガダークライex アビスアイ RR", "single_card"),
    # Booster pack
    ("アビスアイ 5パック", "booster_pack"),
    # Starter deck (deck wins over an incidental 未開封)
    ("ポケモンカード スタートデッキ100 未開封", "starter_deck"),
    # Promo
    ("プロモパック 限定", "promo"),
    # Single-card via card number
    ("アビスアイ ピカチュウex 234/193", "single_card"),
    # Single-card via rarity tag — production-observed forms from the bug
    ("アビスアイ　トサキント　ar", "single_card"),
    ("アビスアイ エネルギーつけかえ SR 汎用", "single_card"),
    # Bare set name — no signal, classifier punts to "other"
    ("アビスアイ", "other"),
])
def test_classify_listing_product_type(title: str, expected: str) -> None:
    assert _classify_listing_product_type(title) == expected


# ── _listing_matches_candidate_type — asymmetric strictness ─────────────────


def test_sealed_box_candidate_rejects_single_card_listing() -> None:
    title = "ポケモンカード アビスアイ ピカチュウex SAR 234/193"
    assert not _listing_matches_candidate_type(title, "sealed_box")


def test_sealed_box_candidate_accepts_box_listing() -> None:
    title = "ポケモンカード アビスアイ 1BOX 未開封"
    assert _listing_matches_candidate_type(title, "sealed_box")


def test_sealed_box_candidate_rejects_ambiguous_listing() -> None:
    # "other"-classified — sealed_box is strict, must see positive box signal
    assert not _listing_matches_candidate_type("アビスアイ", "sealed_box")


def test_single_card_candidate_accepts_other_listings() -> None:
    # single_card is loose — noisy card titles common on Mercari
    assert _listing_matches_candidate_type("アビスアイ", "single_card")
    assert _listing_matches_candidate_type("ピカチュウex 234/193", "single_card")


def test_single_card_candidate_rejects_explicit_box_listing() -> None:
    # never match a box to a single-card candidate
    assert not _listing_matches_candidate_type(
        "ポケモンカード 1BOX 未開封 シュリンク付き", "single_card"
    )


def test_booster_pack_candidate_rejects_box_listing() -> None:
    # Box has stronger signal than digit+パック, so 1BOX takes precedence
    assert not _listing_matches_candidate_type(
        "アビスアイ 1BOX 30パック入", "booster_pack"
    )


def test_starter_deck_candidate_rejects_bare_single_card() -> None:
    # Without any "スタートデッキ" wording, a SAR card is single_card and
    # a starter_deck candidate must reject it.
    assert not _listing_matches_candidate_type(
        "ポケモンカード ピカチュウex SAR 234/193", "starter_deck"
    )


# ── MercariOpportunityListingFinder end-to-end with the filter ──────────────


def test_mercari_finder_skips_single_card_listing_for_sealed_box_candidate(monkeypatch) -> None:
    """The アビスアイ production bug: single SR cards must not be matched
    against a sealed_box candidate just because the set name appears in
    both titles."""

    def fake_search(query, *, price_max, max_results, condition_ids):
        return [
            {"item_id": "A", "url": "u/A", "price_jpy": 4622,
             "title": "ポケモンカード アビスアイ ピカチュウex SAR 234/193",
             "thumbnail_url": ""},
            {"item_id": "B", "url": "u/B", "price_jpy": 8500,
             "title": "アビスアイ 1BOX 未開封 シュリンク付き", "thumbnail_url": ""},
        ]

    monkeypatch.setattr(
        "openclaw_adapter.opportunity_agent.search_mercari", fake_search
    )
    cand = OpportunityCandidate(
        candidate_id="opp_box",
        game="pokemon", product_type="sealed_box",
        title="アビスアイ", search_query="アビスアイ",
        heat_score=95, reason="abyss eye box",
    )
    offers = MercariOpportunityListingFinder().find(cand, price_max_jpy=10000, limit=5)
    assert [o.listing_id for o in offers] == ["B"]


def test_mercari_finder_keeps_card_listings_for_single_card_candidate(monkeypatch) -> None:
    """Single_card candidates must still match noisy card listings — many
    real card listings lack rarity tags in the title."""

    def fake_search(query, *, price_max, max_results, condition_ids):
        return [
            {"item_id": "X", "url": "u/X", "price_jpy": 800,
             "title": "アビスアイ トサキント ar", "thumbnail_url": ""},
            {"item_id": "Y", "url": "u/Y", "price_jpy": 1200,
             "title": "ピカチュウex 234/193 美品", "thumbnail_url": ""},
        ]

    monkeypatch.setattr(
        "openclaw_adapter.opportunity_agent.search_mercari", fake_search
    )
    cand = OpportunityCandidate(
        candidate_id="opp_card",
        game="pokemon", product_type="single_card",
        title="ピカチュウex", search_query="ピカチュウex",
        heat_score=80, reason="r",
    )
    offers = MercariOpportunityListingFinder().find(cand, price_max_jpy=5000, limit=5)
    assert {o.listing_id for o in offers} == {"X", "Y"}
