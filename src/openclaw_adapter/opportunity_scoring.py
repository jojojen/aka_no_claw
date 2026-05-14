from __future__ import annotations

from dataclasses import dataclass

from .opportunity_models import ListingOffer, OpportunityCandidate, PriceCheck, ReputationCheck


@dataclass(frozen=True, slots=True)
class OpportunityThresholds:
    min_heat_score: float = 70.0
    max_price_ratio: float = 0.85
    min_price_confidence: float = 0.60
    min_total_reviews: int = 30
    min_positive_rate: float = 97.0


@dataclass(frozen=True, slots=True)
class OpportunityDecision:
    accepted: bool
    discount_pct: float
    score: float
    reasons: tuple[str, ...]


def target_price_for(price: PriceCheck, thresholds: OpportunityThresholds) -> int:
    if price.target_price_jpy is not None:
        return price.target_price_jpy
    return int(price.fair_value_jpy * thresholds.max_price_ratio)


def evaluate_opportunity(
    *,
    candidate: OpportunityCandidate,
    price: PriceCheck,
    listing: ListingOffer,
    reputation: ReputationCheck,
    thresholds: OpportunityThresholds,
) -> OpportunityDecision:
    reasons: list[str] = []
    accepted = True

    if candidate.heat_score < thresholds.min_heat_score:
        accepted = False
        reasons.append(f"SNS heat {candidate.heat_score:.0f} is below {thresholds.min_heat_score:.0f}.")
    else:
        reasons.append(f"SNS heat {candidate.heat_score:.0f} passed.")

    if price.confidence < thresholds.min_price_confidence:
        accepted = False
        reasons.append(f"Price confidence {price.confidence:.2f} is below {thresholds.min_price_confidence:.2f}.")
    else:
        reasons.append(f"Price confidence {price.confidence:.2f} passed.")

    price_ratio = listing.price_jpy / price.fair_value_jpy if price.fair_value_jpy > 0 else 999.0
    discount_pct = max(0.0, (1.0 - price_ratio) * 100.0)
    if price_ratio > thresholds.max_price_ratio:
        accepted = False
        reasons.append(f"Listing price is only {discount_pct:.1f}% below fair value.")
    else:
        reasons.append(f"Listing price is {discount_pct:.1f}% below fair value.")

    if not reputation.trusted:
        accepted = False
        reasons.append(reputation.reason or "Seller reputation did not pass.")
    else:
        reasons.append(reputation.reason or "Seller reputation passed.")

    heat_component = min(max(candidate.heat_score, 0.0), 100.0)
    discount_component = min(discount_pct / 30.0 * 100.0, 100.0)
    reputation_component = _reputation_component(reputation)
    score = round(heat_component * 0.35 + discount_component * 0.35 + reputation_component * 0.30, 2)

    return OpportunityDecision(
        accepted=accepted,
        discount_pct=round(discount_pct, 1),
        score=score,
        reasons=tuple(reasons),
    )


def reputation_passes(reputation: ReputationCheck, thresholds: OpportunityThresholds) -> tuple[bool, str]:
    total = reputation.total_reviews
    rate = reputation.positive_rate
    if total is None:
        return False, "Seller total review count is unavailable."
    if total < thresholds.min_total_reviews:
        return False, f"Seller has only {total} reviews."
    if rate is None:
        return False, "Seller positive review rate is unavailable."
    if rate < thresholds.min_positive_rate:
        return False, f"Seller positive rate {rate:.1f}% is below {thresholds.min_positive_rate:.1f}%."
    return True, f"Seller reputation passed: {rate:.1f}% positive over {total} reviews."


def _reputation_component(reputation: ReputationCheck) -> float:
    grade_bonus = {"A": 100.0, "B": 82.0, "C": 55.0, "D": 30.0}.get((reputation.grade or "").upper())
    if grade_bonus is not None:
        return grade_bonus
    if reputation.positive_rate is not None:
        return min(max(reputation.positive_rate, 0.0), 100.0)
    return 0.0
