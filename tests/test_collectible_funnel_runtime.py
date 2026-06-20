"""Issue #8 — runtime wiring of the collectible intelligence funnel.

Unlike test_collectible_intelligence.py (which calls the helpers directly), these
tests drive the *production paths* and assert that signal rows are actually
created at runtime:

- OpportunityPipeline persists official-store candidates as signals (findings 1+2)
- the official path runs the valuation/promote gate and stores the decision
- CollectibleSignalStore.upsert_signal merges evidence instead of overwriting (finding 3)
- the SNS evidence hook routes concrete posts through classify_sns_post (full scope)
- the daily digest reads product intelligence from the signal store (finding 4)
"""
from __future__ import annotations

import json

from openclaw_adapter.collectible_signal import candidate_to_signal, make_signal
from openclaw_adapter.collectible_signal_store import CollectibleSignalStore
from openclaw_adapter.opportunity_models import OpportunityCandidate, PriceCheck
from openclaw_adapter.opportunity_pipeline import OpportunityPipeline, _MutableStats
from openclaw_adapter.opportunity_scoring import OpportunityThresholds
from openclaw_adapter.opportunity_store import OpportunityStore


def _official_candidate(**kw) -> OpportunityCandidate:
    defaults = dict(
        candidate_id="cand_ua_csm",
        game="union_arena",
        product_type="sealed_box",
        title="UNION ARENA チェンソーマン 1BOX",
        search_query="UNION ARENA チェンソーマン",
        heat_score=0.85,
        reason="store preorder",
        source_kind="official_store_preorder",
        source_url="https://store.example/ua-csm",
        metadata={
            "source_store": "joshin",
            "listing_status": "lottery_open",
            "listing_url": "https://store.example/ua-csm",
            "ip_canonical": "チェンソーマン",
            "official_price_jpy": 4180,
            "product_code": "UA-CSM-01",
            "source_confidence": 0.9,
        },
    )
    defaults.update(kw)
    return OpportunityCandidate(**defaults)


class _OneShotProvider:
    def __init__(self, candidate):
        self._candidate = candidate

    def discover(self, *, limit):
        return [self._candidate]


class _MockNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, recommendation):
        self.sent.append(recommendation.candidate.title)


class _NullPriceChecker:
    def check(self, candidate):
        return None


def _pipeline(tmp_path, *, provider, price_checker, signal_store):
    store = OpportunityStore(tmp_path / "opp.sqlite3")
    store.bootstrap()
    return OpportunityPipeline(
        store=store,
        candidate_provider=provider,
        price_checker=price_checker,
        listing_finder=_NullPriceChecker(),  # unused on the official path
        reputation_checker=_NullPriceChecker(),
        notifier=_MockNotifier(),
        thresholds=OpportunityThresholds(),
        signal_store=signal_store,
    )


# --- findings 1+2: official-store ingestion becomes first-class intelligence ---

def test_run_once_persists_official_store_signal(tmp_path):
    candidate = _official_candidate()
    signal_store = CollectibleSignalStore(tmp_path / "sig.sqlite3")
    signal_store.bootstrap()
    pipeline = _pipeline(
        tmp_path,
        provider=_OneShotProvider(candidate),
        price_checker=_NullPriceChecker(),
        signal_store=signal_store,
    )

    pipeline.run_once()

    sid = candidate_to_signal(candidate).signal_id
    stored = signal_store.get_signal(sid)
    assert stored is not None
    assert stored.collectible_domain == "tcg"
    assert stored.ip_canonical == "チェンソーマン"
    assert stored.source_kind == "official_store"


def test_official_path_runs_promote_gate_and_stores_decision(tmp_path):
    candidate = _official_candidate()
    signal_store = CollectibleSignalStore(tmp_path / "sig.sqlite3")
    signal_store.bootstrap()

    class _MarketPriceChecker:
        def check(self, cand):
            # secondary market ¥6000 vs ¥4180 retail → strong, confident
            return PriceCheck(
                candidate_id=cand.candidate_id,
                fair_value_jpy=6000,
                confidence=0.8,
                sample_count=12,
            )

    pipeline = _pipeline(
        tmp_path,
        provider=_OneShotProvider(candidate),
        price_checker=_MarketPriceChecker(),
        signal_store=signal_store,
    )

    stats = _MutableStats()
    pipeline._run_official_store_candidate(candidate, stats)

    sid = candidate_to_signal(candidate).signal_id
    stored = signal_store.get_signal(sid)
    assert stored is not None
    assert stored.actionability == "actionable"
    assert stored.metadata["promotion"]["fair_value_jpy"] == 6000


def test_pipeline_without_signal_store_is_noop(tmp_path):
    # No signal store wired → official path still works, just no intelligence.
    candidate = _official_candidate()
    pipeline = _pipeline(
        tmp_path,
        provider=_OneShotProvider(candidate),
        price_checker=_NullPriceChecker(),
        signal_store=None,
    )
    pipeline.run_once()  # must not raise


# --- finding 3: upsert merges evidence instead of overwriting -----------------

def test_upsert_merges_urls_anchors_and_metadata(tmp_path):
    store = CollectibleSignalStore(tmp_path / "sig.sqlite3")
    store.bootstrap()

    common = dict(
        collectible_domain="tcg", ip_canonical="X", title="BOX",
        product_type="sealed_box", official_code="UA-1",
    )
    a = make_signal(
        source_kind="official_store",
        source_urls=("https://a.jp/1",),
        anchor_types=("official_store_listing",),
        metadata={"k1": "v1"}, confidence=0.5, heat_score=0.3, evidence_count=1,
        **common,
    )
    b = make_signal(
        source_kind="marketplace",
        source_urls=("https://b.jp/2",),
        anchor_types=("marketplace_listing",),
        metadata={"k2": "v2"}, confidence=0.7, heat_score=0.2, evidence_count=1,
        **common,
    )
    assert a.signal_id == b.signal_id  # same identity

    store.upsert_signal(a)
    store.upsert_signal(b)
    merged = store.get_signal(a.signal_id)

    assert set(merged.source_urls) == {"https://a.jp/1", "https://b.jp/2"}
    assert set(merged.anchor_types) == {"official_store_listing", "marketplace_listing"}
    assert merged.metadata["k1"] == "v1"
    assert merged.metadata["k2"] == "v2"
    assert merged.confidence == 0.7          # MAX, weaker echo never demotes
    assert merged.heat_score == 0.3          # MAX
    assert merged.evidence_count == 2        # monotonic, ≥ distinct URLs


# --- full scope: SNS concrete evidence routed through classify_sns_post --------

def test_sns_hook_persists_concrete_signal(tmp_path):
    from openclaw_adapter.sns_tools import _persist_sns_signal

    store = CollectibleSignalStore(tmp_path / "sig.sqlite3")
    store.bootstrap()

    verdict = {
        "is_collectible": True, "confidence": 0.9, "has_concrete_anchor": True,
        "collectible_domain": "tcg", "entity_kind": "set", "product_type": "sealed_box",
        "ip_canonical": "Project SEKAI", "title": "PJSK Booster BOX",
        "heat_score": 0.8, "reason": "named a specific booster box",
    }
    _persist_sns_signal(
        store,
        text="ヴァイス Project SEKAI ブースターBOX 予約開始",
        source_kind="sns",
        source_url="https://x.com/p/1",
        llm_fn=lambda prompt: json.dumps(verdict),
    )
    signals = store.list_signals()
    assert len(signals) == 1
    assert signals[0].ip_canonical == "Project SEKAI"
    assert signals[0].actionability == "informational"
    assert signals[0].source_urls == ("https://x.com/p/1",)


def test_sns_hook_rejects_chatter(tmp_path):
    from openclaw_adapter.sns_tools import _persist_sns_signal

    store = CollectibleSignalStore(tmp_path / "sig.sqlite3")
    store.bootstrap()
    _persist_sns_signal(
        store,
        text="新弾そろそろかな〜楽しみ！",
        source_kind="sns",
        source_url="",
        llm_fn=lambda prompt: json.dumps(
            {"is_collectible": True, "confidence": 0.9, "has_concrete_anchor": False}
        ),
    )
    assert store.list_signals() == []


# --- finding 4: daily digest reads product intel from the signal store ---------

def test_digest_product_intel_reads_from_signal_store(tmp_path):
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
    # A legacy product knowledge entry must NOT appear once the signal store is
    # the product-intel source of truth.
    db.upsert_entry(
        entity_canonical="mercari:legacy", entity_type="product",
        summary="舊式商品頁快取，不該出現。", confidence=0.7, origin="research_command",
    )

    sig_path = tmp_path / "sig.sqlite3"
    sig_store = CollectibleSignalStore(sig_path)
    sig_store.bootstrap()
    sig_store.upsert_signal(make_signal(
        source_kind="official_store", collectible_domain="tcg",
        ip_canonical="鬼滅の刃", title="UNION ARENA 鬼滅の刃 BOX",
        product_type="sealed_box", official_code="UA-KMT-01",
        retail_price_jpy=4400, confidence=0.9, actionability="actionable",
        source_urls=("https://store.example/x",),
    ))

    sent: list = []
    sched = RagDailyDigestScheduler(
        db_path=db_path, chat_ids=("123",),
        send_fn=lambda chat_id, text, markup: sent.append((text, markup)),
        signal_db_path=sig_path,
    )
    sched._send_digest()

    texts = [t for t, _ in sent]
    joined = "\n".join(texts)
    assert _SECTION_DURABLE in joined
    assert _SECTION_PRODUCT_INTEL in joined
    assert "UNION ARENA 鬼滅の刃 BOX" in joined        # signal headline shown
    assert "舊式商品頁快取" not in joined               # legacy product entry suppressed
    # signal messages carry no keep/delete buttons
    intel = next(t for t, m in sent if _SECTION_PRODUCT_INTEL in t)
    intel_markup = next(m for t, m in sent if _SECTION_PRODUCT_INTEL in t)
    assert intel_markup is None
    assert "鬼滅" in intel
