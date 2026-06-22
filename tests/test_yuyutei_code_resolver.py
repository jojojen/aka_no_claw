"""Tests for YuyuteiGameCodeResolver (KB cache → LLM gate → /search grounding)."""
from __future__ import annotations

from dataclasses import dataclass

from openclaw_adapter.knowledge_db import KnowledgeDatabase
from openclaw_adapter.yuyutei_code_resolver import YuyuteiGameCodeResolver


@dataclass
class _Hit:
    title: str
    url: str = ""
    snippet: str = ""


class _RecordingLLM:
    """json_call_fn stub returning queued JSON replies, recording call count."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def __call__(self, **kwargs: object) -> str:
        self.calls += 1
        return self._replies.pop(0) if self._replies else "{}"


def _make_resolver(db_path, llm, *, search_fn=None) -> YuyuteiGameCodeResolver:
    return YuyuteiGameCodeResolver(
        knowledge_db_path=str(db_path),
        json_call_fn=llm,
        endpoint="http://localhost:11434",
        model="qwen3:14b",
        timeout_seconds=10,
        search_fn=search_fn,
    )


def test_normalize_raw_card_query_returns_llm_term(tmp_path) -> None:
    """#41: the LLM normalizer derives the clean raw-card search term from a noisy
    query so 遊々亭 stops 0-hitting."""
    db = tmp_path / "k.sqlite3"
    llm = _RecordingLLM(['{"query": "風に舞う花びらの中で 初音ミク SSP"}'])
    resolver = _make_resolver(db, llm)
    out = resolver.normalize_raw_card_query("風に舞う花びらの中で 初音ミク ssp プロセカ")
    assert out == "風に舞う花びらの中で 初音ミク SSP"
    assert llm.calls == 1


def test_normalize_raw_card_query_returns_none_when_unsure(tmp_path) -> None:
    """A null verdict (model unsure) yields None so the caller keeps its own term."""
    db = tmp_path / "k.sqlite3"
    llm = _RecordingLLM(['{"query": null}'])
    resolver = _make_resolver(db, llm)
    assert resolver.normalize_raw_card_query("謎のカード") is None


def test_direct_classify_resolves_without_search(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    llm = _RecordingLLM(['{"code": "ws"}'])
    search_calls = {"n": 0}

    def search_fn(q, limit):
        search_calls["n"] += 1
        return []

    resolver = _make_resolver(db, llm, search_fn=search_fn)
    assert resolver.resolve("大好きを前に 桐谷遥 SSP") == "ws"
    assert search_calls["n"] == 0  # model knew it → no web search spent
    assert llm.calls == 1


def test_cache_hit_skips_llm_and_search(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    # First call resolves + caches.
    llm = _RecordingLLM(['{"code": "ws"}'])
    resolver = _make_resolver(db, llm)
    assert resolver.resolve("プロセカ 桐谷遥 SSP") == "ws"

    # Second resolver instance, no replies queued: must hit the cache.
    llm2 = _RecordingLLM([])
    resolver2 = _make_resolver(db, llm2)
    assert resolver2.resolve("プロセカ 桐谷遥 SSP") == "ws"
    assert llm2.calls == 0


def test_search_grounding_when_model_unsure(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    # Step 1 says "search"; step 2 (grounding) returns the code.
    llm = _RecordingLLM(['{"code": "search"}', '{"code": "ws"}'])
    hits = [_Hit(title="ヴァイスシュヴァルツ プロジェクトセカイ", snippet="桐谷遥 SSP")]

    def search_fn(q, limit):
        return hits

    resolver = _make_resolver(db, llm, search_fn=search_fn)
    assert resolver.resolve("謎カード 桐谷遥") == "ws"
    assert llm.calls == 2


def test_non_tcg_caches_negative_and_returns_none(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    llm = _RecordingLLM(["null"])
    resolver = _make_resolver(db, llm)
    assert resolver.resolve("初音ミク フィギュア") is None

    # Negative is cached: a fresh resolver with no replies still returns None.
    llm2 = _RecordingLLM([])
    resolver2 = _make_resolver(db, llm2)
    assert resolver2.resolve("初音ミク フィギュア") is None
    assert llm2.calls == 0


def test_transient_llm_failure_not_cached(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"

    class _Boom:
        calls = 0

        def __call__(self, **kwargs: object) -> str:
            type(self).calls += 1
            raise RuntimeError("ollama down")

    boom = _Boom()
    resolver = _make_resolver(db, boom)
    assert resolver.resolve("プロセカ 桐谷遥 SSP") is None

    # No negative cached → a later working call still resolves.
    llm2 = _RecordingLLM(['{"code": "ws"}'])
    resolver2 = _make_resolver(db, llm2)
    assert resolver2.resolve("プロセカ 桐谷遥 SSP") == "ws"


def test_search_grounding_null_returns_none(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    llm = _RecordingLLM(['{"code": "search"}', "null"])
    resolver = _make_resolver(db, llm, search_fn=lambda q, limit: [_Hit(title="無関係")])
    assert resolver.resolve("謎の商品") is None
    # Negative cached: KB now has the marker.
    entry = KnowledgeDatabase(str(db)).get_entry("謎の商品")
    assert entry is not None and "yuyutei_code=none" in entry.summary


def test_enrich_cache_records_matched_title(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    query = "プロセカ 桐谷遥 SSP"
    # Resolve first → caches yuyutei_code=ws with no title yet.
    _make_resolver(db, _RecordingLLM(['{"code": "ws"}'])).resolve(query)

    # Enrich from already-fetched listing titles; LLM picks listing #2.
    llm = _RecordingLLM(['{"index": 2, "kind": "single"}'])
    resolver = _make_resolver(db, llm)
    titles = ("無関係カード A", "大好きを前に 桐谷遥 SSP/PJSK", "別カード C")
    resolver.enrich_cache(query, titles)

    entry = KnowledgeDatabase(str(db)).get_entry(query)
    assert entry is not None
    # Marker stays at head (cache lookup + digest-hide both rely on it).
    assert entry.summary.startswith("yuyutei_code=ws")
    # Verbatim matched title recorded; kind labelled.
    assert "大好きを前に 桐谷遥 SSP/PJSK" in entry.summary
    assert "単カード" in entry.summary
    # Code still resolvable from cache (no extra LLM call).
    assert _make_resolver(db, _RecordingLLM([])).resolve(query) == "ws"


def test_enrich_cache_no_match_leaves_entry(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    query = "プロセカ 桐谷遥 SSP"
    _make_resolver(db, _RecordingLLM(['{"code": "ws"}'])).resolve(query)

    # Model isn't sure which listing matches → picks nothing, no title stored.
    resolver = _make_resolver(db, _RecordingLLM(['{"index": null, "kind": null}']))
    resolver.enrich_cache(query, ("無関係 A", "無関係 B"))

    entry = KnowledgeDatabase(str(db)).get_entry(query)
    assert entry is not None and entry.summary.startswith("yuyutei_code=ws")
    assert "一致商品" not in entry.summary


def test_enrich_cache_skips_when_no_cached_code(tmp_path) -> None:
    db = tmp_path / "k.sqlite3"
    # No prior resolve → nothing cached; enrich must be a no-op (no LLM call).
    llm = _RecordingLLM(['{"index": 1, "kind": "single"}'])
    resolver = _make_resolver(db, llm)
    resolver.enrich_cache("未キャッシュ商品", ("何か A",))
    assert llm.calls == 0
    assert KnowledgeDatabase(str(db)).get_entry("未キャッシュ商品") is None
