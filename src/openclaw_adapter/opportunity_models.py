from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from typing import Mapping


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_candidate_id(*, game: str, title: str, search_query: str, source_url: str = "") -> str:
    key = f"{game.strip().lower()}|{title.strip().lower()}|{search_query.strip().lower()}|{source_url.strip()}"
    return "opp_" + sha1(key.encode("utf-8")).hexdigest()[:16]


def build_listing_key(url: str) -> str:
    return "listing_" + sha1(url.strip().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    candidate_id: str
    game: str
    title: str
    search_query: str
    heat_score: float
    reason: str
    source_kind: str = "sns"
    source_url: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


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
