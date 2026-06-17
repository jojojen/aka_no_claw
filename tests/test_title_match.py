"""Unit tests for the bge-m3 semantic title matcher.

Uses a deterministic in-process fake embedder so the tests never touch Ollama.
The point is the *wiring* — cosine-threshold keep/drop and the query-embed-fail
lexical fallback — not the quality of any real model (that's the live spike).
"""
from __future__ import annotations

from openclaw_adapter.title_match import build_semantic_title_matcher


def _make_embedder(table: dict[str, list[float] | None]):
    """Return an embedder that maps known text → vector, else a zero-ish vector."""

    def _embed(text: str) -> list[float] | None:
        return table.get(text)

    return _embed


def _lexical_fallback(query, items):
    # Marker fallback: keeps every item whose title contains the raw query.
    return [it for it in items if query in str(it.get("title") or "")]


def test_matcher_keeps_titles_above_threshold_and_drops_below() -> None:
    # Orthogonal-ish axes so cosine is easy to reason about.
    embedder = _make_embedder(
        {
            "query": [1.0, 0.0],
            "near": [0.95, 0.05],   # cosine ~0.998 -> keep
            "far": [0.0, 1.0],      # cosine 0.0    -> drop
        }
    )
    matcher = build_semantic_title_matcher(
        embedder, threshold=0.72, lexical_fallback=_lexical_fallback
    )

    items = [
        {"item_id": "a", "title": "near"},
        {"item_id": "b", "title": "far"},
    ]

    kept = matcher("query", items)

    assert [it["item_id"] for it in kept] == ["a"]


def test_matcher_skips_items_with_empty_or_unembeddable_titles() -> None:
    embedder = _make_embedder(
        {
            "query": [1.0, 0.0],
            "good": [1.0, 0.0],
            "bad": None,  # embedder returns nothing -> skipped, not crash
        }
    )
    matcher = build_semantic_title_matcher(
        embedder, threshold=0.5, lexical_fallback=_lexical_fallback
    )

    items = [
        {"item_id": "a", "title": "good"},
        {"item_id": "b", "title": ""},      # blank title -> skipped
        {"item_id": "c", "title": "bad"},   # unembeddable -> skipped
    ]

    kept = matcher("query", items)

    assert [it["item_id"] for it in kept] == ["a"]


def test_matcher_defers_to_lexical_fallback_when_query_embed_fails() -> None:
    embedder = _make_embedder({"query": None})  # query itself can't embed
    matcher = build_semantic_title_matcher(
        embedder, threshold=0.72, lexical_fallback=_lexical_fallback
    )

    items = [
        {"item_id": "a", "title": "query plus extra"},
        {"item_id": "b", "title": "something else"},
    ]

    kept = matcher("query", items)

    # Whole batch routed through the lexical fallback, not silently dropped.
    assert [it["item_id"] for it in kept] == ["a"]


def test_matcher_returns_empty_for_empty_items_without_embedding() -> None:
    calls: list[str] = []

    def _tracking_embed(text: str):
        calls.append(text)
        return [1.0, 0.0]

    matcher = build_semantic_title_matcher(
        _tracking_embed, threshold=0.72, lexical_fallback=_lexical_fallback
    )

    assert matcher("query", []) == []
    assert calls == []  # short-circuits before embedding the query
