"""Unit tests for KnowledgeDatabase + format_knowledge_block."""

from __future__ import annotations

import pytest

from openclaw_adapter.knowledge_db import (
    ENTITY_TYPES,
    KnowledgeDatabase,
    KnowledgeEntry,
    ORIGINS,
    format_knowledge_block,
    is_source_id,
)


@pytest.fixture
def db(tmp_path):
    return KnowledgeDatabase(tmp_path / "knowledge.sqlite3")


def test_upsert_and_get_entry_roundtrip(db):
    db.upsert_entry(
        entity_canonical="pjsk",
        entity_type="ip",
        summary="プロセカ。SEGA + Crypton。客群以年輕女性為主。",
        source_urls=("https://example.com/a",),
        confidence=0.7,
        origin="web_research",
        aliases=("PJSK", "プロセカ", "Project Sekai"),
    )
    entry = db.get_entry("pjsk")
    assert entry is not None
    assert entry.entity_canonical == "pjsk"
    assert entry.entity_type == "ip"
    assert entry.confidence == pytest.approx(0.7)
    assert "プロセカ" in entry.summary
    # source URLs are interned through the registry (issue #9 D4): the entry
    # stores a stable S-id, and it resolves back to the canonical URL.
    assert len(entry.source_urls) == 1
    sid = entry.source_urls[0]
    assert is_source_id(sid)
    rec = db.get_source(sid)
    assert rec is not None and rec.canonical_url == "https://example.com/a"


def test_upsert_entry_interns_raw_urls_and_drops_opaque(db):
    """issue #9 D4: every producer is forced through the registry at upsert.

    Raw URLs become S-ids; an already-interned S-id passes through unchanged; an
    opaque redirect (untraceable offline) is dropped rather than stored raw."""
    pre = db.intern_source("https://example.com/already")
    assert pre is not None
    db.upsert_entry(
        entity_canonical="union_arena",
        entity_type="tcg",
        summary="UNION ARENA。",
        source_urls=(
            "https://www.suruga-ya.jp/item/9?utm_source=x",  # raw → interned
            pre,                                              # S-id → kept
            "https://rd.listing.yahoo.co.jp/abc123",         # opaque → dropped
        ),
        confidence=0.7,
        origin="web_research",
    )
    entry = db.get_entry("union_arena")
    assert entry is not None
    assert all(is_source_id(s) for s in entry.source_urls)
    assert pre in entry.source_urls
    resolved = [db.get_source(s).canonical_url for s in entry.source_urls]
    assert "https://www.suruga-ya.jp/item/9" in resolved
    assert not any("yahoo.co.jp" in u for u in resolved)  # opaque refused


def test_upsert_entry_drops_dangling_source_id(db):
    """issue #9: an S-id is only traceable if it resolves to a sources row.

    A formally-shaped but non-existent id (e.g. ``S999``) must be dropped, not
    stored, so the entry never carries an unresolvable citation."""
    assert db.get_source("S999") is None
    db.upsert_entry(
        entity_canonical="union_arena",
        entity_type="tcg",
        summary="UNION ARENA。",
        source_urls=("S999",),
        confidence=0.7,
        origin="web_research",
    )
    entry = db.get_entry("union_arena")
    assert entry is not None
    assert entry.source_urls == ()  # dangling id dropped


def test_get_entry_is_case_insensitive(db):
    db.upsert_entry(entity_canonical="PJSK", entity_type="ip",
                    summary="x", source_urls=(), confidence=0.5, origin="manual", aliases=())
    assert db.get_entry("pjsk") is not None
    assert db.get_entry("PJSK") is not None


def test_alias_lookup_resolves_to_canonical(db):
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="x", source_urls=(), confidence=0.5, origin="manual",
                    aliases=("プロセカ", "PJSK"))
    assert db.lookup_canonical("プロセカ") == "pjsk"
    assert db.lookup_canonical("PJSK") == "pjsk"
    assert db.lookup_canonical("unknown") is None


def test_higher_confidence_overrides_lower(db):
    """Manual / curated knowledge (confidence=1.0) must win over
    web-research auto-backfill (0.5)."""
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="自動爬到的舊資訊", source_urls=(), confidence=0.5,
                    origin="web_research", aliases=())
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="使用者手動補的正確版", source_urls=(), confidence=1.0,
                    origin="manual", aliases=())
    entry = db.get_entry("pjsk")
    assert entry.summary == "使用者手動補的正確版"
    assert entry.origin == "manual"
    assert entry.confidence == pytest.approx(1.0)


def test_lower_confidence_does_not_override_higher(db):
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="高信任版", source_urls=(), confidence=1.0,
                    origin="manual", aliases=())
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="低信任版（不該蓋過）", source_urls=(), confidence=0.3,
                    origin="web_research", aliases=())
    entry = db.get_entry("pjsk")
    assert entry.summary == "高信任版"


def test_all_aliases_includes_canonical(db):
    """The canonical name itself should appear in all_aliases() so substring
    scans catch it even when no manual alias was registered."""
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="x", source_urls=(), confidence=0.5, origin="manual",
                    aliases=("PJSK",))
    aliases = db.all_aliases()
    alias_strs = {a for a, _ in aliases}
    assert "pjsk" in alias_strs or "PJSK" in alias_strs


def test_add_alias_after_creation(db):
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip",
                    summary="x", source_urls=(), confidence=0.5, origin="manual",
                    aliases=())
    assert db.add_alias("Project Sekai", "pjsk") is True
    assert db.lookup_canonical("Project Sekai") == "pjsk"


def test_mark_referenced_updates_timestamp(db):
    db.upsert_entry(entity_canonical="x", entity_type="other",
                    summary="x", source_urls=(), confidence=0.5, origin="manual",
                    aliases=())
    before = db.get_entry("x")
    db.mark_referenced("x")
    after = db.get_entry("x")
    assert after.last_referenced_at is not None
    assert after.last_referenced_at != before.last_referenced_at or before.last_referenced_at is None


def test_recent_entries_respects_limit_and_returns_entries(db):
    """Three insertions may share the same updated_at timestamp (sqlite's
    resolution is seconds), so we assert on count + presence, not order."""
    for name in ("a", "b", "c"):
        db.upsert_entry(entity_canonical=name, entity_type="ip",
                        summary=name, source_urls=(), confidence=0.5,
                        origin="manual", aliases=())
    entries = db.recent_entries(limit=2)
    assert len(entries) == 2
    assert all(isinstance(e.entity_canonical, str) for e in entries)
    all_entries = db.recent_entries(limit=10)
    assert {e.entity_canonical for e in all_entries} == {"a", "b", "c"}


def test_entity_types_constant_matches_schema_intent():
    """If anyone adds a new entity_type they must update the constant too."""
    assert set(ENTITY_TYPES) >= {"ip", "product", "set", "creator", "event", "store", "other"}


def test_research_command_origin_is_whitelisted():
    assert "research_command" in ORIGINS


# ── format_knowledge_block helper ──────────────────────────────────────────


def test_format_knowledge_block_includes_summary_for_each_entry():
    entries = [
        KnowledgeEntry(entry_id="e1", entity_canonical="pjsk", entity_type="ip",
                       summary="プロセカ。節奏音遊。", source_urls=(), confidence=0.7,
                       origin="web_research", created_at="", updated_at=""),
        KnowledgeEntry(entry_id="e2", entity_canonical="アビスアイ", entity_type="set",
                       summary="ポケカ拡張パック。", source_urls=(), confidence=0.6,
                       origin="web_research", created_at="", updated_at=""),
    ]
    block = format_knowledge_block(entries)
    assert "pjsk (ip)" in block
    assert "アビスアイ (set)" in block
    assert "プロセカ" in block


def test_format_knowledge_block_shows_unknown_entities_as_placeholder():
    block = format_knowledge_block([], unknown_entities=("謎の新弾",))
    assert "謎の新弾" in block
    assert "資料庫尚無" in block


def test_format_knowledge_block_empty_returns_placeholder():
    assert format_knowledge_block([]) == "(無)"


def test_format_knowledge_block_truncates_long_summary():
    long_summary = "x" * 5000
    entry = KnowledgeEntry(entry_id="e", entity_canonical="big", entity_type="other",
                           summary=long_summary, source_urls=(), confidence=0.5,
                           origin="manual", created_at="", updated_at="")
    block = format_knowledge_block([entry], max_chars=200)
    # Block headers/labels add some chars, so allow generous overhead.
    assert len(block) < 500
    assert "big (other)" in block


# ── append_observation — silenced-signal sink ───────────────────────────────


from openclaw_adapter.knowledge_db import (
    NO_DATA_SUMMARY,
    OBSERVATION_MARKER,
    OBSERVATION_SUMMARY_CAP,
    is_insufficient_entry,
    is_operational_cache_entry,
)


def _seed_entity(db, *, canonical="union_arena", summary="UNION ARENA。Bandai 旗下 TCG。", origin="manual"):
    db.upsert_entry(
        entity_canonical=canonical, entity_type="tcg",
        summary=summary, source_urls=(), confidence=0.7,
        origin=origin, aliases=("UNION ARENA", "UA"),
    )


def test_append_observation_existing_entity_appends_with_marker(db):
    _seed_entity(db)
    ok = db.append_observation(
        entity_alias_or_canonical="union_arena",
        observed_at="2026-05-27T01:00:00+00:00",
        rationale="新弾発表",
        suggested_action="關注發售資訊",
        tweet_url="https://x.com/UA_EN_TCG/status/1",
        deadline="2026-06-27T00:00:00Z",
    )
    assert ok is True
    entry = db.get_entry("union_arena")
    assert OBSERVATION_MARKER in entry.summary
    assert entry.summary.startswith("UNION ARENA")
    assert "[2026-05-27]" in entry.summary
    assert "新弾発表" in entry.summary
    assert "https://x.com/UA_EN_TCG/status/1" in entry.summary
    assert "[~2026-06-27]" in entry.summary


def test_append_observation_unknown_entity_returns_false_no_write(db):
    ok = db.append_observation(
        entity_alias_or_canonical="totally_unknown_ip",
        observed_at="2026-05-27T01:00:00+00:00",
        rationale="x", suggested_action="y", tweet_url="https://x.com/a/status/1",
    )
    assert ok is False
    assert db.get_entry("totally_unknown_ip") is None


def test_append_observation_via_alias_resolves_to_canonical(db):
    _seed_entity(db)
    ok = db.append_observation(
        entity_alias_or_canonical="UA",  # alias, not canonical
        observed_at="2026-05-27T01:00:00+00:00",
        rationale="alias 解析測試", suggested_action="z",
        tweet_url="https://x.com/a/status/2",
    )
    assert ok is True
    entry = db.get_entry("union_arena")
    assert "alias 解析測試" in entry.summary


def test_append_observation_fifo_drops_oldest_when_over_cap(db):
    head = "UNION ARENA 簡介。" + "x" * (OBSERVATION_SUMMARY_CAP - 200)
    _seed_entity(db, summary=head)
    # Append several bullets — first will be FIFO-dropped to stay under cap.
    for i in range(5):
        db.append_observation(
            entity_alias_or_canonical="union_arena",
            observed_at=f"2026-05-2{i}T01:00:00+00:00",
            rationale=f"觀察 #{i}",
            suggested_action="x" * 80,
            tweet_url=f"https://x.com/a/status/{i}",
        )
    entry = db.get_entry("union_arena")
    assert entry.summary.startswith("UNION ARENA 簡介。"), "head must be preserved"
    assert len(entry.summary) <= OBSERVATION_SUMMARY_CAP
    # Newest bullet must survive; oldest one(s) must have been dropped.
    assert "觀察 #4" in entry.summary
    assert "觀察 #0" not in entry.summary


def test_append_observation_does_not_change_origin(db):
    _seed_entity(db, origin="manual")
    db.append_observation(
        entity_alias_or_canonical="union_arena",
        observed_at="2026-05-27T01:00:00+00:00",
        rationale="x", suggested_action="y", tweet_url="https://x.com/a/status/1",
    )
    assert db.get_entry("union_arena").origin == "manual"


def test_append_observation_updates_timestamps(db):
    _seed_entity(db)
    before = db.get_entry("union_arena")
    db.append_observation(
        entity_alias_or_canonical="union_arena",
        observed_at="2026-05-27T01:00:00+00:00",
        rationale="x", suggested_action="y", tweet_url="https://x.com/a/status/1",
    )
    after = db.get_entry("union_arena")
    assert after.updated_at >= before.updated_at
    assert after.last_referenced_at is not None
    assert after.last_referenced_at >= (before.last_referenced_at or "")


# ── 資料不足 no-data stubs — never store onto / never surface ─────────────────


def _seed_no_data_stub(db, canonical="pokeca_new_card"):
    db.upsert_entry(
        entity_canonical=canonical, entity_type="other",
        summary=NO_DATA_SUMMARY, confidence=0.0, origin="web_research",
    )


def test_is_insufficient_entry_flags_no_data_stub(db):
    _seed_no_data_stub(db)
    assert is_insufficient_entry(db.get_entry("pokeca_new_card")) is True


def test_is_insufficient_entry_passes_real_entry(db):
    _seed_entity(db)
    assert is_insufficient_entry(db.get_entry("union_arena")) is False


def test_is_operational_cache_entry_flags_yuyutei_marker():
    op = KnowledgeEntry(
        entry_id="e", entity_canonical="プロセカ 桐谷遥 SSP", entity_type="tcg",
        summary="yuyutei_code=ws. 遊々亭一致商品「…」. 検索語「…」の遊々亭ゲームコード判定。",
        confidence=0.6,
    )
    assert is_operational_cache_entry(op) is True


def test_is_operational_cache_entry_flags_mercari_item_cache():
    op = KnowledgeEntry(
        entry_id="e",
        entity_canonical="mercari:m93045899435",
        entity_type="product",
        summary=(
            "Mercari 商品頁資料：ヴァイスシュヴァルツ いっぱいの祝福 桐谷遥 SSP。 "
            "標示價格 ¥26,999。 商品狀態：目立った傷や汚れなし。"
        ),
        confidence=0.85,
        origin="research_command",
    )
    assert is_operational_cache_entry(op) is True


def test_is_operational_cache_entry_passes_real_entry(db):
    _seed_entity(db)
    assert is_operational_cache_entry(db.get_entry("union_arena")) is False


def test_is_insufficient_entry_stays_true_even_with_appended_observation(db):
    # An appended 最近觀察 bullet must NOT launder a stub into a 'real' entry:
    # the 資料不足 head + zero confidence still mark it insufficient.
    _seed_no_data_stub(db)
    entry = db.get_entry("pokeca_new_card")
    laundered = KnowledgeEntry(
        entry_id=entry.entry_id,
        entity_canonical=entry.entity_canonical,
        entity_type=entry.entity_type,
        summary=NO_DATA_SUMMARY + OBSERVATION_MARKER + "- [2026-06-08] 推文提及抽選",
        confidence=0.0,
    )
    assert is_insufficient_entry(laundered) is True


def test_append_observation_refuses_to_grow_no_data_stub(db):
    _seed_no_data_stub(db)
    ok = db.append_observation(
        entity_alias_or_canonical="pokeca_new_card",
        observed_at="2026-06-08T01:00:00+00:00",
        rationale="推文提及抽選與阿比斯眼",
        suggested_action="無需行動",
        tweet_url="https://x.com/pokeca_new_card/status/1",
    )
    assert ok is False
    # Summary must remain the bare stub — no observation marker accreted.
    entry = db.get_entry("pokeca_new_card")
    assert entry.summary == NO_DATA_SUMMARY
    assert OBSERVATION_MARKER not in entry.summary
