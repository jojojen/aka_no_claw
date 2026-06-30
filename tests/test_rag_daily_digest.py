"""The daily RAG digest must never push 資料不足 no-data stubs to the user."""

from __future__ import annotations

import json
from dataclasses import replace

from openclaw_adapter.collectible_signal import make_signal
from openclaw_adapter.collectible_signal_store import CollectibleSignalStore
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


def test_digest_skips_operational_cache_entry(tmp_path):
    """The 遊々亭 game-code cache is internal plumbing (kept only to avoid
    re-searching) — it must never surface in the digest, even at confidence ≥ 0.1."""
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="プロセカ 桐谷遥 SSP", entity_type="tcg",
        summary="yuyutei_code=ws. 遊々亭一致商品「大好きを前に 桐谷遥 SSP」（単カード）. 検索語…",
        confidence=0.6, origin="research_command",
    )
    sched._send_digest()
    assert sent == [], "yuyutei_code= operational cache must not be pushed"


def test_digest_skips_mercari_item_page_cache(tmp_path):
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="mercari:m93045899435",
        entity_type="product",
        summary=(
            "Mercari 商品頁資料：ヴァイスシュヴァルツ いっぱいの祝福 桐谷遥 SSP。 "
            "標示價格 ¥26,999。 商品狀態：目立った傷や汚れなし。"
        ),
        source_urls=("https://jp.mercari.com/item/m93045899435",),
        confidence=0.85,
        origin="research_command",
    )
    sched._send_digest()
    assert sent == [], "Mercari item-page cache must not be pushed as RAG news"


def test_digest_renders_compact_source_citation(tmp_path):
    """Issue #9 D5: S-id source refs render as ``[S1] domain``, never the raw
    multi-thousand-char redirect URL."""
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    sid = db.intern_source(
        "https://www.google.com/url?q=https%3A%2F%2Fwww.suruga-ya.jp%2Fitem%2F1"
        "&utm_source=x",
        title="Suruga-ya item",
    )
    db.upsert_entry(
        entity_canonical="union_arena", entity_type="tcg",
        summary="UNION ARENA。Bandai 旗下 TCG。", confidence=0.7,
        source_urls=(sid,), origin="web_research",
    )
    sched._send_digest()
    assert len(sent) == 1
    text = sent[0][1]
    assert f"[{sid}] Suruga-ya (Marketplace)" in text  # issue #11 enriched label
    assert "google.com/url" not in text  # no raw redirect wrapper leaked
    assert "utm_source" not in text


def test_digest_legacy_raw_url_degrades_to_domain(tmp_path):
    """Pre-registry raw URLs (not S-ids) still render as a clean domain label.

    upsert_entry now interns sources, so a *legacy* raw-url row is simulated by
    writing source_urls_json directly — the case real pre-#9 rows present."""
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="union_arena", entity_type="tcg",
        summary="UNION ARENA。Bandai 旗下 TCG。", confidence=0.7,
        origin="web_research",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE knowledge_entries SET source_urls_json = ? "
            "WHERE entity_canonical = ?",
            (json.dumps(["https://www.suruga-ya.jp/item/9?utm_source=x"]),
             "union_arena"),
        )
    sched._send_digest()
    assert len(sent) == 1
    # legacy raw url → seeded domain label (issue #11), not the raw wrapper
    assert "Suruga-ya (Marketplace)" in sent[0][1]
    assert "utm_source" not in sent[0][1]


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


def test_digest_skips_sparse_product_signal_without_source(tmp_path):
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    sig_path = tmp_path / "signals.sqlite3"
    sig_store = CollectibleSignalStore(sig_path)
    sig_store.bootstrap()
    sig_store.upsert_signal(make_signal(
        source_kind="official_store",
        collectible_domain="tcg",
        ip_canonical="鏈鋸人",
        title="UA 鏈鋸人 BOX",
        product_type="sealed_box",
        confidence=0.95,
        actionability="informational",
    ))
    sched = RagDailyDigestScheduler(
        db_path=db_path,
        chat_ids=("123",),
        send_fn=lambda chat_id, text, markup: sent.append((chat_id, text)),
        signal_db_path=sig_path,
    )

    sched._send_digest()

    assert sent == []


def test_digest_does_not_repeat_old_signal_refreshed_today(tmp_path):
    sent: list = []
    sched, db_path = _make_scheduler(tmp_path, sent)
    sig_path = tmp_path / "signals.sqlite3"
    sig_store = CollectibleSignalStore(sig_path)
    sig_store.bootstrap()
    old = replace(
        make_signal(
            source_kind="official_store",
            collectible_domain="tcg",
            ip_canonical="鏈鋸人",
            title="UA 鏈鋸人 BOX",
            product_type="sealed_box",
            official_code="UA-CSM-01",
            source_urls=("https://store.example/csm-box",),
            confidence=0.95,
            actionability="actionable",
        ),
        created_at="2020-01-01T00:00:00+00:00",
    )
    sig_store.upsert_signal(old)
    sig_store.upsert_signal(replace(old, heat_score=0.9))
    sched = RagDailyDigestScheduler(
        db_path=db_path,
        chat_ids=("123",),
        send_fn=lambda chat_id, text, markup: sent.append((chat_id, text)),
        signal_db_path=sig_path,
    )

    sched._send_digest()

    assert sent == []
