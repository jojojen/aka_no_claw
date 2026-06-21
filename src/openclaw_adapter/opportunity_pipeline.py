from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Protocol, Sequence

from .opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    attach_fair_value,
    build_listing_key,
)
from .opportunity_scoring import OpportunityThresholds, evaluate_opportunity, target_price_for
from .opportunity_store import OpportunityStore, recommendation_id_for
from .collectible_signal import candidate_to_signal
from .collectible_signal_store import CollectibleSignalStore
from .collectible_valuation import (
    MarketValuation,
    TcgMarketValuationProvider,
    promote_signal,
)

logger = logging.getLogger(__name__)

SEALED_BOX_MIN_PROFIT_PCT: float = 10.0  # skip official-store notifications below this return %

OFFICIAL_STORE_SOURCE_KIND = "official_store_preorder"


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
        signal_store: CollectibleSignalStore | None = None,
        fair_value_engine=None,
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
        # Collectible intelligence layer (issue #8). When wired, official-store
        # candidates are persisted as structured CollectibleSignals *alongside*
        # the existing TCG opportunity flow, which is left untouched. Recording
        # is always best-effort: a signal-store failure must never break a
        # recommendation (C4 — logged, not swallowed silently).
        self._signal_store = signal_store
        # #15 D7: when wired, each discovered candidate with a canonical entity is
        # stamped with a fair-value snapshot (fair value / discount / liquidity
        # adjustment / reasons) before persistence. Best-effort: a valuation
        # failure must never block discovery (C4 — logged, not swallowed).
        self._fair_value_engine = fair_value_engine

    def run_once(self) -> OpportunityPipelineStats:
        stats = _MutableStats()
        discovered = list(self._candidate_provider.discover(limit=self._candidate_limit))
        stats.discovered = len(discovered)
        for candidate in discovered:
            candidate = self._attach_fair_value(candidate)
            self._store.upsert_candidate(candidate)
            self._record_discovery_signal(candidate)

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
            self._record_skip_signal(candidate, reason="missing_listing_url")
            return
        if self._store.listing_seen(official_url):
            return

        official_price = candidate.metadata.get("official_price_jpy")
        price_jpy = int(official_price) if official_price is not None else 0

        sample_note: str | None = None
        if price_jpy and candidate.product_type in ("sealed_box", "booster_pack"):
            market = self._price_checker.check(candidate)
            if market and market.fair_value_jpy > price_jpy:
                discount_pct = (market.fair_value_jpy - price_jpy) / price_jpy * 100
                if discount_pct < SEALED_BOX_MIN_PROFIT_PCT:
                    logger.info(
                        "Official store sealed box skipped — profit below threshold candidate=%s "
                        "lottery=%d fair_value=%d pct=%.1f",
                        candidate.title, price_jpy, market.fair_value_jpy, discount_pct,
                    )
                    self._record_skip_signal(
                        candidate,
                        reason="profit_below_threshold",
                        fair_value_jpy=market.fair_value_jpy,
                        retail_price_jpy=price_jpy,
                        profit_pct=round(discount_pct, 1),
                        threshold_pct=SEALED_BOX_MIN_PROFIT_PCT,
                    )
                    return
                fair_value_jpy = market.fair_value_jpy
                sample_note = f"二手参考価格 ¥{market.fair_value_jpy:,}（{market.sample_count}件）"
            else:
                fair_value_jpy = int(price_jpy * 1.3)
                sample_note = "二手市場資料不足，以定価 ×1.3 估算"
        else:
            fair_value_jpy = int(price_jpy * 1.3) if price_jpy else 1

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
            notes=(sample_note,) if sample_note else (),
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
        self._promote_and_record_signal(candidate, synthetic_price)
        self._store.record_recommendation(recommendation, accepted=True)
        self._notifier.notify(recommendation)
        self._store.mark_notified(recommendation.recommendation_id)
        stats.recommendations_sent += 1
        logger.info(
            "Official store pre-order notification sent candidate=%s url=%s",
            candidate.title, official_url,
        )

    def _attach_fair_value(self, candidate: OpportunityCandidate) -> OpportunityCandidate:
        """Stamp a #15 fair-value snapshot onto a candidate before persistence.

        Best-effort and entity-gated: with no engine or no resolved ``entity_id``
        the candidate is returned untouched. A valuation error is logged (C4) but
        never aborts discovery — the candidate still persists without valuation."""
        if self._fair_value_engine is None or not candidate.entity_id:
            return candidate
        try:
            return attach_fair_value(candidate, self._fair_value_engine)
        except Exception:
            logger.exception(
                "Opportunity pipeline: fair-value attach failed candidate_id=%s entity_id=%s",
                candidate.candidate_id,
                candidate.entity_id,
            )
            return candidate

    def _record_discovery_signal(self, candidate: OpportunityCandidate) -> None:
        """Persist an official-store candidate as structured intelligence on first
        sight (issue #8 finding 2). Only official-store listings are product
        truth; other provider candidates (web-trend / hot-card guesses) are not
        promoted into the signal layer here to keep it clean.

        Discovery is "seen, not yet evaluated": the valuation/promote gate is the
        sole authority for ``actionable``. So a first-sighting is recorded as
        ``informational`` (never the optimistic ``actionable`` default), and a
        re-sighting must not clobber a verdict the gate has since written — both
        avoid showing a 可下手 the runtime has not confirmed (issue #8 review)."""
        if self._signal_store is None:
            return
        if candidate.source_kind != OFFICIAL_STORE_SOURCE_KIND:
            return
        try:
            signal = candidate_to_signal(candidate)
            existing = self._signal_store.get_signal(signal.signal_id)
            if existing is None:
                signal = replace(
                    signal, actionability="informational", block_reason=None
                )
            else:
                signal = replace(
                    signal,
                    actionability=existing.actionability,
                    block_reason=existing.block_reason,
                )
            self._signal_store.upsert_signal(signal)
        except Exception:
            logger.exception(
                "OpportunityPipeline: signal record failed candidate=%s",
                candidate.candidate_id,
            )

    def _record_skip_signal(
        self,
        candidate: OpportunityCandidate,
        *,
        reason: str,
        **diagnostics,
    ) -> None:
        """Persist a non-actionable final state for an official-store candidate the
        runtime evaluated but did **not** recommend, so the intelligence layer and
        the real recommendation outcome stay consistent (issue #8 review).

        The candidate is genuine product intelligence (a real official-store
        listing), it is just not a buy — recorded as ``informational`` with a
        structured ``skip`` note explaining why, never left at ``actionable``."""
        if self._signal_store is None:
            return
        if candidate.source_kind != OFFICIAL_STORE_SOURCE_KIND:
            return
        try:
            signal = candidate_to_signal(candidate)
            signal = replace(
                signal,
                actionability="informational",
                block_reason=None,
                metadata={
                    **dict(signal.metadata),
                    "skip": {"reason": reason, **diagnostics},
                },
            )
            self._signal_store.upsert_signal(signal)
        except Exception:
            logger.exception(
                "OpportunityPipeline: skip signal record failed candidate=%s",
                candidate.candidate_id,
            )

    def _promote_and_record_signal(
        self,
        candidate: OpportunityCandidate,
        price: PriceCheck,
    ) -> None:
        """Run the market-valuation/promote gate for an official-store candidate
        and persist the (merged) decision signal — realizing the funnel's
        ``signal → valuation/promote gate`` step at runtime (issue #8 finding 1).

        Reuses the price already computed for the recommendation, so it adds no
        extra price check (C5/C7 — no rate-limit amplification)."""
        if self._signal_store is None:
            return
        try:
            signal = candidate_to_signal(candidate)
            valuation = MarketValuation(
                fair_value_jpy=price.fair_value_jpy,
                confidence=price.confidence,
                sample_count=price.sample_count,
                notes=price.notes,
            )
            provider = TcgMarketValuationProvider(lambda _s: valuation)
            decision = promote_signal(signal, provider)
            self._signal_store.upsert_signal(decision.signal)
        except Exception:
            logger.exception(
                "OpportunityPipeline: signal promotion failed candidate=%s",
                candidate.candidate_id,
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
