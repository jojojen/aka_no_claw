from __future__ import annotations

from pathlib import Path

from openclaw_adapter.opportunity_agent import (
    SnsPost,
    WebOpportunityResearcher,
    WebResearchCandidateProvider,
    _build_opportunity_research_query,
    _parse_candidate_response,
    format_opportunity_recommendation,
)
from openclaw_adapter.opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    build_candidate_id,
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
        return '{"is_relevant":true,"demand_score":98,"reason":"Outside sources mention collector demand and price movement."}'

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
    assert "Web research: Outside sources mention collector demand" in enriched.reason
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
        json_call_fn=lambda **kwargs: '{"is_relevant":false,"demand_score":10,"reason":"Search results point to the wrong product."}',
    ).enrich(candidate)

    assert enriched.heat_score == candidate.heat_score - 15
    assert enriched.metadata["web_research"]["assessment"]["is_relevant"] is False
    assert "wrong product" in enriched.reason


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
        candidate_id=build_candidate_id(game="pokemon", title="Umbreon ex SAR", search_query="Umbreon ex SAR"),
        game="pokemon",
        title="Umbreon ex SAR",
        search_query="Umbreon ex SAR",
        heat_score=94.0,
        reason="SNS demand is rising. Web research: Collector demand is visible.",
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


def _candidate() -> OpportunityCandidate:
    return OpportunityCandidate(
        candidate_id=build_candidate_id(game="pokemon", title="Umbreon ex SAR", search_query="Umbreon ex SAR"),
        game="pokemon",
        title="Umbreon ex SAR",
        search_query="Umbreon ex SAR",
        heat_score=91.0,
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
