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
