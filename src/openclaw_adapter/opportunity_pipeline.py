from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

from .opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    build_listing_key,
)
from .opportunity_scoring import OpportunityThresholds, evaluate_opportunity, target_price_for
from .opportunity_store import OpportunityStore, recommendation_id_for

logger = logging.getLogger(__name__)


class CandidateProvider(Protocol):
    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        ...


class PriceChecker(Protocol):
    def check(self, candidate: OpportunityCandidate) -> PriceCheck | None:
        ...


class ListingFinder(Protocol):
    def find(self, candidate: OpportunityCandidate, *, price_max_jpy: int, limit: int) -> Sequence[ListingOffer]:
        ...


class ReputationChecker(Protocol):
    def check(self, listing: ListingOffer) -> ReputationCheck:
        ...


class RecommendationNotifier(Protocol):
    def notify(self, recommendation: OpportunityRecommendation) -> None:
        ...


@dataclass(frozen=True, slots=True)
class OpportunityPipelineStats:
    discovered: int = 0
    candidates_checked: int = 0
    price_checks: int = 0
    listings_checked: int = 0
    recommendations_sent: int = 0
    skipped_seen_listings: int = 0
    rejected: int = 0


class OpportunityPipeline:
    def __init__(
        self,
        *,
        store: OpportunityStore,
        candidate_provider: CandidateProvider,
        price_checker: PriceChecker,
        listing_finder: ListingFinder,
        reputation_checker: ReputationChecker,
        notifier: RecommendationNotifier,
        thresholds: OpportunityThresholds,
        candidate_limit: int = 8,
        listing_limit: int = 5,
        candidate_check_interval_seconds: int = 30 * 60,
    ) -> None:
        self._store = store
        self._candidate_provider = candidate_provider
        self._price_checker = price_checker
        self._listing_finder = listing_finder
        self._reputation_checker = reputation_checker
        self._notifier = notifier
        self._thresholds = thresholds
        self._candidate_limit = candidate_limit
        self._listing_limit = listing_limit
        self._candidate_check_interval_seconds = candidate_check_interval_seconds

    def run_once(self) -> OpportunityPipelineStats:
        stats = _MutableStats()
        discovered = list(self._candidate_provider.discover(limit=self._candidate_limit))
        stats.discovered = len(discovered)
        for candidate in discovered:
            self._store.upsert_candidate(candidate)

        has_any_target = self._store.has_any_target()
        due_candidates = self._store.list_due_candidates(
            limit=self._candidate_limit,
            min_interval_seconds=self._candidate_check_interval_seconds,
        )
        for candidate in due_candidates:
            self._run_candidate(candidate, stats, has_any_target=has_any_target)
        return stats.freeze()

    def _run_candidate(
        self,
        candidate: OpportunityCandidate,
        stats: "_MutableStats",
        *,
        has_any_target: bool,
    ) -> None:
        stats.candidates_checked += 1
        logger.info(
            "Opportunity candidate check started candidate_id=%s game=%s title=%s heat=%s is_target=%s",
            candidate.candidate_id,
            candidate.game,
            candidate.title,
            candidate.heat_score,
            candidate.is_target,
        )
        try:
            if candidate.source_kind == "official_store_preorder":
                self._run_official_store_candidate(candidate, stats)
                return

            price = self._price_checker.check(candidate)
            if price is None:
                logger.info("Opportunity candidate skipped: no fair value candidate_id=%s", candidate.candidate_id)
                return
            stats.price_checks += 1
            self._store.record_price_check(price)

            price_max = target_price_for(price, self._thresholds, is_target=candidate.is_target)
            listings = list(self._listing_finder.find(candidate, price_max_jpy=price_max, limit=self._listing_limit))
            for listing in listings:
                self._run_listing(candidate, price, listing, stats, has_any_target=has_any_target)
        finally:
            self._store.mark_candidate_checked(candidate.candidate_id)

    def _run_official_store_candidate(
        self,
        candidate: OpportunityCandidate,
        stats: "_MutableStats",
    ) -> None:
        """Direct-notify path for official-store pre-order / lottery candidates.

        Bypasses the price_checker / listing_finder pipeline since official
        store announcements are not Mercari listings — we push the 🎫 headline
        immediately on first discovery. Dedup via listing_seen(source_url)."""
        official_url = str(candidate.metadata.get("listing_url") or candidate.source_url)
        if not official_url:
            return
        if self._store.listing_seen(official_url):
            return

        official_price = candidate.metadata.get("official_price_jpy")
        price_jpy = int(official_price) if official_price is not None else 0
        fair_value_jpy = int(price_jpy * 1.3) if price_jpy else 1  # notional fair value > retail

        synthetic_listing = ListingOffer(
            listing_id=build_listing_key(official_url),
            title=candidate.title,
            price_jpy=price_jpy,
            url=official_url,
        )
        synthetic_price = PriceCheck(
            candidate_id=candidate.candidate_id,
            fair_value_jpy=fair_value_jpy,
            confidence=0.9,
            sample_count=0,
        )
        synthetic_reputation = ReputationCheck(
            listing_url=official_url,
            trusted=True,
            status="official_store",
            reason="公式店舗",
        )
        recommendation = OpportunityRecommendation(
            recommendation_id=recommendation_id_for(synthetic_listing),
            candidate=candidate,
            price=synthetic_price,
            listing=synthetic_listing,
            reputation=synthetic_reputation,
            discount_pct=0.0,
            score=candidate.heat_score * 100,
            reasons=("official_store_preorder",),
        )
        self._store.record_recommendation(recommendation, accepted=True)
        self._notifier.notify(recommendation)
        self._store.mark_notified(recommendation.recommendation_id)
        stats.recommendations_sent += 1
        logger.info(
            "Official store pre-order notification sent candidate=%s url=%s",
            candidate.title, official_url,
        )

    def _run_listing(
        self,
        candidate: OpportunityCandidate,
        price: PriceCheck,
        listing: ListingOffer,
        stats: "_MutableStats",
        *,
        has_any_target: bool,
    ) -> None:
        if self._store.listing_seen(listing.url):
            stats.skipped_seen_listings += 1
            return
        stats.listings_checked += 1

        reputation = self._reputation_checker.check(listing)
        decision = evaluate_opportunity(
            candidate=candidate,
            price=price,
            listing=listing,
            reputation=reputation,
            thresholds=self._thresholds,
            has_any_target=has_any_target,
        )
        recommendation = OpportunityRecommendation(
            recommendation_id=recommendation_id_for(listing),
            candidate=candidate,
            price=price,
            listing=listing,
            reputation=reputation,
            discount_pct=decision.discount_pct,
            score=decision.score,
            reasons=decision.reasons,
        )
        self._store.record_recommendation(recommendation, accepted=decision.accepted)
        if not decision.accepted:
            stats.rejected += 1
            logger.info(
                "Opportunity listing rejected recommendation_id=%s score=%s reasons=%s",
                recommendation.recommendation_id,
                recommendation.score,
                list(recommendation.reasons),
            )
            return

        self._notifier.notify(recommendation)
        self._store.mark_notified(recommendation.recommendation_id)
        stats.recommendations_sent += 1
        logger.info(
            "Opportunity recommendation sent recommendation_id=%s candidate=%s listing=%s score=%s",
            recommendation.recommendation_id,
            candidate.title,
            listing.url,
            recommendation.score,
        )


@dataclass(slots=True)
class _MutableStats:
    discovered: int = 0
    candidates_checked: int = 0
    price_checks: int = 0
    listings_checked: int = 0
    recommendations_sent: int = 0
    skipped_seen_listings: int = 0
    rejected: int = 0

    def freeze(self) -> OpportunityPipelineStats:
        return OpportunityPipelineStats(
            discovered=self.discovered,
            candidates_checked=self.candidates_checked,
            price_checks=self.price_checks,
            listings_checked=self.listings_checked,
            recommendations_sent=self.recommendations_sent,
            skipped_seen_listings=self.skipped_seen_listings,
            rejected=self.rejected,
        )
