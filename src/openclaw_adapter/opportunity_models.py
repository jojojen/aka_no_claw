from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from typing import Mapping


# Layer 2 of the candidate hierarchy. The LLM is constrained to these values
# so that two products on different "type" lines never get squeezed into the
# same candidate just because they happen to be the same IP.
PRODUCT_TYPES: tuple[str, ...] = (
    "single_card",
    "booster_pack",
    "sealed_box",
    "starter_deck",
    "promo",
    "other",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_product_type(value: object) -> str:
    """Coerce an LLM-supplied product_type string into the constrained enum.

    Falls back to ``"other"`` for unknown / missing values. Only common
    English/Japanese aliases are mapped; the goal is to absorb the most
    obvious variants without growing into a sprawling synonym table.
    """
    if not isinstance(value, str):
        return "other"
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "card": "single_card",
        "singles": "single_card",
        "trading_card": "single_card",
        "booster": "booster_pack",
        "pack": "booster_pack",
        "box": "sealed_box",
        "display": "sealed_box",
        "deck": "starter_deck",
        "structure_deck": "starter_deck",
        "trial_deck": "starter_deck",
        "promo_card": "promo",
        "promotional": "promo",
    }
    cleaned = aliases.get(cleaned, cleaned)
    return cleaned if cleaned in PRODUCT_TYPES else "other"


def build_candidate_id(
    *,
    game: str,
    product_type: str,
    title: str,
    search_query: str,
    product_identifier: str | None = None,
    source_url: str = "",
) -> str:
    key = "|".join(
        [
            game.strip().lower(),
            product_type.strip().lower(),
            title.strip().lower(),
            (product_identifier or "").strip().lower(),
            search_query.strip().lower(),
            source_url.strip(),
        ]
    )
    return "opp_" + sha1(key.encode("utf-8")).hexdigest()[:16]


def build_listing_key(url: str) -> str:
    return "listing_" + sha1(url.strip().encode("utf-8")).hexdigest()[:16]


def merge_string_list(
    existing: Sequence[str],
    incoming: Iterable[str],
    *,
    max_len: int = 12,
    skip: Iterable[str] = (),
) -> tuple[str, ...]:
    """Merge two string sequences, casefold-deduped, preserving order, capped.

    `skip` is a set of strings (e.g. the candidate's own title) that must not
    appear in the output — LLMs commonly echo the title back as an alias.
    """
    skip_set = {s.casefold() for s in skip if s}
    seen: set[str] = set()
    out: list[str] = []
    for kw in tuple(existing) + tuple(incoming):
        if not isinstance(kw, str):
            continue
        cleaned = " ".join(kw.strip().split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen or key in skip_set:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= max_len:
            break
    return tuple(out)


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    candidate_id: str
    game: str                              # Layer 1: IP (pokemon/ws/yugioh/union_arena)
    product_type: str                      # Layer 2: PRODUCT_TYPES enum
    title: str                             # Layer 3: specific product name
    search_query: str
    heat_score: float
    reason: str
    product_identifier: str | None = None  # Layer 3 detail: card number or set code
    source_kind: str = "sns"
    source_url: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    aliases: tuple[str, ...] = ()
    related_keywords: tuple[str, ...] = ()
    is_target: bool = False                # 🎯 user-declared target (lenient thresholds) vs 🔍 system-discovered opportunity
    entity_id: str | None = None           # canonical Market Entity join key (issue #12); None when unresolved/ambiguous
    # Fair-value snapshot from the #15 engine, attached via attach_fair_value().
    # All optional: a candidate with no resolved entity / insufficient evidence
    # simply carries None and the pipeline treats valuation as unknown.
    fair_value_jpy: int | None = None
    fair_value_confidence: float | None = None
    discount_to_fair_value: float | None = None   # >0 ⇔ cheapest ask is below fair value
    liquidity_adjustment: float | None = None
    valuation_reasons: tuple[str, ...] = ()


def attach_fair_value(candidate: "OpportunityCandidate", engine) -> "OpportunityCandidate":
    """Compute a fair-value snapshot for *candidate* via the #15 ``FairValueEngine``
    and return a copy with the valuation fields populated (Deliverable 7).

    The valuation is keyed on the candidate's canonical ``entity_id``; without one
    there is nothing to value against, so the candidate is returned unchanged. The
    cheapest current ask (from the price ledger) is the "buyable" price compared
    against fair value to derive the discount. Insufficient evidence degrades to a
    valuation-reasons note rather than a fabricated number."""
    from dataclasses import replace

    eid = (candidate.entity_id or "").strip()
    if not eid:
        return candidate

    estimate = engine.estimate(eid)
    if not estimate.has_value or estimate.fair_value is None:
        return replace(
            candidate,
            valuation_reasons=tuple(estimate.explanation) or ("insufficient market evidence",),
        )

    observed = None
    if getattr(engine, "price_ledger", None) is not None:
        snapshot = engine.price_ledger.get_market_snapshot(eid)
        observed = snapshot.min_price  # cheapest ask = the price you could buy at

    discount = None
    reasons = list(estimate.explanation)
    if observed is not None:
        mispricing = engine.evaluate_mispricing(eid, observed)
        discount = mispricing.discount_to_fair_value
        reasons.extend(mispricing.reasons)

    return replace(
        candidate,
        fair_value_jpy=int(estimate.fair_value),
        fair_value_confidence=round(float(estimate.confidence), 4),
        discount_to_fair_value=discount,
        liquidity_adjustment=estimate.liquidity_adjustment,
        valuation_reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class PriceCheck:
    candidate_id: str
    fair_value_jpy: int
    confidence: float
    sample_count: int = 0
    target_price_jpy: int | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ListingOffer:
    listing_id: str
    title: str
    price_jpy: int
    url: str
    thumbnail_url: str | None = None


@dataclass(frozen=True, slots=True)
class ReputationCheck:
    listing_url: str
    trusted: bool
    proof_url: str = ""
    total_reviews: int | None = None
    positive_rate: float | None = None
    grade: str | None = None
    status: str = "unknown"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OpportunityRecommendation:
    recommendation_id: str
    candidate: OpportunityCandidate
    price: PriceCheck
    listing: ListingOffer
    reputation: ReputationCheck
    discount_pct: float
    score: float
    reasons: tuple[str, ...]
