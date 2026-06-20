"""Issue #8 — collectible intelligence funnel (V1).

Tests map 1:1 onto the issue Test Plan:

- low-signal SNS chatter rejected from intelligence creation
- concrete SNS catalyst → evidence (informational), not a recommendation
- official/store TCG listing → structured signal + valid opportunity candidate
- non-TCG (shikishi / cd) representable as intelligence-only
- Mercari product-page cache stays out of the RAG digest (regression)
- integration: official TCG preorder → signal → candidate → valuation → promotion
- diagnostics enumerate every block reason
"""
from __future__ import annotations

import json

from openclaw_adapter.opportunity_models import OpportunityCandidate
from openclaw_adapter.collectible_signal import (
    ANCHOR_OFFICIAL_STORE_LISTING,
    ANCHOR_SNS_CATALYST,
    BLOCK_LOW_PRICE_CONFIDENCE,
    BLOCK_MISSING_MARKET_VALIDATION,
    BLOCK_NO_CONCRETE_PRODUCT,
    BLOCK_UNSUPPORTED_DOMAIN,
    candidate_to_signal,
    make_signal,
)
from openclaw_adapter.collectible_signal_store import CollectibleSignalStore
from openclaw_adapter.collectible_sns_gate import classify_sns_post
from openclaw_adapter.collectible_valuation import (
    MarketValuation,
    TcgMarketValuationProvider,
    promote_signal,
)


def _stub_llm(payload: dict):
    return lambda prompt: json.dumps(payload)


# --- Test Plan #1: low-signal SNS general discussion rejected -----------------

def test_low_signal_sns_chatter_creates_no_intelligence():
    # collectible-related but no concrete anchor → heat only, no signal
    chatter = classify_sns_post(
        text="新弾そろそろかな〜楽しみ！",
        source_kind="sns",
        llm_fn=_stub_llm({
            "is_collectible": True, "confidence": 0.9,
            "has_concrete_anchor": False, "collectible_domain": "tcg",
            "ip_canonical": "", "heat_score": 0.4,
        }),
    )
    assert chatter is None

    # not collectible at all → no signal
    offtopic = classify_sns_post(
        text="今日のランチ最高",
        llm_fn=_stub_llm({"is_collectible": False, "confidence": 0.95}),
    )
    assert offtopic is None

    # low model confidence → no signal even if it claims an anchor
    unsure = classify_sns_post(
        text="なんか出るらしい",
        llm_fn=_stub_llm({
            "is_collectible": True, "confidence": 0.2,
            "has_concrete_anchor": True, "collectible_domain": "tcg",
            "ip_canonical": "X",
        }),
    )
    assert unsure is None


# --- Test Plan #2: concrete SNS catalyst = evidence, not recommendation -------

def test_concrete_sns_catalyst_is_evidence_only():
    signal = classify_sns_post(
        text="ヴァイスシュヴァルツ Project SEKAI ブースターBOX 予約開始",
        source_kind="twitter",
        source_url="https://x.com/p/1",
        llm_fn=_stub_llm({
            "is_collectible": True, "confidence": 0.9,
            "has_concrete_anchor": True, "collectible_domain": "tcg",
            "entity_kind": "set", "product_type": "sealed_box",
            "ip_canonical": "Project SEKAI", "title": "PJSK Booster BOX",
            "heat_score": 0.8, "reason": "named a specific booster box",
        }),
    )
    assert signal is not None
    assert signal.actionability == "informational"  # evidence, never auto-recommended here
    assert signal.anchor_types == (ANCHOR_SNS_CATALYST,)
    assert signal.source_urls == ("https://x.com/p/1",)

    # Promotion without market validation must block, not recommend.
    no_market = TcgMarketValuationProvider(lambda s: None)
    decision = promote_signal(signal, no_market)
    assert decision.actionability == "blocked"
    assert decision.block_reason == BLOCK_MISSING_MARKET_VALIDATION


# --- Test Plan #3: official/store TCG listing → signal + valid candidate ------

def _official_tcg_candidate() -> OpportunityCandidate:
    # Shaped exactly as OfficialStoreCandidateProvider builds it (issue #8 D2).
    return OpportunityCandidate(
        candidate_id="cand_ua_1",
        game="union_arena",
        product_type="sealed_box",
        title="UNION ARENA 鬼滅の刃 BOX",
        search_query="union arena 鬼滅",
        heat_score=0.75,
        reason="store preorder",
        source_kind="official_store_preorder",
        source_url="https://store.example/x",
        metadata={
            "source_store": "AmiAmi",
            "ip_canonical": "鬼滅の刃",
            "official_price_jpy": 4400,
            "product_code": "UA-KMT-01",
            "source_confidence": 0.9,
        },
    )


def test_official_store_tcg_listing_becomes_signal_and_candidate(tmp_path):
    candidate = _official_tcg_candidate()
    signal = candidate_to_signal(candidate)

    # generic collectible metadata is present and TCG-actionable
    assert signal.collectible_domain == "tcg"
    assert signal.ip_canonical == "鬼滅の刃"
    assert signal.official_code == "UA-KMT-01"
    assert signal.retail_price_jpy == 4400
    assert signal.source_kind == "official_store"
    assert signal.actionability == "actionable"
    assert signal.anchor_types == (ANCHOR_OFFICIAL_STORE_LISTING,)
    assert signal.is_recommendable_domain is True

    # the original candidate is unchanged / still valid (pipeline compatibility)
    assert candidate.product_type == "sealed_box"
    assert candidate.source_kind == "official_store_preorder"

    # persists + round-trips through the store
    store = CollectibleSignalStore(tmp_path / "sig.db")
    store.bootstrap()
    store.upsert_signal(signal)
    fetched = store.get_signal(signal.signal_id)
    assert fetched is not None
    assert fetched.signal_id == signal.signal_id
    assert fetched.created_at == signal.created_at
    assert fetched.retail_price_jpy == 4400


# --- Test Plan #4: non-TCG (shikishi / cd) intelligence-only ------------------

def test_non_tcg_goods_are_intelligence_only(tmp_path):
    store = CollectibleSignalStore(tmp_path / "sig.db")
    store.bootstrap()

    shikishi = make_signal(
        source_kind="official_store", collectible_domain="goods",
        ip_canonical="Some IP", title="描き下ろし色紙", entity_kind="character",
        product_type="shikishi", retail_price_jpy=2200, confidence=0.8,
        actionability="informational",
    )
    cd = make_signal(
        source_kind="marketplace", collectible_domain="music",
        ip_canonical="Ado", title="新譜 初回限定盤", entity_kind="artist",
        product_type="cd", retail_price_jpy=3300, confidence=0.7,
        actionability="informational",
    )
    store.upsert_signal(shikishi)
    store.upsert_signal(cd)

    # representable without being forced into TCG fields
    assert store.get_signal(shikishi.signal_id).product_type == "shikishi"
    assert store.get_signal(cd.signal_id).product_type == "cd"
    assert shikishi.is_recommendable_domain is False
    assert cd.is_recommendable_domain is False

    # promotion gate refuses to auto-recommend non-TCG in V1
    prov = TcgMarketValuationProvider(
        lambda s: MarketValuation(fair_value_jpy=9999, confidence=0.9)
    )
    for sig in (shikishi, cd):
        decision = promote_signal(sig, prov)
        assert decision.actionability == "blocked"
        assert decision.block_reason == BLOCK_UNSUPPORTED_DOMAIN

    goods = store.list_signals(collectible_domain="goods")
    assert all(s.collectible_domain == "goods" for s in goods)


# --- Test Plan #5: Mercari item-page cache stays out of the digest ------------

def test_mercari_item_cache_not_in_rag_digest(tmp_path):
    from openclaw_adapter.knowledge_db import KnowledgeDatabase
    from openclaw_adapter.rag_daily_digest import RagDailyDigestScheduler

    db_path = tmp_path / "knowledge.sqlite3"
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="mercari:m123",
        entity_type="product",
        summary="Mercari 商品頁資料：ヴァイス 色紙。 標示價格 ¥9,999。",
        source_urls=("https://jp.mercari.com/item/m123",),
        confidence=0.85,
        origin="research_command",
    )
    sent: list = []
    sched = RagDailyDigestScheduler(
        db_path=db_path, chat_ids=("123",),
        send_fn=lambda chat_id, text, markup: sent.append(text),
    )
    sched._send_digest()
    assert sent == []


def test_rag_digest_splits_durable_and_product_intelligence(tmp_path):
    from openclaw_adapter.knowledge_db import KnowledgeDatabase
    from openclaw_adapter.rag_daily_digest import (
        RagDailyDigestScheduler,
        _SECTION_DURABLE,
        _SECTION_PRODUCT_INTEL,
    )

    db_path = tmp_path / "knowledge.sqlite3"
    db = KnowledgeDatabase(db_path)
    db.upsert_entry(
        entity_canonical="union_arena", entity_type="tcg",
        summary="UNION ARENA。Bandai 旗下 TCG。", confidence=0.7, origin="web_research",
    )
    db.upsert_entry(
        entity_canonical="鬼滅 BOX 行情", entity_type="product",
        summary="鬼滅 BOX 二手約 ¥6000、定価 ¥4400。", confidence=0.7,
        origin="research_command",
    )
    sent: list = []
    sched = RagDailyDigestScheduler(
        db_path=db_path, chat_ids=("123",),
        send_fn=lambda chat_id, text, markup: sent.append(text),
    )
    sched._send_digest()
    joined = "\n".join(sent)
    assert _SECTION_DURABLE in joined
    assert _SECTION_PRODUCT_INTEL in joined
    durable_msg = next(m for m in sent if _SECTION_DURABLE in m)
    intel_msg = next(m for m in sent if _SECTION_PRODUCT_INTEL in m)
    assert "UNION ARENA" in durable_msg
    assert "鬼滅" in intel_msg


# --- Test Plan #6: integration official TCG preorder → recommendation --------

def test_integration_official_tcg_preorder_to_recommendation(tmp_path):
    store = CollectibleSignalStore(tmp_path / "sig.db")
    store.bootstrap()

    # 1. official store preorder candidate (existing pipeline shape)
    candidate = _official_tcg_candidate()
    # 2. → collectible signal
    signal = candidate_to_signal(candidate)
    store.upsert_signal(signal)
    assert signal.actionability == "actionable"

    # 3. → market validation via existing TCG fair-value (injected)
    def fair_value(sig) -> MarketValuation:
        # secondary market ¥6000 vs ¥4400 retail → strong, confident
        return MarketValuation(fair_value_jpy=6000, confidence=0.8, sample_count=15)

    provider = TcgMarketValuationProvider(fair_value)
    decision = promote_signal(signal, provider)

    # 4. → promoted to recommendation-eligible with diagnostics
    assert decision.actionability == "actionable"
    assert decision.block_reason is None
    assert decision.valuation.fair_value_jpy == 6000
    assert decision.signal.metadata["promotion"]["fair_value_jpy"] == 6000


# --- Diagnostics: every block reason is reachable -----------------------------

def test_promotion_diagnostics_cover_block_reasons():
    def tcg(**kw):
        base = dict(source_kind="official_store", collectible_domain="tcg",
                    ip_canonical="X", title="BOX", product_type="sealed_box")
        base.update(kw)
        return make_signal(**base)

    strong = TcgMarketValuationProvider(
        lambda s: MarketValuation(fair_value_jpy=6000, confidence=0.9)
    )
    weak = TcgMarketValuationProvider(
        lambda s: MarketValuation(fair_value_jpy=6000, confidence=0.1)
    )
    none = TcgMarketValuationProvider(lambda s: None)

    # unsupported domain
    music = make_signal(source_kind="sns", collectible_domain="music",
                        ip_canonical="A", product_type="cd")
    assert promote_signal(music, strong).block_reason == BLOCK_UNSUPPORTED_DOMAIN
    # no concrete product
    vague = tcg(product_type="other", official_code=None, title="")
    assert promote_signal(vague, strong).block_reason == BLOCK_NO_CONCRETE_PRODUCT
    # missing market validation
    assert promote_signal(tcg(), none).block_reason == BLOCK_MISSING_MARKET_VALIDATION
    # low price confidence
    assert promote_signal(tcg(), weak).block_reason == BLOCK_LOW_PRICE_CONFIDENCE
