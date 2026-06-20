"""`/source S<n>` inspection command (issue #9 D6): compact citations stay
traceable back to the original article."""

from __future__ import annotations

from openclaw_adapter.knowledge_db import KnowledgeDatabase
from openclaw_adapter.source_command import build_source_handler


class _Settings:
    def __init__(self, db_path):
        self.knowledge_db_path = db_path


def _handler(tmp_path):
    db_path = tmp_path / "knowledge.sqlite3"
    db = KnowledgeDatabase(db_path)
    return build_source_handler(_Settings(db_path)), db


def test_source_lookup_shows_traceable_fields(tmp_path):
    handler, db = _handler(tmp_path)
    sid = db.intern_source(
        "https://www.google.com/url?q=https%3A%2F%2Fwww.suruga-ya.jp%2Fitem%2F1",
        title="Suruga-ya item",
    )
    out = handler(sid, "123")
    assert sid in out
    assert "Suruga-ya item" in out
    assert "suruga-ya.jp" in out
    assert "https://www.suruga-ya.jp/item/1" in out  # canonical, resolves to real source


def test_source_lookup_shows_domain_registry_metadata(tmp_path):
    """A seeded domain enriches /source with display name, type and trust (#11)."""
    handler, db = _handler(tmp_path)
    sid = db.intern_source("https://www.suruga-ya.jp/item/2", title="Suruga-ya item 2")
    out = handler(sid, "123")
    assert "Suruga-ya（Marketplace）" in out
    assert "信任度：" in out


def test_source_lookup_unseeded_domain_uses_default(tmp_path):
    """An unseeded host falls back to the default source-type / trust prior."""
    handler, db = _handler(tmp_path)
    sid = db.intern_source("https://example.com/article", title="Some article")
    out = handler(sid, "123")
    assert "未收錄網域" in out
    assert "信任度：" in out


def test_source_unknown_id(tmp_path):
    handler, _ = _handler(tmp_path)
    assert "找不到" in handler("S999", "123")


def test_source_bad_token(tmp_path):
    handler, _ = _handler(tmp_path)
    assert "不是合法" in handler("nonsense", "123")


def test_source_empty_shows_usage(tmp_path):
    handler, _ = _handler(tmp_path)
    assert "用法" in handler("", "123")
