from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from assistant_runtime import AssistantSettings
from tcg_tracker.hot_cards import HotCardBoard, HotCardEntry
from openclaw_adapter.opportunity_agent import (
    ChainedCandidateProvider,
    DEFAULT_WEB_TREND_QUERIES,
    HotCardBoardCandidateProvider,
    ScheduledWebSearchCandidateProvider,
    SnsLlmCandidateProvider,
    SnsPost,
    WebOpportunityResearcher,
    WebResearchCandidateProvider,
    _build_opportunity_research_query,
    _build_sns_candidate_prompt,
    _build_web_trend_candidate_prompt,
    _format_title_with_identifier,
    _parse_candidate_response,
    dismiss_opportunity_target,
    format_opportunity_recommendation,
    format_opportunity_status,
)
from openclaw_adapter.opportunity_sns_discovery import (
    DEFAULT_DISCOVERY_QUERIES,
    discover_tcg_sns_accounts,
)
from openclaw_adapter.opportunity_sns_domain_backfill import backfill_missing_domains
from openclaw_adapter.opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PRODUCT_TYPES,
    PriceCheck,
    ReputationCheck,
    build_candidate_id,
    normalize_product_type,
)
from openclaw_adapter.web_search import WebSearchResult
from openclaw_adapter.opportunity_pipeline import OpportunityPipeline
from openclaw_adapter.opportunity_scoring import OpportunityThresholds, evaluate_opportunity
from openclaw_adapter.opportunity_store import OpportunityStore


def test_evaluate_opportunity_accepts_good_candidate() -> None:
    candidate = _candidate()
    price = PriceCheck(candidate_id=candidate.candidate_id, fair_value_jpy=10000, confidence=0.82, sample_count=6)
    listing = ListingOffer(listing_id="m1", title="Card", price_jpy=8000, url="https://jp.mercari.com/item/m1")
    reputation = ReputationCheck(
        listing_url=listing.url,
        trusted=True,
        total_reviews=120,
        positive_rate=99.0,
        proof_url="http://127.0.0.1:5055/p/proof_1",
        reason="Seller reputation passed.",
    )

    decision = evaluate_opportunity(
        candidate=candidate,
        price=price,
        listing=listing,
        reputation=reputation,
        thresholds=OpportunityThresholds(),
    )

    assert decision.accepted is True
    assert decision.discount_pct == 20.0
    assert decision.score > 70


def test_pipeline_records_and_notifies_recommendation(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opportunities.sqlite3")
    store.bootstrap()
    candidate = _candidate()
    listing = ListingOffer(
        listing_id="m111",
        title="Umbreon SAR",
        price_jpy=8000,
        url="https://jp.mercari.com/item/m111",
    )
    notifier = _FakeNotifier()

    pipeline = OpportunityPipeline(
        store=store,
        candidate_provider=_FakeCandidateProvider([candidate]),
        price_checker=_FakePriceChecker(
            PriceCheck(candidate_id=candidate.candidate_id, fair_value_jpy=10000, confidence=0.9, sample_count=8)
        ),
        listing_finder=_FakeListingFinder([listing]),
        reputation_checker=_FakeReputationChecker(
            ReputationCheck(
                listing_url=listing.url,
                trusted=True,
                proof_url="http://127.0.0.1:5055/p/proof_1",
                total_reviews=240,
                positive_rate=99.6,
                reason="Seller reputation passed.",
            )
        ),
        notifier=notifier,
        thresholds=OpportunityThresholds(),
        candidate_check_interval_seconds=0,
    )

    stats = pipeline.run_once()

    assert stats.discovered == 1
    assert stats.candidates_checked == 1
    assert stats.recommendations_sent == 1
    assert len(notifier.sent) == 1

    rows = store.list_recent_recommendations()
    assert len(rows) == 1
    assert rows[0]["accepted"] == 1
    assert rows[0]["notified_at"] is not None


def test_pipeline_skips_seen_listing(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "opportunities.sqlite3")
    store.bootstrap()
    candidate = _candidate()
    listing = ListingOffer(
        listing_id="m222",
        title="Umbreon SAR",
        price_jpy=8000,
        url="https://jp.mercari.com/item/m222",
    )
    pipeline = OpportunityPipeline(
        store=store,
        candidate_provider=_FakeCandidateProvider([candidate]),
        price_checker=_FakePriceChecker(
            PriceCheck(candidate_id=candidate.candidate_id, fair_value_jpy=10000, confidence=0.9)
        ),
        listing_finder=_FakeListingFinder([listing]),
        reputation_checker=_FakeReputationChecker(
            ReputationCheck(
                listing_url=listing.url,
                trusted=True,
                total_reviews=100,
                positive_rate=99.0,
                reason="Seller reputation passed.",
            )
        ),
        notifier=_FakeNotifier(),
        thresholds=OpportunityThresholds(),
        candidate_check_interval_seconds=0,
    )

    assert pipeline.run_once().recommendations_sent == 1
    assert pipeline.run_once().skipped_seen_listings == 1


def test_dismiss_opportunity_target_by_status_index(tmp_path: Path) -> None:
    db_path = tmp_path / "opportunities.sqlite3"
    store = OpportunityStore(db_path)
    store.bootstrap()
    first = _candidate(title="Umbreon ex SAR", heat_score=91)
    second = _candidate(title="Pikachu promo", heat_score=84)
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    settings = AssistantSettings(opportunity_db_path=str(db_path))

    reply = dismiss_opportunity_target(settings, "2")

    assert "已從機會清單移除" in reply
    assert "Pikachu promo" in reply
    active_titles = [row["title"] for row in store.list_recent_candidates(limit=10)]
    assert active_titles == ["Umbreon ex SAR"]


def test_dismissed_opportunity_target_stays_hidden_after_upsert(tmp_path: Path) -> None:
    db_path = tmp_path / "opportunities.sqlite3"
    store = OpportunityStore(db_path)
    store.bootstrap()
    candidate = _candidate(title="Umbreon ex SAR", heat_score=91)
    store.upsert_candidate(candidate)
    settings = AssistantSettings(opportunity_db_path=str(db_path))

    reply = dismiss_opportunity_target(settings, "Umbreon")
    store.upsert_candidate(candidate)

    assert "Umbreon ex SAR" in reply
    assert store.list_recent_candidates(limit=10) == []


def test_parse_sns_candidate_response_builds_candidates() -> None:
    posts = [
        SnsPost(
            tweet_id="t1",
            author_handle="@source",
            text="Umbreon SAR is getting hot",
            created_at="2026-05-13T00:00:00+00:00",
            rule_label="pokemon",
        )
    ]
    raw = """
    {"candidates":[{"game":"pokemon","title":"Umbreon ex SAR","search_query":"Umbreon ex SAR","heat_score":88,"reason":"Multiple posts mention demand.","source_tweet_ids":["t1"]}]}
    """

    candidates = _parse_candidate_response(raw, posts=posts, limit=5)

    assert len(candidates) == 1
    assert candidates[0].game == "pokemon"
    assert candidates[0].heat_score == 88
    assert candidates[0].metadata["source_tweet_ids"] == ["t1"]


def test_parse_sns_candidate_response_normalizes_real_product_names() -> None:
    posts = [
        SnsPost(
            tweet_id="t1",
            author_handle="@source",
            text="アビスアイ 抽選情報 / アビスアイ収録 ホエルオーex / カスミの元気 Mercari",
            created_at="2026-05-13T00:00:00+00:00",
            rule_label="pokemon",
        )
    ]
    raw = """
    {
      "candidates": [
        {"game":"pokemon","title":"アビスアイ 抽選情報","search_query":"アビスアイ Mercari","heat_score":80,"reason":"抽選情報で話題。","source_tweet_ids":["t1"]},
        {"game":"pokemon","title":"アビスアイ収録 ホエルオーex","search_query":"ホエルオーex Mercari","heat_score":70,"reason":"収録カードが話題。","source_tweet_ids":["t1"]},
        {"game":"pokemon","title":"カスミの元気 Mercari","search_query":"カスミの元気 Mercari","heat_score":65,"reason":"フリマ検索が増加。","source_tweet_ids":["t1"]}
      ]
    }
    """

    candidates = _parse_candidate_response(raw, posts=posts, limit=5)

    assert [candidate.title for candidate in candidates] == ["アビスアイ", "ホエルオーex", "カスミの元気"]
    assert [candidate.search_query for candidate in candidates] == ["アビスアイ", "ホエルオーex", "カスミの元気"]


def test_parse_sns_candidate_response_accepts_yugioh_candidates() -> None:
    posts = [
        SnsPost(
            tweet_id="t1",
            author_handle="@source",
            text="遊☆戯☆王 ORIGINAL ARTWORK COLLECTION(アジア版)",
            created_at="2026-05-13T00:00:00+00:00",
            rule_label="tcg",
        )
    ]
    raw = """
    {"candidates":[{"game":"ygo","title":"遊☆戯☆王 ORIGINAL ARTWORK COLLECTION(アジア版)","search_query":"遊☆戯☆王 ORIGINAL ARTWORK COLLECTION アジア版","heat_score":88,"reason":"話題。","source_tweet_ids":["t1"]}]}
    """

    candidates = _parse_candidate_response(raw, posts=posts, limit=5)

    assert len(candidates) == 1
    assert candidates[0].game == "yugioh"
    assert candidates[0].title == "遊☆戯☆王 ORIGINAL ARTWORK COLLECTION(アジア版)"


def test_parse_sns_candidate_response_rejects_unsupported_franchises() -> None:
    posts = [
        SnsPost(
            tweet_id="t1",
            author_handle="@source",
            text="ONE PIECE CARD GAME 新カード",
            created_at="2026-05-13T00:00:00+00:00",
            rule_label="tcg",
        )
    ]
    raw = """
    {"candidates":[{"game":"pokemon","title":"ONE PIECE CARD GAME 新カード","search_query":"ONE PIECE CARD GAME 新カード","heat_score":88,"reason":"話題。","source_tweet_ids":["t1"]}]}
    """

    candidates = _parse_candidate_response(raw, posts=posts, limit=5)

    assert candidates == []


def test_web_opportunity_researcher_enriches_candidate_with_sources() -> None:
    candidate = _candidate()
    sources = (
        WebSearchResult(
            title="Pikachu promo demand jumps",
            url="https://example.com/pikachu-demand",
            snippet="Collectors discuss demand and higher resale prices.",
        ),
    )
    calls: dict[str, object] = {}

    def search(query: str, limit: int) -> tuple[WebSearchResult, ...]:
        calls["search"] = (query, limit)
        return sources

    def json_call(**kwargs) -> str:
        calls["prompt"] = kwargs["prompt"]
        return '{"is_relevant":true,"demand_score":98,"reason":"外部來源提到收藏需求與價格動能。"}'

    enriched = WebOpportunityResearcher(
        endpoint="http://127.0.0.1:11434",
        model="qwen3:4b",
        timeout_seconds=30,
        search_fn=search,
        json_call_fn=json_call,
    ).enrich(candidate)

    assert calls["search"] == (
        "Umbreon ex SAR Pokemon card demand popularity price trend resale",
        3,
    )
    assert "Umbreon ex SAR" in str(calls["prompt"])
    assert enriched.heat_score > candidate.heat_score
    assert enriched.source_kind == "sns+web"
    assert "網路佐證：外部來源提到收藏需求與價格動能。" in enriched.reason
    assert "Traditional Chinese as used in Taiwan" in str(calls["prompt"])
    web_research = enriched.metadata["web_research"]
    assert web_research["assessment"]["demand_score"] == 98.0
    assert web_research["sources"][0]["url"] == "https://example.com/pikachu-demand"


def test_web_opportunity_researcher_keeps_candidate_when_search_finds_nothing() -> None:
    candidate = _candidate()

    enriched = WebOpportunityResearcher(
        endpoint="http://127.0.0.1:11434",
        model="qwen3:4b",
        timeout_seconds=30,
        search_fn=lambda query, limit: (),
        json_call_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    ).enrich(candidate)

    assert enriched == candidate


def test_web_opportunity_researcher_lowers_heat_when_sources_are_irrelevant() -> None:
    candidate = _candidate()
    sources = (
        WebSearchResult(
            title="Unrelated sports card article",
            url="https://example.com/sports",
            snippet="Not about the requested TCG product.",
        ),
    )

    enriched = WebOpportunityResearcher(
        endpoint="http://127.0.0.1:11434",
        model="qwen3:4b",
        timeout_seconds=30,
        search_fn=lambda query, limit: sources,
        json_call_fn=lambda **kwargs: '{"is_relevant":false,"demand_score":10,"reason":"搜尋結果指向錯誤商品。"}',
    ).enrich(candidate)

    assert enriched.heat_score == candidate.heat_score - 15
    assert enriched.metadata["web_research"]["assessment"]["is_relevant"] is False
    assert "搜尋結果指向錯誤商品" in enriched.reason


def test_web_research_candidate_provider_preserves_candidate_when_enrichment_fails() -> None:
    candidate = _candidate()

    class FailingResearcher:
        def enrich(self, candidate: OpportunityCandidate) -> OpportunityCandidate:
            raise RuntimeError("search unavailable")

    provider = WebResearchCandidateProvider(
        base_provider=_FakeCandidateProvider([candidate]),
        researcher=FailingResearcher(),
    )

    assert provider.discover(limit=5) == (candidate,)


def test_build_opportunity_research_query_includes_market_context() -> None:
    query = _build_opportunity_research_query(_candidate())

    assert "Umbreon ex SAR" in query
    assert "Pokemon card" in query
    assert "demand" in query
    assert "resale" in query


def test_format_opportunity_recommendation_contains_key_fields() -> None:
    candidate = _candidate()
    price = PriceCheck(candidate_id=candidate.candidate_id, fair_value_jpy=10000, confidence=0.9)
    listing = ListingOffer(
        listing_id="m333",
        title="Umbreon SAR",
        price_jpy=8000,
        url="https://jp.mercari.com/item/m333",
    )
    reputation = ReputationCheck(
        listing_url=listing.url,
        trusted=True,
        proof_url="http://127.0.0.1:5055/p/proof_1",
        total_reviews=100,
        positive_rate=99.0,
        reason="Seller reputation passed.",
    )
    decision = evaluate_opportunity(
        candidate=candidate,
        price=price,
        listing=listing,
        reputation=reputation,
        thresholds=OpportunityThresholds(),
    )

    text = format_opportunity_recommendation(
        recommendation=OpportunityRecommendation(
            recommendation_id="listing_1",
            candidate=candidate,
            price=price,
            listing=listing,
            reputation=reputation,
            discount_pct=decision.discount_pct,
            score=decision.score,
            reasons=decision.reasons,
        )
    )

    assert "Umbreon ex SAR" in text
    assert "¥10,000" in text
    assert "https://jp.mercari.com/item/m333" in text
    assert "http://127.0.0.1:5055/p/proof_1" in text


def test_format_opportunity_recommendation_includes_web_research_sources() -> None:
    candidate = OpportunityCandidate(
        candidate_id=build_candidate_id(
            game="pokemon",
            product_type="single_card",
            title="Umbreon ex SAR",
            search_query="Umbreon ex SAR",
        ),
        game="pokemon",
        product_type="single_card",
        title="Umbreon ex SAR",
        search_query="Umbreon ex SAR",
        heat_score=94.0,
        reason="SNS 需求升溫。網路佐證：看得到收藏需求。",
        metadata={
            "web_research": {
                "sources": [
                    {"title": "Demand source", "url": "https://example.com/demand", "snippet": "Demand."},
                    {"title": "Price source", "url": "https://example.com/price", "snippet": "Price."},
                ]
            }
        },
    )
    price = PriceCheck(candidate_id=candidate.candidate_id, fair_value_jpy=10000, confidence=0.9)
    listing = ListingOffer(
        listing_id="m333",
        title="Umbreon SAR",
        price_jpy=8000,
        url="https://jp.mercari.com/item/m333",
    )
    reputation = ReputationCheck(
        listing_url=listing.url,
        trusted=True,
        proof_url="http://127.0.0.1:5055/p/proof_1",
        total_reviews=100,
        positive_rate=99.0,
        reason="Seller reputation passed.",
    )
    decision = evaluate_opportunity(
        candidate=candidate,
        price=price,
        listing=listing,
        reputation=reputation,
        thresholds=OpportunityThresholds(),
    )

    text = format_opportunity_recommendation(
        recommendation=OpportunityRecommendation(
            recommendation_id="listing_1",
            candidate=candidate,
            price=price,
            listing=listing,
            reputation=reputation,
            discount_pct=decision.discount_pct,
            score=decision.score,
            reasons=decision.reasons,
        )
    )

    assert "市場佐證：" in text
    assert "[1] Demand source" in text
    assert "https://example.com/demand" in text
    assert "[2] Price source" in text


def _candidate(
    title: str = "Umbreon ex SAR",
    heat_score: float = 91.0,
    *,
    product_type: str = "single_card",
    product_identifier: str | None = None,
) -> OpportunityCandidate:
    return OpportunityCandidate(
        candidate_id=build_candidate_id(
            game="pokemon",
            product_type=product_type,
            title=title,
            search_query=title,
            product_identifier=product_identifier,
        ),
        game="pokemon",
        product_type=product_type,
        title=title,
        product_identifier=product_identifier,
        search_query=title,
        heat_score=heat_score,
        reason="SNS demand is rising.",
    )


class _FakeCandidateProvider:
    def __init__(self, candidates: list[OpportunityCandidate]) -> None:
        self._candidates = candidates

    def discover(self, *, limit: int) -> list[OpportunityCandidate]:
        return self._candidates[:limit]


class _FakePriceChecker:
    def __init__(self, price: PriceCheck) -> None:
        self._price = price

    def check(self, candidate: OpportunityCandidate) -> PriceCheck:
        return self._price


class _FakeListingFinder:
    def __init__(self, listings: list[ListingOffer]) -> None:
        self._listings = listings

    def find(self, candidate: OpportunityCandidate, *, price_max_jpy: int, limit: int) -> list[ListingOffer]:
        return [listing for listing in self._listings if listing.price_jpy <= price_max_jpy][:limit]


class _FakeReputationChecker:
    def __init__(self, reputation: ReputationCheck) -> None:
        self._reputation = reputation

    def check(self, listing: ListingOffer) -> ReputationCheck:
        return self._reputation


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent = []

    def notify(self, recommendation) -> None:
        self.sent.append(recommendation)


# ─── Three-level candidate structure (game / product_type / title) ───────────


def test_parse_sns_candidate_response_extracts_three_level_structure() -> None:
    posts = [
        SnsPost(
            tweet_id="t1",
            author_handle="@source",
            text="スタートデッキ100 予約情報",
            created_at="2026-05-16T00:00:00+00:00",
            rule_label="pokemon",
        )
    ]
    raw = """
    {"candidates":[{"game":"pokemon","product_type":"starter_deck","title":"スタートデッキ100","product_identifier":null,"search_query":"スタートデッキ100","heat_score":70,"reason":"予約","source_tweet_ids":["t1"]}]}
    """

    candidates = _parse_candidate_response(raw, posts=posts, limit=5)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.game == "pokemon"
    assert candidate.product_type == "starter_deck"
    assert candidate.title == "スタートデッキ100"
    assert candidate.product_identifier is None


def test_parse_sns_candidate_response_splits_multi_product_into_separate_candidates() -> None:
    # The previously merged "インフェルノX・スタートデッキ100" bug case — once
    # the LLM correctly emits two candidates with different product_types,
    # the parser must keep them separate (different candidate_ids).
    posts = [
        SnsPost(
            tweet_id="t1",
            author_handle="@source",
            text="インフェルノX・スタートデッキ100 抽選情報",
            created_at="2026-05-16T00:00:00+00:00",
            rule_label="pokemon",
        )
    ]
    raw = """
    {
      "candidates": [
        {"game":"pokemon","product_type":"sealed_box","title":"インフェルノX","product_identifier":null,"search_query":"インフェルノX","heat_score":70,"reason":"再販情報","source_tweet_ids":["t1"]},
        {"game":"pokemon","product_type":"starter_deck","title":"スタートデッキ100","product_identifier":null,"search_query":"スタートデッキ100","heat_score":68,"reason":"予約情報","source_tweet_ids":["t1"]}
      ]
    }
    """

    candidates = _parse_candidate_response(raw, posts=posts, limit=5)

    assert len(candidates) == 2
    assert {c.title for c in candidates} == {"インフェルノX", "スタートデッキ100"}
    assert {c.product_type for c in candidates} == {"sealed_box", "starter_deck"}
    assert candidates[0].candidate_id != candidates[1].candidate_id


def test_normalize_product_type_maps_aliases() -> None:
    assert normalize_product_type("single_card") == "single_card"
    assert normalize_product_type("trading_card") == "single_card"
    assert normalize_product_type("box") == "sealed_box"
    assert normalize_product_type("Display") == "sealed_box"
    assert normalize_product_type("deck") == "starter_deck"
    assert normalize_product_type("structure-deck") == "starter_deck"
    assert normalize_product_type("Booster") == "booster_pack"
    assert normalize_product_type("random_nonsense") == "other"
    assert normalize_product_type(None) == "other"
    assert normalize_product_type("") == "other"
    # Every alias output should be a real enum value.
    for alias_input in ("card", "trading_card", "pack", "Box", "Display", "Trial Deck", "promotional"):
        assert normalize_product_type(alias_input) in PRODUCT_TYPES


def test_build_candidate_id_differs_when_product_type_differs() -> None:
    common = dict(game="pokemon", title="ピカチュウex", search_query="ピカチュウex")
    single = build_candidate_id(**common, product_type="single_card")
    sealed = build_candidate_id(**common, product_type="sealed_box")
    assert single != sealed


def test_opportunity_status_renders_product_type_layer(tmp_path: Path) -> None:
    settings = AssistantSettings(opportunity_db_path=str(tmp_path / "hunt.sqlite3"))
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    store.upsert_candidate(
        OpportunityCandidate(
            candidate_id=build_candidate_id(
                game="pokemon",
                product_type="single_card",
                title="ピカチュウex SAR",
                search_query="ピカチュウex 201/165 SAR",
                product_identifier="201/165",
            ),
            game="pokemon",
            product_type="single_card",
            title="ピカチュウex SAR",
            product_identifier="201/165",
            search_query="ピカチュウex 201/165 SAR",
            heat_score=85.0,
            reason="收藏熱度上升",
        )
    )
    store.upsert_candidate(
        OpportunityCandidate(
            candidate_id=build_candidate_id(
                game="pokemon",
                product_type="starter_deck",
                title="スタートデッキ100",
                search_query="スタートデッキ100",
            ),
            game="pokemon",
            product_type="starter_deck",
            title="スタートデッキ100",
            search_query="スタートデッキ100",
            heat_score=70.0,
            reason="再販情報",
        )
    )

    text = format_opportunity_status(settings)

    # Three-level header on each row.
    assert "[pokemon / single_card]" in text
    assert "[pokemon / starter_deck]" in text
    # Single card shows identifier in parentheses.
    assert "ピカチュウex SAR (201/165)" in text


def test_format_title_with_identifier_uses_product_type_aware_braces() -> None:
    assert _format_title_with_identifier(title="X", product_type="single_card", identifier="201/165") == "X (201/165)"
    assert _format_title_with_identifier(title="X", product_type="sealed_box", identifier="sv-p") == "X [sv-p]"
    assert _format_title_with_identifier(title="X", product_type="booster_pack", identifier="sv-p") == "X [sv-p]"
    assert _format_title_with_identifier(title="X", product_type="other", identifier="sv-p") == "X"
    assert _format_title_with_identifier(title="X", product_type="single_card", identifier=None) == "X"


def test_sns_candidate_prompt_explains_three_level_structure() -> None:
    prompt = _build_sns_candidate_prompt(
        posts=[
            SnsPost(
                tweet_id="t1",
                author_handle="@source",
                text="dummy",
                created_at="2026-05-16T00:00:00+00:00",
                rule_label="pokemon",
            )
        ],
        limit=3,
    )

    # All six enum values must appear so the LLM knows the constrained set.
    for product_type in PRODUCT_TYPES:
        assert product_type in prompt
    # Rule wording exists.
    assert "拆成多個 candidate" in prompt
    # Both the "split" example and the "keep" example are present.
    assert "インフェルノX・スタートデッキ100" in prompt
    assert "ピカチュウ・カビゴンex" in prompt


def test_opportunity_store_migrates_legacy_schema_by_drop_rebuild(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "hunt.sqlite3"
    legacy_schema = """
    CREATE TABLE opportunity_candidates (
        candidate_id TEXT PRIMARY KEY,
        game TEXT NOT NULL,
        title TEXT NOT NULL,
        search_query TEXT NOT NULL,
        heat_score REAL NOT NULL,
        reason TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_url TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'active',
        last_checked_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """
    with sqlite3.connect(db_path) as connection:
        connection.executescript(legacy_schema)
        connection.execute(
            "INSERT INTO opportunity_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_id",
                "pokemon",
                "legacy title",
                "legacy search",
                10.0,
                "legacy reason",
                "sns",
                "",
                "{}",
                "active",
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()

    store = OpportunityStore(db_path)
    store.bootstrap()

    with sqlite3.connect(db_path) as connection:
        rows = list(connection.execute("SELECT * FROM opportunity_candidates"))
        columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunity_candidates)")}
    assert rows == []  # legacy row was dropped during migration
    assert "product_type" in columns
    assert "product_identifier" in columns


# ─── Provider A / B / chain + SNS domain filter & auto-discovery ─────────────


class _FakeHotCardService:
    def __init__(self, boards):
        self._boards = tuple(boards)

    def load_boards(self, *, limit):  # noqa: ARG002 — sig mirrors real service
        return self._boards


def _hot_entry(*, rank, title, card_number, hot_score, rarity="SAR", set_code="sv08"):
    return HotCardEntry(
        game="pokemon",
        rank=rank,
        title=title,
        price_jpy=10000,
        thumbnail_url="",
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
        listing_count=5,
        best_ask_jpy=10000,
        best_bid_jpy=8000,
        previous_bid_jpy=8000,
        bid_ask_ratio=0.8,
        buy_support_score=80.0,
        momentum_boost_score=5.0,
        buy_signal_label=None,
        hot_score=hot_score,
        attention_score=40.0,
        social_post_count=2,
        social_engagement_count=50,
        notes=(),
        is_graded=False,
        references=(),
    )


def _hot_board(items):
    return HotCardBoard(
        game="pokemon",
        label="Pokemon Liquidity Board",
        methodology="stub",
        generated_at=datetime.now(timezone.utc),
        items=tuple(items),
    )


def test_hot_card_board_provider_emits_single_card_candidates() -> None:
    service = _FakeHotCardService([
        _hot_board([
            _hot_entry(rank=1, title="ピカチュウex SAR", card_number="201/165", hot_score=92.0),
            _hot_entry(rank=2, title="リザードンex SAR", card_number="195/165", hot_score=88.0),
        ])
    ])
    provider = HotCardBoardCandidateProvider(hot_card_service=service, per_game_limit=2, min_hot_score=50.0)

    candidates = list(provider.discover(limit=5))

    assert len(candidates) == 2
    assert all(c.product_type == "single_card" for c in candidates)
    assert {c.product_identifier for c in candidates} == {"201/165", "195/165"}
    assert all(c.source_kind == "hot_card_board" for c in candidates)


def test_hot_card_board_provider_filters_below_min_score() -> None:
    service = _FakeHotCardService([
        _hot_board([
            _hot_entry(rank=1, title="A", card_number="100/165", hot_score=30.0),
            _hot_entry(rank=2, title="B", card_number="101/165", hot_score=65.0),
            _hot_entry(rank=3, title="C", card_number="102/165", hot_score=80.0),
        ])
    ])
    provider = HotCardBoardCandidateProvider(hot_card_service=service, per_game_limit=5, min_hot_score=60.0)

    titles = [c.title for c in provider.discover(limit=10)]

    assert titles == ["B", "C"]


def test_scheduled_web_search_provider_runs_each_query_and_extracts_candidates() -> None:
    from openclaw_adapter.web_search import WebSearchResult

    seen_queries: list[str] = []

    def fake_search(query, *, max_results):  # noqa: ARG001
        seen_queries.append(query)
        return (WebSearchResult(title=f"{query} 結果", url=f"https://example.com/{len(seen_queries)}", snippet="snippet"),)

    def fake_llm(prompt):  # noqa: ARG001
        return (
            '{"candidates":[{"game":"pokemon","product_type":"sealed_box","title":"インフェルノX",'
            '"product_identifier":null,"search_query":"インフェルノX","heat_score":70,"reason":"web",'
            '"source_tweet_ids":["https://example.com/1"]}]}'
        )

    provider = ScheduledWebSearchCandidateProvider(
        search_fn=fake_search,
        llm_fn=fake_llm,
        queries=("q1", "q2", "q3"),
        results_per_query=1,
    )
    candidates = list(provider.discover(limit=5))

    assert seen_queries == ["q1", "q2", "q3"]
    assert len(candidates) == 1
    assert candidates[0].product_type == "sealed_box"
    assert candidates[0].title == "インフェルノX"
    assert candidates[0].source_kind == "web_trend_search"


def test_chained_candidate_provider_dedupes_by_candidate_id() -> None:
    from openclaw_adapter.opportunity_models import build_candidate_id

    spec = dict(game="pokemon", product_type="single_card", title="Same", search_query="Same")
    shared_id = build_candidate_id(**spec)
    shared = OpportunityCandidate(
        candidate_id=shared_id,
        game="pokemon",
        product_type="single_card",
        title="Same",
        product_identifier=None,
        search_query="Same",
        heat_score=70.0,
        reason="provider A",
    )
    duplicate_with_higher_heat = OpportunityCandidate(
        candidate_id=shared_id,
        game="pokemon",
        product_type="single_card",
        title="Same",
        product_identifier=None,
        search_query="Same",
        heat_score=90.0,
        reason="provider B",
    )

    class _StaticProvider:
        def __init__(self, items):
            self._items = items

        def discover(self, *, limit):  # noqa: ARG002
            return tuple(self._items)

    chained = ChainedCandidateProvider([
        _StaticProvider([shared]),
        _StaticProvider([duplicate_with_higher_heat]),
    ])
    candidates = list(chained.discover(limit=10))

    assert len(candidates) == 1
    assert candidates[0].heat_score == 90.0  # higher heat wins


def test_sns_provider_filters_by_tcg_domain_intersection(tmp_path: Path) -> None:
    """End-to-end: seed three rules (Trump=politic, Laurier=pokemon, untagged),
    seed corresponding tweets, and assert _read_recent_posts only yields the
    pokemon-tagged author's tweets.
    """
    import sqlite3
    from sns_monitor.storage import SnsDatabase
    from sns_monitor.models import AccountWatch

    db_path = tmp_path / "sns.sqlite3"
    sns_db = SnsDatabase(db_path)
    sns_db.bootstrap()

    sns_db.save_watch_rule(AccountWatch(
        rule_id="r-trump", screen_name="realDonaldTrump", user_id=None,
        label="@realDonaldTrump", include_keywords=(),
        domains=("politic", "stock"), enabled=True, schedule_minutes=15, chat_id="",
        last_checked_at=None,
    ))
    sns_db.save_watch_rule(AccountWatch(
        rule_id="r-laurier", screen_name="Laurier_News", user_id=None,
        label="@Laurier_News", include_keywords=(),
        domains=("pokemon", "yugioh"), enabled=True, schedule_minutes=15, chat_id="",
        last_checked_at=None,
    ))
    sns_db.save_watch_rule(AccountWatch(
        rule_id="r-untagged", screen_name="aka_claw", user_id=None,
        label="@aka_claw", include_keywords=(),
        domains=(), enabled=True, schedule_minutes=15, chat_id="",
        last_checked_at=None,
    ))

    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as connection:
        for (tid, rule_id, handle, text) in (
            ("t1", "r-trump", "realDonaldTrump", "America First"),
            ("t2", "r-laurier", "Laurier_News", "Pokemon TCG 新弾 情報"),
            ("t3", "r-untagged", "aka_claw", "no domain tag"),
        ):
            connection.execute(
                "INSERT INTO seen_tweets (tweet_id, rule_id, author_handle, text, created_at, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tid, rule_id, handle, text, now_iso, now_iso),
            )
        connection.commit()

    provider = SnsLlmCandidateProvider(
        db_path=db_path,
        endpoint="http://stub",
        model="",  # avoid actual LLM call
        timeout_seconds=1,
        lookback_hours=24,
    )
    posts = provider._read_recent_posts(limit=10)

    assert [p.author_handle for p in posts] == ["Laurier_News"]


def test_account_watch_supports_domains_field_round_trip(tmp_path: Path) -> None:
    from sns_monitor.models import AccountWatch
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule = AccountWatch(
        rule_id="abc",
        screen_name="Laurier_News",
        user_id=None,
        label="@Laurier_News",
        include_keywords=("抽選",),
        domains=("pokemon", "yugioh"),
        enabled=True,
        schedule_minutes=15,
        chat_id="123",
        last_checked_at=None,
    )
    db.save_watch_rule(rule)
    loaded = db.get_watch_rule("abc")
    assert isinstance(loaded, AccountWatch)
    assert loaded.include_keywords == ("抽選",)
    assert loaded.domains == ("pokemon", "yugioh")


def test_snsadd_parses_filter_bracket_and_domain_bracket() -> None:
    from sns_monitor.filters import parse_account_watch_text

    assert parse_account_watch_text("@Laurier_News filter[抽選, 予約] domain[pokemon, ws]") == (
        "Laurier_News",
        ("抽選", "予約"),
        ("pokemon", "ws"),
    )


def test_snsadd_keeps_legacy_json_array_filter_compat() -> None:
    from sns_monitor.filters import parse_account_watch_text

    handle, filters, domains = parse_account_watch_text('@elonmusk ["buy", "sell"]')
    assert handle == "elonmusk"
    assert filters == ("buy", "sell")
    assert domains is None  # caller preserves existing rule's domains


def test_domain_backfill_uses_llm_and_saves_one_per_run(tmp_path: Path, monkeypatch) -> None:
    from sns_monitor.models import AccountWatch, KeywordWatch
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()

    # Two enabled, no-domain rules → only the first should be processed.
    db.save_watch_rule(AccountWatch(
        rule_id="r1", screen_name="alpha", user_id=None, label="@alpha",
        include_keywords=(), domains=(),
        enabled=True, schedule_minutes=15, chat_id="", last_checked_at=None,
    ))
    db.save_watch_rule(KeywordWatch(
        rule_id="r2", query="機動戦士", label="機動戦士",
        domains=(),
        enabled=True, schedule_minutes=30, chat_id="", last_checked_at=None,
    ))

    llm_calls = []

    def fake_llm(prompt):
        llm_calls.append(prompt)
        return '{"domains":["pokemon"],"reason":"頻繁提到 pokemon"}'

    notifications: list[str] = []
    updated = backfill_missing_domains(
        sns_db=db,
        sns_db_path=tmp_path / "sns.sqlite3",
        llm_fn=fake_llm,
        telegram_notify_fn=notifications.append,
        limit=1,
    )

    assert len(updated) == 1
    assert updated[0].domains == ("pokemon",)
    assert len(llm_calls) == 1
    assert notifications and "自動標記" in notifications[0]

    # Second rule still has no domain
    remaining = db.list_watch_rules_missing_domains()
    assert len(remaining) == 1


def test_sns_account_auto_discovery_extracts_handles_from_search_urls(tmp_path: Path) -> None:
    """Regex must pull genuine handles and skip protected paths."""
    from openclaw_adapter.opportunity_sns_discovery import _HANDLE_RE

    assert _HANDLE_RE.findall("https://twitter.com/pokemon_cojp/status/12345") == ["pokemon_cojp"]
    assert _HANDLE_RE.findall("https://x.com/foo_bar123") == ["foo_bar123"]
    # Protected paths must NOT yield handles
    assert _HANDLE_RE.findall("https://twitter.com/i/status/12345") == []
    assert _HANDLE_RE.findall("https://twitter.com/search?q=pokemon") == []
    assert _HANDLE_RE.findall("https://twitter.com/hashtag/PokemonCard") == []


def test_sns_account_auto_discovery_respects_confidence_cap_and_tcg_intersection(tmp_path: Path) -> None:
    """Cap=2, only high-confidence + TCG-domain candidates get added."""
    from openclaw_adapter.web_search import WebSearchResult
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()

    # 5 search URLs → 5 candidate handles
    fake_results = [
        WebSearchResult(title=f"r{i}", url=f"https://twitter.com/handle{i}", snippet="")
        for i in range(1, 6)
    ]

    def fake_search(query, *, max_results):  # noqa: ARG001
        return fake_results

    verdicts = iter([
        '{"is_tcg":true,"domains":["pokemon"],"confidence":0.9,"reason":"r1"}',
        '{"is_tcg":true,"domains":["yugioh"],"confidence":0.85,"reason":"r2"}',
        '{"is_tcg":true,"domains":["pokemon"],"confidence":0.5,"reason":"low conf"}',  # below 0.7 floor
        '{"is_tcg":false,"domains":[],"confidence":0.9,"reason":"not tcg"}',
        '{"is_tcg":true,"domains":["politic"],"confidence":0.95,"reason":"wrong domain"}',
    ])

    def fake_llm(prompt):  # noqa: ARG001
        return next(verdicts)

    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=fake_search,
        llm_fn=fake_llm,
        queries=("site:twitter.com test",),
        max_new_per_run=2,
        min_confidence=0.7,
        results_per_query=5,
    )

    assert [r.screen_name for r in added] == ["handle1", "handle2"]


def test_sns_account_auto_discovery_sends_notification_with_domains(tmp_path: Path) -> None:
    from openclaw_adapter.web_search import WebSearchResult
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()

    source_tweet_url = "https://twitter.com/poke_news_jp/status/1234567890"

    def fake_search(query, *, max_results):  # noqa: ARG001
        return (WebSearchResult(title="r", url=source_tweet_url, snippet=""),)

    def fake_llm(prompt):  # noqa: ARG001
        return '{"is_tcg":true,"domains":["pokemon","tcg"],"confidence":0.9,"reason":"covers TCG news"}'

    notifications: list[str] = []
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=fake_search,
        llm_fn=fake_llm,
        telegram_notify_fn=notifications.append,
        queries=("test",),
        max_new_per_run=1,
        min_confidence=0.7,
    )

    assert len(added) == 1
    assert added[0].screen_name == "poke_news_jp"
    msg = notifications[0]
    assert "自動加入追蹤 @poke_news_jp" in msg
    assert "pokemon" in msg
    # Account link the user can click straight to the profile
    assert "帳號：https://x.com/poke_news_jp" in msg
    # The specific URL that triggered the auto-add (only included when distinct
    # from the bare profile link)
    assert f"觸發來源：{source_tweet_url}" in msg


def test_sns_account_auto_discovery_omits_source_when_same_as_profile(tmp_path: Path) -> None:
    """If the only search-result URL is the profile itself there's no extra
    tweet to surface — don't echo the same link twice."""
    from openclaw_adapter.web_search import WebSearchResult
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()

    def fake_search(query, *, max_results):  # noqa: ARG001
        return (WebSearchResult(title="r", url="https://x.com/poke_news_jp", snippet=""),)

    def fake_llm(prompt):  # noqa: ARG001
        return '{"is_tcg":true,"domains":["pokemon"],"confidence":0.9,"reason":"covers TCG news"}'

    notifications: list[str] = []
    discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=fake_search,
        llm_fn=fake_llm,
        telegram_notify_fn=notifications.append,
        queries=("test",),
        max_new_per_run=1,
        min_confidence=0.7,
    )

    assert notifications
    assert "觸發來源" not in notifications[0]
    assert "帳號：https://x.com/poke_news_jp" in notifications[0]
