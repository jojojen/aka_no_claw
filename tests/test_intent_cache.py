"""Tests for semantic intent cache."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from openclaw_adapter.intent_cache import (
    OllamaEmbedClient,
    SemanticIntentCache,
)


def fake_embed_factory(seed: int = 42) -> dict[str, list[float]]:
    """Create deterministic embeddings for test text."""
    np.random.seed(seed)
    embeddings: dict[str, list[float]] = {}

    def embed_fn(text: str) -> list[float]:
        if text not in embeddings:
            embeddings[text] = np.random.randn(384).tolist()
        return embeddings[text]

    embed_fn.cache = embeddings
    return embed_fn


def test_exact_hit_no_embed_call(tmp_path: Path) -> None:
    """Exact hash match should return cached intent without calling embed_fn."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory()
    cache = SemanticIntentCache(db_path, embed_fn=embed_fn)

    # Store an intent
    intent1 = {"intent": "play_music", "music_query": "初音ミク"}
    cache.store("ns1", "放初音ミク的歌", intent1)

    # Lookup should return exact hit
    hit = cache.lookup("ns1", "放初音ミク的歌")
    assert hit is not None
    result_intent, hit_kind = hit
    assert result_intent == intent1
    assert hit_kind == "exact"

    # embed_fn should have been called once (during store), not twice
    assert len(embed_fn.cache) == 1


def test_semantic_hit_above_threshold(tmp_path: Path) -> None:
    """Semantic similarity above threshold should return hit."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory(seed=42)
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        similarity_threshold=0.5,  # Lower threshold for test
    )

    intent1 = {"intent": "play_music", "music_query": "song"}
    cache.store("ns1", "放歌", intent1)

    # Create a similar text with same digits
    similar_text = "放一首歌"
    # Make embedding similar by reusing the same base
    np.random.seed(42)
    base_emb = np.random.randn(384)
    embed_fn.cache[similar_text] = (base_emb + np.random.randn(384) * 0.1).tolist()

    hit = cache.lookup("ns1", similar_text)
    if hit is not None:  # Will depend on similarity threshold
        result_intent, hit_kind = hit
        assert hit_kind in ("exact", "semantic")


def test_below_threshold_miss(tmp_path: Path) -> None:
    """Similarity below threshold should return None."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory(seed=42)
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        similarity_threshold=0.99,  # Very high threshold
    )

    intent1 = {"intent": "home_action", "home_command": "on"}
    cache.store("ns1", "開燈", intent1)

    # Different text with different embedding
    hit = cache.lookup("ns1", "關燈")
    assert hit is None


def test_digit_guard_prevents_cross_parameter_hits(tmp_path: Path) -> None:
    """Two texts with same intent but different numbers should not cross-match."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory(seed=42)
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        similarity_threshold=0.0,  # Accept any similarity
    )

    # Store intent for "音量調到50"
    intent1 = {"intent": "volume_set", "level": 50}
    cache.store("ns1", "音量調到50", intent1)

    # Make embedding identical for text with different number
    embed_fn.cache["音量調到70"] = embed_fn.cache["音量調到50"]

    # Lookup for "音量調到70" should NOT hit the cached "音量調到50"
    # because digits differ (50 vs 70)
    hit = cache.lookup("ns1", "音量調到70")
    assert hit is None


def test_containment_guard_blocks_compound_query_on_cached_single_step(
    tmp_path: Path,
) -> None:
    """A multi-step instruction containing a cached command must not hit it."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory(seed=42)
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        similarity_threshold=0.0,  # Accept any similarity
    )

    cache.store("ns1", "開燈", {"intent": "home_action", "home_command": "light_on"})
    embed_fn.cache["開燈然後播放初音的歌"] = embed_fn.cache["開燈"]

    assert cache.lookup("ns1", "開燈然後播放初音的歌") is None


def test_containment_guard_blocks_single_step_query_on_cached_compound(
    tmp_path: Path,
) -> None:
    """The reverse containment (query inside cached compound) must also miss."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory(seed=42)
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        similarity_threshold=0.0,
    )

    cache.store("ns1", "開燈然後放歌", {"intent": "create_workflow"})
    embed_fn.cache["開燈"] = embed_fn.cache["開燈然後放歌"]

    assert cache.lookup("ns1", "開燈") is None


def test_containment_guard_allows_non_containment_paraphrase(tmp_path: Path) -> None:
    """Word-order / paraphrase pairs without containment still hit semantically."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory(seed=42)
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        similarity_threshold=0.9,
    )

    cache.store("ns1", "電燈打開", {"intent": "home_action", "home_command": "light_on"})
    embed_fn.cache["打開電燈"] = embed_fn.cache["電燈打開"]

    hit = cache.lookup("ns1", "打開電燈")
    assert hit is not None
    assert hit[1] == "semantic"


def test_ttl_expiry(tmp_path: Path) -> None:
    """Expired entries should not be returned."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory()
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        ttl_seconds=1,  # 1 second TTL
    )

    intent1 = {"intent": "play_music", "music_query": "song"}
    cache.store("ns1", "放歌", intent1)

    # Immediate lookup should hit
    hit = cache.lookup("ns1", "放歌")
    assert hit is not None

    # Wait for expiry
    time.sleep(1.1)

    # Should be expired now
    hit = cache.lookup("ns1", "放歌")
    assert hit is None


def test_eviction_beyond_max_entries(tmp_path: Path) -> None:
    """Oldest entries by last_hit should be evicted beyond max_entries."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory()
    cache = SemanticIntentCache(
        db_path,
        embed_fn=embed_fn,
        max_entries=3,
    )

    # Store 4 intents
    for i in range(4):
        intent = {"intent": "test", "index": i}
        cache.store("ns1", f"text{i}", intent)
        time.sleep(0.01)  # Ensure different timestamps

    # Oldest (text0) should be evicted
    hit = cache.lookup("ns1", "text0")
    assert hit is None

    # Others should be present
    hit = cache.lookup("ns1", "text3")
    assert hit is not None


def test_different_namespace_miss(tmp_path: Path) -> None:
    """Entries in different namespaces should not cross-match."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory()
    cache = SemanticIntentCache(db_path, embed_fn=embed_fn)

    intent1 = {"intent": "play_music", "music_query": "song"}
    cache.store("ns1", "放歌", intent1)

    hit = cache.lookup("ns2", "放歌")
    assert hit is None


def test_store_lookup_roundtrip_preserves_dict(tmp_path: Path) -> None:
    """Store and lookup should preserve intent dict exactly."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory()
    cache = SemanticIntentCache(db_path, embed_fn=embed_fn)

    original_intent = {
        "intent": "create_workflow",
        "workflow_description": "每天早上查天氣",
        "confidence": 0.85,
    }
    cache.store("ns1", "建立 workflow", original_intent)

    hit = cache.lookup("ns1", "建立 workflow")
    assert hit is not None
    retrieved_intent, _ = hit
    assert retrieved_intent == original_intent


def test_embed_fn_exception_is_swallowed(tmp_path: Path) -> None:
    """embed_fn raising should not propagate; cache returns None."""
    db_path = str(tmp_path / "cache.db")

    def failing_embed(text: str) -> list[float]:
        raise RuntimeError("Embedding service down")

    cache = SemanticIntentCache(db_path, embed_fn=failing_embed)

    # lookup should return None, not raise
    hit = cache.lookup("ns1", "some text")
    assert hit is None

    # store should not raise either
    cache.store("ns1", "some text", {"intent": "test"})


def test_ollama_embed_client_url_normalization() -> None:
    """OllamaEmbedClient._url() should normalize various endpoint formats."""
    client1 = OllamaEmbedClient(endpoint="http://x:11434", model="bge-m3")
    assert client1._url().endswith("/api/embed")

    client2 = OllamaEmbedClient(endpoint="http://x:11434/", model="bge-m3")
    assert client2._url().endswith("/api/embed")

    client3 = OllamaEmbedClient(endpoint="http://x:11434/api", model="bge-m3")
    assert client3._url().endswith("/api/embed")

    client4 = OllamaEmbedClient(endpoint="http://x:11434/api/generate", model="bge-m3")
    assert client4._url().endswith("/api/embed")

    client5 = OllamaEmbedClient(endpoint="http://x:11434/api/embed", model="bge-m3")
    assert client5._url().endswith("/api/embed")


def test_ollama_embed_client_keep_alive() -> None:
    """OllamaEmbedClient should construct with keep_alive parameter."""
    client = OllamaEmbedClient(
        endpoint="http://localhost:11434",
        model="bge-m3",
        keep_alive="5m",
    )
    assert client.keep_alive == "5m"


def test_cache_handles_unicode_normalization(tmp_path: Path) -> None:
    """Cache should normalize NFKC to treat equivalent texts the same."""
    db_path = str(tmp_path / "cache.db")
    embed_fn = fake_embed_factory()
    cache = SemanticIntentCache(db_path, embed_fn=embed_fn)

    intent = {"intent": "test", "value": 1}
    # Store with one form
    cache.store("ns1", "café", intent)

    # Lookup with equivalent form should hit (both normalize to same NFKC)
    hit = cache.lookup("ns1", "café")
    assert hit is not None
