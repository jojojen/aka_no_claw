"""Collectible intelligence contract (issue #8, Deliverable 1).

A ``CollectibleSignal`` is the structured product-intelligence record that sits
*between* a raw demand/catalyst signal (SNS / 4chan / web) or a product-truth
source (official store / marketplace) and an ``OpportunityCandidate``.

It is intentionally generic so that non-TCG verticals — 色紙 (shikishi), CD,
acrylic goods, figures, event merch — can be represented in the intelligence
layer even though V1 only auto-recommends TCG. The TCG opportunity pipeline is
untouched; signals are derived alongside candidates, never instead of them.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from hashlib import sha1
from typing import Mapping

from .opportunity_models import OpportunityCandidate, utc_now_iso

# --- closed vocabularies (protocol tokens, not open-world recognition) -------

COLLECTIBLE_DOMAINS: tuple[str, ...] = (
    "tcg",
    "music",
    "goods",
    "figure",
    "book",
    "event_merch",
    "other",
)

ENTITY_KINDS: tuple[str, ...] = (
    "character",
    "group",
    "event",
    "artist",
    "set",
    "campaign",
    "store",
    "other",
)

# Where a signal originated. SNS / 4chan / web are demand/catalyst sources;
# official_store / marketplace are product/price truth sources.
SIGNAL_SOURCE_KINDS: tuple[str, ...] = (
    "sns",
    "fourchan",
    "official_store",
    "marketplace",
    "manual",
    "web_search",
)

# actionable   = eligible for recommendation promotion (subject to valuation)
# informational = stored as intelligence/evidence only, not promoted
# blocked      = explicitly disqualified; see block_reason
ACTIONABILITY: tuple[str, ...] = ("actionable", "informational", "blocked")

# Generic product types across verticals. TCG types kept for compatibility with
# opportunity_models.PRODUCT_TYPES; collectible verticals add their own.
COLLECTIBLE_PRODUCT_TYPES: tuple[str, ...] = (
    "single_card",
    "booster_pack",
    "sealed_box",
    "starter_deck",
    "promo",
    "shikishi",
    "cd",
    "blu_ray",
    "acrylic_stand",
    "figure",
    "plush",
    "book",
    "tapestry",
    "badge",
    "other",
)

# Structured diagnostic reasons a candidate/signal is blocked from recommendation.
BLOCK_NO_CONCRETE_PRODUCT = "no_concrete_product"
BLOCK_MISSING_OFFICIAL_STORE_EVIDENCE = "missing_official_store_evidence"
BLOCK_MISSING_MARKET_VALIDATION = "missing_market_validation"
BLOCK_UNSUPPORTED_DOMAIN = "unsupported_domain"
BLOCK_LOW_PRICE_CONFIDENCE = "low_price_confidence"
BLOCK_SELLER_REPUTATION_ISSUE = "seller_reputation_issue"

BLOCK_REASONS: tuple[str, ...] = (
    BLOCK_NO_CONCRETE_PRODUCT,
    BLOCK_MISSING_OFFICIAL_STORE_EVIDENCE,
    BLOCK_MISSING_MARKET_VALIDATION,
    BLOCK_UNSUPPORTED_DOMAIN,
    BLOCK_LOW_PRICE_CONFIDENCE,
    BLOCK_SELLER_REPUTATION_ISSUE,
)

# V1: only TCG goods are pushed as automatic recommendations. Other domains can
# be represented as intelligence but must not be auto-recommended yet.
RECOMMENDABLE_DOMAINS: frozenset[str] = frozenset({"tcg"})

_DOMAIN_ALIASES: dict[str, str] = {
    "trading_card": "tcg",
    "trading_card_game": "tcg",
    "card": "tcg",
    "cd": "music",
    "album": "music",
    "single": "music",
    "acrylic": "goods",
    "acrylic_stand": "goods",
    "shikishi": "goods",
    "merch": "goods",
    "merchandise": "goods",
    "figures": "figure",
    "prize_figure": "figure",
    "books": "book",
    "artbook": "book",
    "doujin": "book",
}

_SOURCE_ALIASES: dict[str, str] = {
    "official_store_preorder": "official_store",
    "store": "official_store",
    "mercari": "marketplace",
    "rakuma": "marketplace",
    "yahoo": "marketplace",
    "x": "sns",
    "twitter": "sns",
    "4chan": "fourchan",
    "search": "web_search",
}


def _coerce(value: object, allowed: tuple[str, ...], aliases: dict[str, str], default: str) -> str:
    if not isinstance(value, str):
        return default
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    cleaned = aliases.get(cleaned, cleaned)
    return cleaned if cleaned in allowed else default


def normalize_domain(value: object) -> str:
    return _coerce(value, COLLECTIBLE_DOMAINS, _DOMAIN_ALIASES, "other")


def normalize_source_kind(value: object) -> str:
    return _coerce(value, SIGNAL_SOURCE_KINDS, _SOURCE_ALIASES, "manual")


def normalize_entity_kind(value: object) -> str:
    return _coerce(value, ENTITY_KINDS, {}, "other")


def normalize_collectible_product_type(value: object) -> str:
    return _coerce(value, COLLECTIBLE_PRODUCT_TYPES, {}, "other")


def build_signal_id(
    *,
    collectible_domain: str,
    ip_canonical: str,
    product_type: str,
    title: str,
    official_code: str | None = None,
) -> str:
    key = "|".join(
        [
            collectible_domain.strip().lower(),
            ip_canonical.strip().lower(),
            product_type.strip().lower(),
            title.strip().lower(),
            (official_code or "").strip().lower(),
        ]
    )
    return "sig_" + sha1(key.encode("utf-8")).hexdigest()[:16]


def _clean_urls(urls: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        u = url.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CollectibleSignal:
    signal_id: str
    source_kind: str                      # SIGNAL_SOURCE_KINDS
    collectible_domain: str               # COLLECTIBLE_DOMAINS
    ip_canonical: str                     # IP / work / franchise, e.g. "Project SEKAI"
    title: str = ""
    entity_kind: str = "other"            # ENTITY_KINDS
    product_family: str | None = None     # official product line / series
    product_type: str = "other"           # COLLECTIBLE_PRODUCT_TYPES
    official_code: str | None = None      # JAN / SKU / catalog / set / card number
    release_window: str | None = None
    retail_price_jpy: int | None = None   # official/store cost basis when present
    source_urls: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence_count: int = 0
    actionability: str = "informational"  # ACTIONABILITY
    block_reason: str | None = None       # BLOCK_REASONS when actionability == "blocked"
    heat_score: float = 0.0
    anchor_types: tuple[str, ...] = ()     # concrete anchors that justified evidence
    entity_id: str | None = None           # canonical Market Entity join key (issue #12); None when unresolved/ambiguous
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def is_recommendable_domain(self) -> bool:
        return self.collectible_domain in RECOMMENDABLE_DOMAINS


# Anchor tokens recording *why* a signal carries evidence weight. These are
# protocol tokens emitted by trusted source paths (not open-world recognition).
ANCHOR_OFFICIAL_STORE_LISTING = "official_store_listing"
ANCHOR_MARKETPLACE_LISTING = "marketplace_listing"
ANCHOR_SNS_CATALYST = "sns_catalyst"


def candidate_to_signal(
    candidate: OpportunityCandidate,
    *,
    collectible_domain: str = "tcg",
) -> CollectibleSignal:
    """Bridge an existing OpportunityCandidate into a CollectibleSignal.

    Signals are derived *alongside* candidates, never instead of them — the TCG
    opportunity pipeline is untouched. This lets the generic intelligence layer
    capture the same product truth a candidate already represents.

    Reads structured fields the official-store provider stores in
    ``candidate.metadata`` (``ip_canonical``, ``official_price_jpy`` /
    ``official_code`` / ``product_code``, ``source_confidence``); it does *not*
    re-run any keyword recognition — that classification already happened when
    the candidate was built.
    """
    meta = dict(candidate.metadata)

    ip_canonical = str(meta.get("ip_canonical") or "").strip() or candidate.title.strip()

    retail = meta.get("official_price_jpy")
    retail_jpy = int(retail) if isinstance(retail, (int, float)) else None

    official_code = (
        meta.get("official_code")
        or meta.get("product_code")
        or candidate.product_identifier
    )

    src = normalize_source_kind(candidate.source_kind)

    # Official-store listings are authoritative product truth → actionable with
    # an official-store anchor. SNS-sourced candidates are demand catalysts and
    # stay informational until a market-valuation gate promotes them.
    if src == "official_store":
        actionability = "actionable"
        anchors: tuple[str, ...] = (ANCHOR_OFFICIAL_STORE_LISTING,)
    elif src == "marketplace":
        actionability = "actionable"
        anchors = (ANCHOR_MARKETPLACE_LISTING,)
    else:
        actionability = "informational"
        anchors = (ANCHOR_SNS_CATALYST,)

    raw_conf = meta.get("source_confidence")
    confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else 0.0

    source_urls = (candidate.source_url,) if candidate.source_url else ()

    return make_signal(
        source_kind=candidate.source_kind,
        collectible_domain=collectible_domain,
        ip_canonical=ip_canonical,
        title=candidate.title,
        product_type=candidate.product_type,
        official_code=official_code if isinstance(official_code, str) else None,
        retail_price_jpy=retail_jpy,
        source_urls=source_urls,
        confidence=confidence,
        evidence_count=1,
        actionability=actionability,
        heat_score=candidate.heat_score,
        anchor_types=anchors,
        entity_id=candidate.entity_id,
        metadata={
            "candidate_id": candidate.candidate_id,
            "game": candidate.game,
            "candidate_source_kind": candidate.source_kind,
        },
    )


def make_signal(
    *,
    source_kind: str,
    collectible_domain: str,
    ip_canonical: str,
    title: str = "",
    entity_kind: str = "other",
    product_family: str | None = None,
    product_type: str = "other",
    official_code: str | None = None,
    release_window: str | None = None,
    retail_price_jpy: int | None = None,
    source_urls: Iterable[str] = (),
    confidence: float = 0.0,
    evidence_count: int = 0,
    actionability: str = "informational",
    block_reason: str | None = None,
    heat_score: float = 0.0,
    anchor_types: Iterable[str] = (),
    entity_id: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> CollectibleSignal:
    """Construct a normalized CollectibleSignal with a stable derived id."""
    domain = normalize_domain(collectible_domain)
    src = normalize_source_kind(source_kind)
    ptype = normalize_collectible_product_type(product_type)
    ekind = normalize_entity_kind(entity_kind)
    act = actionability if actionability in ACTIONABILITY else "informational"
    reason = block_reason if (block_reason in BLOCK_REASONS) else None
    if act != "blocked":
        reason = None
    return CollectibleSignal(
        signal_id=build_signal_id(
            collectible_domain=domain,
            ip_canonical=ip_canonical,
            product_type=ptype,
            title=title,
            official_code=official_code,
        ),
        source_kind=src,
        collectible_domain=domain,
        ip_canonical=ip_canonical.strip(),
        title=title.strip(),
        entity_kind=ekind,
        product_family=(product_family or None),
        product_type=ptype,
        official_code=(official_code or None),
        release_window=(release_window or None),
        retail_price_jpy=retail_price_jpy,
        source_urls=_clean_urls(source_urls),
        confidence=max(0.0, min(1.0, float(confidence))),
        evidence_count=max(0, int(evidence_count)),
        actionability=act,
        block_reason=reason,
        heat_score=float(heat_score),
        anchor_types=tuple(a for a in anchor_types if isinstance(a, str) and a.strip()),
        entity_id=(entity_id or None),
        metadata=dict(metadata or {}),
    )
