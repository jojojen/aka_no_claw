"""Layer B: percentile honesty + skip empty-signal knowledge writes.

Exercises the real ``_record_ip_heat`` closure built by ``_build_sns_buzz_fn``
against a temp IpHeatStore + KnowledgeDatabase, stubbing only the LLM digest so
no network/Ollama is needed. ``_build_sns_buzz_fn`` imports
``summarize_topic_sync`` from ``sns_monitor.digest`` *inside* the function, so we
patch it at that source module.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import sns_monitor.digest as digest_mod
from assistant_runtime import AssistantSettings
from openclaw_adapter.knowledge_db import KnowledgeDatabase
import openclaw_adapter.sns_tools as sns_tools
from sns_monitor.digest import BuzzResult


class _FakeFourchan:
    def __init__(self, value: float, matched: int) -> None:
        self._value = value
        self._matched = matched

    def measure_ip_heat_sync(self, query: str, aliases=()):
        return self._value, self._matched


def _settings(tmpdir: str) -> AssistantSettings:
    return AssistantSettings(
        knowledge_db_path=str(Path(tmpdir) / "knowledge.sqlite3"),
        openclaw_local_text_backend="ollama",
        openclaw_local_text_endpoint="http://localhost:11434",
        openclaw_local_text_model="qwen3:14b",
    )


def _seed_entry(settings) -> KnowledgeDatabase:
    db = KnowledgeDatabase(settings.knowledge_db_path)
    db.upsert_entry(
        entity_canonical="chainsaw man", entity_type="ip",
        summary="鏈鋸人，集英社 IP。", confidence=0.6,
    )
    return db


def _build(settings, fourchan, result, monkeypatch):
    monkeypatch.setattr(digest_mod, "summarize_topic_sync", lambda q, **kw: result)
    return sns_tools._build_sns_buzz_fn(
        settings, x_client=object(), fourchan_client=fourchan
    )


def test_no_percentile_below_min_history(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _settings(tmpdir)
        _seed_entry(settings)
        result = BuzzResult(
            query="chainsaw man", summary="熱門標的：UA 鏈鋸人。收藏訊號：高。",
            sources=[], fetched_count=10, hot_items=("UA 鏈鋸人",),
            catalyst="新彈上市", collectible_signal="high",
        )
        buzz = _build(settings, _FakeFourchan(120.0, 4), result, monkeypatch)
        out = buzz("chainsaw man")
        # Single data point → percentile suppressed (would be a misleading 100%).
        assert "百分位" not in out
        assert "pct" not in out
        assert "已存收藏訊號入知識庫" in out


def test_signal_writes_concrete_observation(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _settings(tmpdir)
        db = _seed_entry(settings)
        result = BuzzResult(
            query="chainsaw man",
            summary="熱門標的：UA 鏈鋸人 EX。催化：新彈上市。收藏訊號：高。",
            sources=[], fetched_count=12, hot_items=("UA 鏈鋸人 EX",),
            catalyst="新彈上市", actionable="關注未開封盒", collectible_signal="high",
        )
        buzz = _build(settings, _FakeFourchan(200.0, 5), result, monkeypatch)
        buzz("chainsaw man")

        entry = db.get_entry("chainsaw man")
        assert entry is not None
        # Canonical head preserved; observation appended with concrete nouns.
        assert "鏈鋸人，集英社 IP。" in entry.summary
        assert "標的：UA 鏈鋸人 EX" in entry.summary
        assert "催化：新彈上市" in entry.summary
        assert "可留意：關注未開封盒" in entry.summary
        assert "4chan熱度 value=200" in entry.summary


def test_cloud_distiller_wired_with_local_fallback(monkeypatch):
    """When the cloud enricher is available, buzz wires an llm_call_fn (cloud
    with single local fallback) + a deep_context_fn into the digest."""
    import openclaw_adapter.dynamic_tools as dyn
    from openclaw_adapter.dynamic_tools import CloudBackendUnavailable

    class _FakeCloud:
        def generate(self, prompt, *, temperature=0.0, think=False):
            raise CloudBackendUnavailable("cloud down")

    monkeypatch.setattr(dyn, "build_research_cloud_text_client", lambda s, **kw: _FakeCloud())
    # Local fallback sentinel (digest._call_ollama is imported at call time).
    monkeypatch.setattr(digest_mod, "_call_ollama", lambda *a, **k: "LOCAL_FALLBACK_OK")

    captured = {}

    def fake_summarize(query, **kw):
        captured.update(kw)
        return None  # short-circuit; we only inspect wiring

    monkeypatch.setattr(digest_mod, "summarize_topic_sync", fake_summarize)

    class _FC:
        def deep_context(self, tweets, *, top_n=3):
            return "DC"

        def measure_ip_heat_sync(self, q, aliases=()):
            return (0.0, 0)

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _settings(tmpdir)
        buzz = sns_tools._build_sns_buzz_fn(settings, x_client=object(), fourchan_client=_FC())
        buzz("pjsk")

    assert captured["deep_context_fn"] is not None
    assert captured["llm_call_fn"] is not None
    # Cloud raises CloudBackendUnavailable → llm_call_fn degrades to local once.
    assert captured["llm_call_fn"]("prompt") == "LOCAL_FALLBACK_OK"


def test_query_expands_to_aliases_for_search_and_heat(monkeypatch):
    """A user term is RAG-expanded to its knowledge-DB aliases before 4chan
    matching, so 'pjsk' searches '/psg/ - Project SEKAI General'. The aliases
    must reach both the digest search and the heat measurement."""
    captured = {}

    class _AliasFourchan:
        def measure_ip_heat_sync(self, query, aliases=()):
            captured["heat_query"] = query
            captured["heat_aliases"] = tuple(aliases)
            return (0.0, 0)

    def fake_summarize(query, **kw):
        captured["search_aliases"] = tuple(kw.get("search_aliases", ()))
        return None

    monkeypatch.setattr(digest_mod, "summarize_topic_sync", fake_summarize)

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _settings(tmpdir)
        db = KnowledgeDatabase(settings.knowledge_db_path)
        db.upsert_entry(
            entity_canonical="pjsk", entity_type="ip",
            summary="Project Sekai 音遊。", confidence=0.6,
            aliases=("Project Sekai", "プロセカ"),
        )
        buzz = sns_tools._build_sns_buzz_fn(
            settings, x_client=object(), fourchan_client=_AliasFourchan()
        )
        buzz("pjsk")

    assert "Project Sekai" in captured["search_aliases"]
    assert "プロセカ" in captured["search_aliases"]
    # Heat recorded under the canonical, measured with the same aliases.
    assert captured["heat_query"] == "pjsk"
    assert "Project Sekai" in captured["heat_aliases"]


def test_bare_chatter_skips_knowledge_write(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _settings(tmpdir)
        db = _seed_entry(settings)
        result = BuzzResult(
            query="chainsaw man",
            summary="多為一般討論，無明確收藏催化（收藏訊號：低）。",
            sources=[], fetched_count=8, collectible_signal="low",
        )
        buzz = _build(settings, _FakeFourchan(40.0, 2), result, monkeypatch)
        out = buzz("chainsaw man")

        entry = db.get_entry("chainsaw man")
        # No 最近觀察 bullet accreted onto the entry.
        assert "最近觀察" not in entry.summary
        assert "常態討論，僅記錄熱度數字" in out
