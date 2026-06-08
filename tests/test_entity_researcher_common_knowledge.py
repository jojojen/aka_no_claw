"""EntityResearcher must not store general common-knowledge entities.

Before researching, it asks the local model "do you already know this?"; if so it
skips the web search entirely and caches a hidden stub (filtered from the digest)."""

from __future__ import annotations

import json

from openclaw_adapter.entity_researcher import EntityResearcher
from openclaw_adapter.knowledge_db import (
    COMMON_KNOWLEDGE_SUMMARY,
    KnowledgeDatabase,
    is_insufficient_entry,
)


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
