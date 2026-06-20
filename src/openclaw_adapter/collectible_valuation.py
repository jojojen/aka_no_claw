"""Market-valuation gate for collectible signals (issue #8, Deliverable 5).

Decides whether a ``CollectibleSignal`` may be *promoted* to ``actionable``
(eligible for recommendation) or must stay ``informational`` / be ``blocked``
with a structured diagnostic reason.

V1 scope: only TCG signals can become actionable, and only when a market
valuation with adequate confidence exists. Non-TCG domains are intelligence-only
— the provider returns ``None`` for them, so they are blocked with
``unsupported_domain``. The valuation source is injected (``MarketValuationProvider``
Protocol) so the existing TCG fair-value machinery can be wired in without this
module importing the whole pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Protocol

from .collectible_signal import (
    BLOCK_LOW_PRICE_CONFIDENCE,
    BLOCK_MISSING_MARKET_VALIDATION,
    BLOCK_NO_CONCRETE_PRODUCT,
    BLOCK_UNSUPPORTED_DOMAIN,
    CollectibleSignal,
)

logger = logging.getLogger(__name__)

DEFAULT_MIN_VALUATION_CONFIDENCE: float = 0.5


@dataclass(frozen=True, slots=True)
class MarketValuation:
    fair_value_jpy: int
    confidence: float
    sample_count: int = 0
    notes: tuple[str, ...] = ()


class MarketValuationProvider(Protocol):
    def valuate(self, signal: CollectibleSignal) -> MarketValuation | None:
        ...


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    signal: CollectibleSignal          # the updated signal (actionability set)
    actionability: str
    block_reason: str | None = None
    valuation: MarketValuation | None = None
    diagnostics: dict = field(default_factory=dict)


def _has_concrete_product(signal: CollectibleSignal) -> bool:
    """A signal anchors a concrete product when it carries a real product type
    or an official catalog code — not just generic IP-level chatter."""
    return signal.product_type != "other" or bool(signal.official_code)


def promote_signal(
    signal: CollectibleSignal,
    provider: MarketValuationProvider,
    *,
    min_confidence: float = DEFAULT_MIN_VALUATION_CONFIDENCE,
) -> PromotionDecision:
    """Run the promotion gate for a single signal.

    Order of checks (first failing gate wins, each with a structured reason):
      1. domain must be recommendable (V1: TCG only) → unsupported_domain
      2. signal must anchor a concrete product → no_concrete_product
      3. provider must return a valuation → missing_market_validation
      4. valuation confidence must clear the floor → low_price_confidence
      5. otherwise → actionable
    """
    diagnostics: dict = {"checked": []}

    def blocked(reason: str, valuation: MarketValuation | None = None) -> PromotionDecision:
        diagnostics["checked"].append(reason)
        updated = replace(
            signal,
            actionability="blocked",
            block_reason=reason,
            metadata={**dict(signal.metadata), "promotion": diagnostics},
        )
        return PromotionDecision(
            signal=updated,
            actionability="blocked",
            block_reason=reason,
            valuation=valuation,
            diagnostics=diagnostics,
        )

    if not signal.is_recommendable_domain:
        return blocked(BLOCK_UNSUPPORTED_DOMAIN)

    if not _has_concrete_product(signal):
        return blocked(BLOCK_NO_CONCRETE_PRODUCT)

    try:
        valuation = provider.valuate(signal)
    except Exception:
        logger.exception("promote_signal: valuation provider raised for %s", signal.signal_id)
        valuation = None
    if valuation is None:
        return blocked(BLOCK_MISSING_MARKET_VALIDATION)

    if valuation.confidence < min_confidence:
        return blocked(BLOCK_LOW_PRICE_CONFIDENCE, valuation=valuation)

    diagnostics["fair_value_jpy"] = valuation.fair_value_jpy
    diagnostics["valuation_confidence"] = valuation.confidence
    diagnostics["sample_count"] = valuation.sample_count
    updated = replace(
        signal,
        actionability="actionable",
        block_reason=None,
        metadata={**dict(signal.metadata), "promotion": diagnostics},
    )
    return PromotionDecision(
        signal=updated,
        actionability="actionable",
        block_reason=None,
        valuation=valuation,
        diagnostics=diagnostics,
    )


class TcgMarketValuationProvider:
    """MarketValuationProvider that values TCG signals via an injected fair-value
    callable and returns ``None`` for every other domain (V1: non-TCG is
    intelligence-only, so it can never be promoted).

    ``fair_value_fn`` adapts whatever existing TCG fair-value machinery the
    caller has — e.g. a wrapper around the opportunity ``PriceChecker`` — into a
    ``MarketValuation``. Keeping it a callable avoids importing the pipeline here.
    """

    def __init__(self, fair_value_fn) -> None:
        self._fair_value_fn = fair_value_fn

    def valuate(self, signal: CollectibleSignal) -> MarketValuation | None:
        if not signal.is_recommendable_domain:
            return None
        try:
            return self._fair_value_fn(signal)
        except Exception:
            logger.exception(
                "TcgMarketValuationProvider: fair_value_fn raised for %s", signal.signal_id
            )
            return None
