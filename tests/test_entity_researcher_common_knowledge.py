"""EntityResearcher must not store general common-knowledge entities.

Before researching, it asks the local model "do you already know this?"; if so it
skips the web search entirely and caches a hidden stub (filtered from the digest)."""

from __future__ import annotations

import json

from openclaw_adapter.entity_researcher import (
    EntityResearcher,
    _build_research_query,
    _grounding_token,
    _snippets_ground_entity,
)
from openclaw_adapter.knowledge_db import (
    COMMON_KNOWLEDGE_SUMMARY,
    KnowledgeDatabase,
    is_insufficient_entry,
)
from openclaw_adapter.web_search import WebSearchResult


def _snippet(title="", snippet="", url="https://example.com"):
    return WebSearchResult(title=title, url=url, snippet=snippet)


def _make_researcher(tmp_path, *, common, search_fn):
    db = KnowledgeDatabase(tmp_path / "knowledge.sqlite3")

    def json_call_fn(*, prompt, **_):
        if "守門員" in prompt and "common_knowledge" in prompt:
            return json.dumps({"common_knowledge": common, "reason": "test"})
        raise AssertionError("unexpected LLM prompt: condensation should not run")

    r = EntityResearcher(
        knowledge_db=db, endpoint="http://x", model="m",
        search_fn=search_fn, json_call_fn=json_call_fn,
    )
    return r, db


def test_common_knowledge_entity_skips_research_and_is_hidden(tmp_path):
    def search_fn(query, limit):
        raise AssertionError("web search must not run for common-knowledge entity")

    r, db = _make_researcher(tmp_path, common=True, search_fn=search_fn)
    r._handle("amazon")

    entry = db.get_entry("amazon")
    assert entry is not None
    assert entry.summary == COMMON_KNOWLEDGE_SUMMARY
    assert entry.confidence == 0.0
    assert is_insufficient_entry(entry), "common-knowledge stub must be hidden from digest"


def test_niche_entity_still_researched(tmp_path):
    searched: list[str] = []

    def search_fn(query, limit):
        searched.append(query)
        return []  # no snippets → research() returns None → no-data stub

    r, db = _make_researcher(tmp_path, common=False, search_fn=search_fn)
    r._handle("union_arena_obscure_set")

    assert searched, "non-common entity must reach the web search"
    entry = db.get_entry("union_arena_obscure_set")
    assert entry is not None
    assert entry.summary != COMMON_KNOWLEDGE_SUMMARY


def test_common_knowledge_check_fails_open_on_bad_json(tmp_path):
    searched: list[str] = []

    def search_fn(query, limit):
        searched.append(query)
        return []

    db = KnowledgeDatabase(tmp_path / "knowledge.sqlite3")

    def json_call_fn(*, prompt, **_):
        return "not json at all"

    r = EntityResearcher(
        knowledge_db=db, endpoint="http://x", model="m",
        search_fn=search_fn, json_call_fn=json_call_fn,
    )
    assert r._is_common_knowledge("whatever") is False
    r._handle("whatever")
    assert searched, "fail-open: a bad common-knowledge response must not block research"


# ── Anti-hallucination: query simplification + grounding gate ──────────────


def test_research_query_has_no_market_stuffing():
    q = _build_research_query("プロジェクトセカイ 鳳えむ")
    assert q == '"プロジェクトセカイ 鳳えむ"'
    for junk in ("TCG", "カード", "客群", "市場"):
        assert junk not in q


def test_grounding_token_picks_most_specific_segment():
    # Multi-segment → the broad IP token must NOT be the anchor.
    assert _grounding_token("プロジェクトセカイ 鳳えむ") == "鳳えむ"
    # Single token → whole name.
    assert _grounding_token("駿河屋") == "駿河屋"


def test_grounding_gate_rejects_junk_snippets():
    # None of these mention 鳳えむ — exactly the live failure mode.
    junk = [
        _snippet(title="ゲームセンター am-net", snippet="アミューズメント施設の案内"),
        _snippet(title="CAPCOM 決算説明会", snippet="2025年度の業績"),
    ]
    assert _snippets_ground_entity("プロジェクトセカイ 鳳えむ", junk) is False


def test_grounding_gate_accepts_on_specific_token_match():
    hits = [
        _snippet(title="無関係", snippet="ノイズ"),
        _snippet(title="鳳えむ とは", snippet="ワンダーランズ×ショウタイムの登場人物"),
    ]
    assert _snippets_ground_entity("プロジェクトセカイ 鳳えむ", hits) is True


def test_research_grounding_gate_skips_llm_on_junk(tmp_path):
    db = KnowledgeDatabase(tmp_path / "knowledge.sqlite3")
    llm_calls: list[str] = []

    def search_fn(query, limit):
        return [_snippet(title="ゲームセンター", snippet="無関係なノイズ")]

    def json_call_fn(*, prompt, **_):
        llm_calls.append(prompt)
        raise AssertionError("LLM must not run when snippets don't mention the entity")

    r = EntityResearcher(
        knowledge_db=db, endpoint="http://x", model="m",
        search_fn=search_fn, json_call_fn=json_call_fn,
    )
    assert r.research("プロジェクトセカイ 鳳えむ") is None
    assert llm_calls == []


def test_research_proceeds_to_llm_when_grounded(tmp_path):
    db = KnowledgeDatabase(tmp_path / "knowledge.sqlite3")

    def search_fn(query, limit):
        return [_snippet(title="鳳えむ", snippet="ワンダショの登場キャラクター")]

    def json_call_fn(*, prompt, **_):
        return json.dumps({
            "entity_type": "creator",
            "summary": "ワンダーランズ×ショウタイムの登場キャラクター。",
            "aliases": ["Emu"],
            "confident": True,
        })

    r = EntityResearcher(
        knowledge_db=db, endpoint="http://x", model="m",
        search_fn=search_fn, json_call_fn=json_call_fn,
    )
    result = r.research("プロジェクトセカイ 鳳えむ")
    assert result is not None
    assert result.entity_type == "creator"
