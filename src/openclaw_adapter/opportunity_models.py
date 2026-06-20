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
