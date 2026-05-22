from __future__ import annotations

from dataclasses import dataclass

from .opportunity_models import ListingOffer, OpportunityCandidate, PriceCheck, ReputationCheck


@dataclass(frozen=True, slots=True)
class OpportunityThresholds:
    # Strict path: 🔍 system-discovered (SNS / hot board / web search) opportunities.
    min_heat_score: float = 70.0
    max_price_ratio: float = 0.85
    min_price_confidence: float = 0.60
    min_total_reviews: int = 30
    min_positive_rate: float = 97.0
    # When any Target exists, tighten the strict path further to suppress noise
    # the user already opted out of via /hunt pin and /watchlist focus.
    min_heat_score_when_target_active: float = 85.0
    # Lenient path: 🎯 user-declared Target (/hunt pin, mercari /watchlist, 👍 feedback).
    target_min_heat_score: float = 0.0
    target_max_price_ratio: float = 0.95
    target_min_price_confidence: float = 0.50


@dataclass(frozen=True, slots=True)
class OpportunityDecision:
    accepted: bool
    discount_pct: float
    score: float
    reasons: tuple[str, ...]


def target_price_for(
    price: PriceCheck,
    thresholds: OpportunityThresholds,
    *,
    is_target: bool = False,
) -> int:
    if price.target_price_jpy is not None:
        return price.target_price_jpy
    ratio = thresholds.target_max_price_ratio if is_target else thresholds.max_price_ratio
    return int(price.fair_value_jpy * ratio)


def evaluate_opportunity(
    *,
    candidate: OpportunityCandidate,
    price: PriceCheck,
    listing: ListingOffer,
    reputation: ReputationCheck,
    thresholds: OpportunityThresholds,
    has_any_target: bool = False,
) -> OpportunityDecision:
    reasons: list[str] = []
    accepted = True

    if candidate.is_target:
        min_heat = thresholds.target_min_heat_score
        max_ratio = thresholds.target_max_price_ratio
        min_conf = thresholds.target_min_price_confidence
    else:
        # When the user has set up Target(s), tighten the bar for unrelated
        # auto-discovered candidates so they don't keep spamming on top.
        min_heat = (
            thresholds.min_heat_score_when_target_active
            if has_any_target
            else thresholds.min_heat_score
        )
        max_ratio = thresholds.max_price_ratio
        min_conf = thresholds.min_price_confidence

    if candidate.heat_score < min_heat:
        accepted = False
        reasons.append(f"SNS heat {candidate.heat_score:.0f} is below {min_heat:.0f}.")
    else:
        reasons.append(f"SNS heat {candidate.heat_score:.0f} passed.")

    if price.confidence < min_conf:
        accepted = False
        reasons.append(f"Price confidence {price.confidence:.2f} is below {min_conf:.2f}.")
    else:
        reasons.append(f"Price confidence {price.confidence:.2f} passed.")

    price_ratio = listing.price_jpy / price.fair_value_jpy if price.fair_value_jpy > 0 else 999.0
    discount_pct = max(0.0, (1.0 - price_ratio) * 100.0)
    if price_ratio > max_ratio:
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
