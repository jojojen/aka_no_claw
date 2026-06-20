"""Tests for the Canonical Market Entity Registry & Alias Graph (issue #12)."""
from __future__ import annotations

import pytest

from openclaw_adapter.market_entity import (
    EntityAlias,
    MarketEntityRegistry,
    build_entity_id,
    normalize_alias_text,
    seed_market_entities,
)


@pytest.fixture()
def registry(tmp_path) -> MarketEntityRegistry:
    return MarketEntityRegistry(tmp_path / "market.sqlite3")


# ── Deliverable 1: model + deterministic ids ─────────────────────────────────
def test_entity_id_deterministic_from_structured_identifiers():
    kw = dict(entity_kind="single_card", franchise="pokemon",
              canonical_title="リザードンex SAR", set_code="sv3",
              card_number="201/108", grade_scope="raw")
    assert build_entity_id(**kw) == build_entity_id(**kw)
    # set_code + card_number drive the id, not the title text.
    assert build_entity_id(**kw) == build_entity_id(
        entity_kind="single_card", franchise="pokemon",
        canonical_title="totally different title", set_code="sv3",
        card_number="201-108", grade_scope="raw",  # punctuation-insensitive
    )


def test_entity_id_distinguishes_raw_and_graded():
    raw = build_entity_id(entity_kind="single_card", franchise="pokemon",
                          set_code="sv3", card_number="201/108", grade_scope="raw")
    graded = build_entity_id(entity_kind="single_card", franchise="pokemon",
                             set_code="sv3", card_number="201/108", grade_scope="graded")
    assert raw != graded
    assert raw.endswith("_raw") and graded.endswith("_graded")


def test_entity_id_falls_back_to_title_without_codes():
    eid = build_entity_id(entity_kind="sealed_box", franchise="pokemon",
                          canonical_title="黒炎の支配者 BOX")
    assert eid.startswith("ent_pokemon_")
    assert "sealedbox" in eid


def test_upsert_and_get_entity_roundtrip(registry):
    rec = registry.upsert_entity(
        entity_kind="single_card", franchise="pokemon",
        canonical_title="リザードンex SAR", set_code="sv3",
        card_number="201/108", grade_scope="raw",
    )
    loaded = registry.get_entity(rec.entity_id)
    assert loaded == rec
    assert loaded.entity_kind == "single_card"
    assert registry.get_entity("ent_does_not_exist") is None


def test_unknown_vocab_snaps_to_default(registry):
    rec = registry.upsert_entity(entity_kind="banana", canonical_title="x",
                                 grade_scope="weird")
    assert rec.entity_kind == "other"
    assert rec.grade_scope == "unknown"


# ── Deliverable 2: alias graph ───────────────────────────────────────────────
def test_aliases_point_to_same_entity_with_confidence_and_source(registry):
    rec = registry.upsert_entity(entity_kind="single_card", franchise="pokemon",
                                 canonical_title="リザードンex SAR")
    assert registry.add_alias(rec.entity_id, "Charizard ex SAR",
                              alias_type="translation", confidence=0.9, source="seed")
    aliases = registry.aliases_of(rec.entity_id)
    texts = {a.alias_text for a in aliases}
    assert "リザードンex SAR" in texts and "Charizard ex SAR" in texts
    charizard = next(a for a in aliases if a.alias_text == "Charizard ex SAR")
    assert charizard.confidence == pytest.approx(0.9)
    assert charizard.source == "seed"


def test_add_alias_to_unknown_entity_fails_safe(registry):
    assert registry.add_alias("ent_nope", "whatever") is False


def test_normalize_alias_text_folds_case_and_punctuation():
    assert normalize_alias_text("  Charizard-ex   SAR!! ") == "charizard ex sar"
    assert normalize_alias_text("リザex／黒炎") == "リザex 黒炎"


# ── Deliverable 3: resolver ──────────────────────────────────────────────────
def test_resolve_exact_alias(registry):
    rec = registry.upsert_entity(entity_kind="single_card", franchise="pokemon",
                                 canonical_title="リザードンex SAR")
    registry.add_alias(rec.entity_id, "Charizard ex SAR", alias_type="translation")
    resolved = registry.resolve_entity("charizard EX sar")  # normalized exact
    assert resolved is not None and resolved.entity_id == rec.entity_id


def test_resolve_structured_code(registry):
    rec = registry.upsert_entity(entity_kind="single_card", franchise="pokemon",
                                 canonical_title="リザードンex SAR", set_code="sv3",
                                 card_number="201/108")
    # a bare card number resolves via the code path
    resolved = registry.resolve_entity("201-108")
    assert resolved is not None and resolved.entity_id == rec.entity_id


def test_resolve_noisy_marketplace_title(registry):
    rec = registry.upsert_entity(entity_kind="single_card", franchise="pokemon",
                                 canonical_title="リザードンex SAR")
    registry.add_alias(rec.entity_id, "charizard ex sar", alias_type="marketplace")
    noisy = "【美品】ポケモンカード Charizard ex SAR 黒炎の支配者 即購入OK 送料無料"
    resolved = registry.resolve_entity(noisy)
    assert resolved is not None and resolved.entity_id == rec.entity_id


def test_ambiguous_query_returns_candidates_not_forced_match(registry):
    a = registry.upsert_entity(entity_kind="single_card", franchise="ws",
                               canonical_title="後藤ひとり SP")
    b = registry.upsert_entity(entity_kind="single_card", franchise="union_arena",
                               canonical_title="ぼっち 後藤ひとり")
    registry.add_alias(a.entity_id, "bocchi", alias_type="sns", confidence=0.6)
    registry.add_alias(b.entity_id, "bocchi", alias_type="sns", confidence=0.6)
    candidates = registry.resolve_entity_candidates("bocchi")
    assert len(candidates) == 2
    assert all(c.ambiguous for c in candidates)
    # resolve_entity refuses to silently pick one
    assert registry.resolve_entity("bocchi") is None


def test_unknown_query_fails_safely(registry):
    registry.upsert_entity(entity_kind="single_card", canonical_title="something")
    assert registry.resolve_entity("totally unrelated zzzzz") is None
    assert registry.resolve_entity_candidates("") == []


def test_resolution_events_logged(registry):
    rec = registry.upsert_entity(entity_kind="single_card", canonical_title="alpha")
    registry.resolve_entity("alpha")
    registry.resolve_entity("nope nope")
    with registry.connect() as conn:
        rows = conn.execute(
            "SELECT outcome, entity_id FROM entity_resolution_events ORDER BY event_id"
        ).fetchall()
    outcomes = [r["outcome"] for r in rows]
    assert "resolved" in outcomes and "unresolved" in outcomes
    resolved_row = next(r for r in rows if r["outcome"] == "resolved")
    assert resolved_row["entity_id"] == rec.entity_id


# ── Deliverable 4: relations ─────────────────────────────────────────────────
def test_parent_child_relations(registry):
    box = registry.upsert_entity(entity_kind="sealed_box", franchise="pokemon",
                                 canonical_title="黒炎 BOX", set_code="sv3",
                                 jan_code="4521329369334")
    the_set = registry.upsert_entity(entity_kind="set", franchise="pokemon",
                                     canonical_title="黒炎の支配者", set_code="sv3b")
    assert registry.add_relation(box.entity_id, the_set.entity_id, relation_type="contains")
    children = registry.children_of(box.entity_id)
    assert len(children) == 1 and children[0].child_entity_id == the_set.entity_id
    assert children[0].relation_type == "contains"
    assert registry.parents_of(the_set.entity_id)[0].parent_entity_id == box.entity_id


def test_relation_to_missing_entity_fails(registry):
    box = registry.upsert_entity(entity_kind="sealed_box", canonical_title="box")
    assert registry.add_relation(box.entity_id, "ent_missing") is False


def test_relations_optional_for_resolution(registry):
    rec = registry.upsert_entity(entity_kind="single_card", canonical_title="lonely card")
    # no relations added; resolution still works
    assert registry.resolve_entity("lonely card").entity_id == rec.entity_id


# ── Deliverable 6: persistence ───────────────────────────────────────────────
def test_persistence_across_instances(tmp_path):
    path = tmp_path / "persist.sqlite3"
    reg1 = MarketEntityRegistry(path)
    rec = reg1.upsert_entity(entity_kind="single_card", canonical_title="persisted")
    reg1.add_alias(rec.entity_id, "persisted nickname", alias_type="sns")

    reg2 = MarketEntityRegistry(path)  # fresh instance, same file
    assert reg2.get_entity(rec.entity_id) is not None
    assert reg2.resolve_entity("persisted nickname").entity_id == rec.entity_id


def test_runtime_alias_added_without_code_change(registry):
    rec = registry.upsert_entity(entity_kind="single_card", canonical_title="base title")
    assert registry.resolve_entity("a brand new nickname") is None
    registry.add_alias(rec.entity_id, "a brand new nickname", alias_type="manual")
    assert registry.resolve_entity("a brand new nickname").entity_id == rec.entity_id


# ── Deliverable 7: seed data ─────────────────────────────────────────────────
def test_seed_resolves_clean_examples(registry):
    ids = seed_market_entities(registry)
    assert len(ids) >= 5
    # clean single card via English translation alias
    assert registry.resolve_entity("Charizard ex SAR") is not None
    # sealed box
    assert registry.resolve_entity("Black Bolt Box JP") is not None
    # structured code on the single card
    assert registry.resolve_entity("201/108") is not None


def test_seed_ambiguous_alias_returns_candidates(registry):
    seed_market_entities(registry)
    candidates = registry.resolve_entity_candidates("bocchi")
    assert len(candidates) >= 2
    assert registry.resolve_entity("bocchi") is None


def test_seed_is_idempotent(registry):
    seed_market_entities(registry)
    seed_market_entities(registry)
    with registry.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM market_entities").fetchone()["c"]
    # second seed upserts rather than duplicating
    assert n == len(set(seed_market_entities(registry)))


def test_describe_entity_for_dashboard(registry):
    ids = seed_market_entities(registry)
    desc = registry.describe_entity(ids[0])  # the Pokémon single card (has aliases)
    assert desc is not None
    assert desc["canonical_title"]
    assert isinstance(desc["aliases"], list) and len(desc["aliases"]) >= 1
    assert registry.describe_entity("ent_missing") is None
