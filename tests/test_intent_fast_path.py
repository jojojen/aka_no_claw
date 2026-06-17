"""Embedding intent fast-path: zero-arg short-circuit vs LLM fall-through.

Uses a deterministic in-process fake embedder so the test never touches Ollama.
The point is the *policy* — that confident zero-arg intents short-circuit, while
slot-bearing or ambiguous utterances return None so the LLM router still runs —
not the quality of any real model (that's the env-gated spike).
"""
from __future__ import annotations

import math

import pytest

from openclaw_adapter.intent_fast_path import (
    URL_SLOT_INTENTS,
    ZERO_ARG_INTENTS,
    EmbeddingIntentRouter,
)


class WordSetEmbedder:
    """Maps text to a sparse vector over a fixed vocabulary by word membership.
    Utterances sharing words get high cosine; disjoint ones get ~0."""

    model = "fake-embed"
    dim = 0

    _VOCAB = [
        "list", "sns", "twitter", "watch", "mercari", "status", "model",
        "tools", "help", "scan", "photo", "track", "price", "card", "buy",
        "research", "reputation", "seller", "item", "value", "investment",
    ]

    def __call__(self, text: str) -> list[float] | None:
        toks = set((text or "").lower().split())
        vec = [1.0 if w in toks else 0.0 for w in self._VOCAB]
        return vec if any(vec) else None


# Phrasings expressed in the fake embedder's vocabulary so cosine is meaningful.
PHRASINGS = {
    "sns_list": ["list sns twitter", "sns twitter list"],
    "list_watches": ["list mercari watch", "mercari watch list"],
    "status": ["status model", "status"],
    "tools": ["tools", "tools list"],
    "help": ["help", "help"],
    "scan_help": ["scan photo card", "scan photo"],
    "add_watch": ["track price card", "watch price buy"],
    "sns_add_account": ["track sns twitter", "sns twitter track"],
    # URL-slot intents: phrasings ship with URLs (stripped at index build).
    "product_research": [
        "research item value investment https://jp.mercari.com/item/m1",
        "item value research investment",
    ],
    "reputation_snapshot": [
        "reputation seller check https://jp.mercari.com/item/m1",
        "seller reputation",
    ],
}


@pytest.fixture
def router():
    return EmbeddingIntentRouter(
        WordSetEmbedder(), PHRASINGS, min_score=0.6, margin=0.03
    )


def test_zero_arg_intent_short_circuits(router):
    intent = router.route("list sns twitter")
    assert intent is not None
    assert intent.intent == "sns_list"
    assert intent.confidence is not None and intent.confidence >= 0.6


def test_distinct_zero_arg_pair_separates(router):
    # mercari watch list -> list_watches, NOT sns_list
    intent = router.route("list mercari watch")
    assert intent is not None
    assert intent.intent == "list_watches"


def test_slot_bearing_intent_falls_through(router):
    # Best match is a slot-bearing intent (add_watch) -> never short-circuit.
    assert router.route("track price card buy") is None


def test_sns_add_account_not_short_circuited(router):
    # "track sns twitter" matches sns_add_account (slot-bearing) -> None,
    # even though it also overlaps the sns_list vocabulary.
    assert router.route("track sns twitter") is None


def test_low_confidence_falls_through():
    r = EmbeddingIntentRouter(
        WordSetEmbedder(), PHRASINGS, min_score=0.99, margin=0.03
    )
    # "model" only partially overlaps the status phrasing (cosine ~0.71),
    # below the 0.99 floor -> defer to LLM.
    assert r.route("model") is None


def test_unembeddable_text_returns_none(router):
    # No vocabulary words -> embedder returns None -> defer.
    assert router.route("xxxxx yyyyy zzzzz") is None


def test_empty_index_disables_router():
    r = EmbeddingIntentRouter(WordSetEmbedder(), {}, min_score=0.6)
    assert r.ready is False
    assert r.route("list sns twitter") is None


def test_embedder_exception_defers_to_llm():
    class Boom:
        model = "boom"
        dim = 0

        def __call__(self, text: str):
            raise RuntimeError("embed outage")

    # Construction swallows per-phrasing failures -> empty index -> not ready.
    r = EmbeddingIntentRouter(Boom(), PHRASINGS, min_score=0.6)
    assert r.ready is False
    assert r.route("list sns twitter") is None


def test_zero_arg_set_is_subset_of_phrasings():
    # Every zero-arg intent we short-circuit must have phrasings shipped.
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "intent_routing_phrasings.json"
    )
    shipped = set(json.loads(path.read_text(encoding="utf-8")))
    assert ZERO_ARG_INTENTS <= shipped


def test_url_slot_set_is_subset_of_phrasings():
    # The URL-slot intents we fast-path must also have phrasings shipped.
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "intent_routing_phrasings.json"
    )
    shipped = set(json.loads(path.read_text(encoding="utf-8")))
    assert URL_SLOT_INTENTS <= shipped


_MERCARI_URL = "https://jp.mercari.com/item/m32448674223"


def test_verb_plus_mercari_url_fast_paths_product_research(router):
    intent = router.route(f"research item value investment {_MERCARI_URL}")
    assert intent is not None
    assert intent.intent == "product_research"
    assert intent.query_url == _MERCARI_URL  # URL slot lifted verbatim
    assert intent.confidence is not None and intent.confidence >= 0.6


def test_verb_plus_mercari_url_fast_paths_reputation(router):
    intent = router.route(f"reputation seller check {_MERCARI_URL}")
    assert intent is not None
    assert intent.intent == "reputation_snapshot"
    assert intent.query_url == _MERCARI_URL


def test_bare_mercari_url_defers_to_llm(router):
    # No verb residual to disambiguate research vs reputation -> defer.
    assert router.route(_MERCARI_URL) is None


def test_non_mercari_url_is_not_url_slot_fast_pathed(router):
    # URL-slot only fires for Mercari product URLs; other URLs defer.
    assert router.route("research item value https://example.com/item/1") is None
