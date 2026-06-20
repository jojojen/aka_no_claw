"""Source registry (issue #9 D2/D3): stable compact ids + dedup by canonical URL."""

from __future__ import annotations

from openclaw_adapter.knowledge_db import (
    KnowledgeDatabase,
    SourceRecord,
    is_source_id,
)


def _db(tmp_path):
    return KnowledgeDatabase(tmp_path / "k.sqlite3")


def test_intern_returns_stable_compact_id(tmp_path):
    db = _db(tmp_path)
    sid = db.intern_source("https://www.suruga-ya.jp/product/detail/12345?utm_source=x")
    assert is_source_id(sid)
    assert sid == "S1"


def test_dedup_collapses_redirect_and_tracking_wrappers(tmp_path):
    db = _db(tmp_path)
    a = db.intern_source("https://www.google.com/url?q=https%3A%2F%2Fsite.jp%2Fx%3Futm_source%3Da")
    b = db.intern_source("https://site.jp/x?fbclid=zzz")
    assert a == b  # same canonical URL → same record (D3)


def test_distinct_urls_get_distinct_ids(tmp_path):
    db = _db(tmp_path)
    a = db.intern_source("https://a.jp/1")
    b = db.intern_source("https://b.jp/2")
    assert a != b


def test_get_source_round_trip(tmp_path):
    db = _db(tmp_path)
    sid = db.intern_source("https://x.com/search?q=foo", title="X Search")
    rec = db.get_source(sid)
    assert isinstance(rec, SourceRecord)
    assert rec.source_id == sid
    assert rec.canonical_url == "https://x.com/search?q=foo"
    assert rec.domain == "x.com"
    assert rec.title == "X Search"
    assert rec.fetched_at  # auto-stamped


def test_get_source_unknown_or_bad_id(tmp_path):
    db = _db(tmp_path)
    assert db.get_source("S999") is None
    assert db.get_source("nonsense") is None
    assert db.get_source("") is None


def test_intern_empty_url_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.intern_source("") is None
    assert db.intern_source("   ") is None


def test_intern_refuses_opaque_redirect(tmp_path):
    # Opaque Yahoo listing redirect: 400 on fetch, no offline unwrap → not
    # traceable back to the original article, so must not become a citation (#9).
    db = _db(tmp_path)
    assert db.intern_source(
        "https://rd.listing.yahoo.co.jp/p/search/GU=opaqueblob;/?ep=more&v=2"
    ) is None


def test_title_backfill_on_reintern(tmp_path):
    db = _db(tmp_path)
    sid = db.intern_source("https://a.jp/1")  # no title
    again = db.intern_source("https://a.jp/1", title="A Site")
    assert again == sid
    assert db.get_source(sid).title == "A Site"
