"""The daily RAG digest must never push 資料不足 no-data stubs to the user."""

from __future__ import annotations

from openclaw_adapter.knowledge_db import KnowledgeDatabase, NO_DATA_SUMMARY
from openclaw_adapter.rag_daily_digest import RagDailyDigestScheduler


def _make_scheduler(tmp_path, sent):
    db_path = tmp_path / "knowledge.sqlite3"
    KnowledgeDatabase(db_path)  # bootstrap
    sched = RagDailyDigestScheduler(
        db_path=db_path,
        chat_ids=("123",),
        send_fn=lambda chat_id, text, markup: sent.append((chat_id, text)),
    )
    return sched, db_path


def test_digest_skips_no_data_stub(tmp_path):
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="pokeca_new_card", entity_type="other",
        summary=NO_DATA_SUMMARY, confidence=0.0, origin="web_research",
    )
    sched._send_digest()
    assert sent == [], "資料不足 stub must not be pushed"


def test_digest_sends_real_entry(tmp_path):
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="union_arena", entity_type="tcg",
        summary="UNION ARENA。Bandai 旗下 TCG。", confidence=0.7,
        origin="web_research",
    )
    sched._send_digest()
    assert len(sent) == 1
    assert "UNION ARENA" in sent[0][1]


def test_digest_mixed_sends_only_real(tmp_path):
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="union_arena", entity_type="tcg",
        summary="UNION ARENA。Bandai 旗下 TCG。", confidence=0.7, origin="web_research",
    )
    db.upsert_entry(
        entity_canonical="pokeca_new_card", entity_type="other",
        summary=NO_DATA_SUMMARY, confidence=0.0, origin="web_research",
    )
    sched._send_digest()
    assert len(sent) == 1
    assert "union_arena".upper() in sent[0][1] or "UNION ARENA" in sent[0][1]
