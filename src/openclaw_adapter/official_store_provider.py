"""OfficialStoreCandidateProvider — wraps all official-store crawlers and
converts OfficialStoreListing → OpportunityCandidate for the pipeline.

Source kind: "official_store_preorder"
Heat scores by status:
  lottery_open  → 0.85 (deadline-driven, highest urgency)
  preorder_open → 0.75
  available     → 0.65
  coming_soon   → 0.55
  other         → 0.45

Game inference priority:
  1. listing.categories (e.g. "union_arena", "pokemon")
  2. Title keyword scan
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Sequence

from .opportunity_models import (
    OpportunityCandidate,
    build_candidate_id,
    normalize_product_type,
    utc_now_iso,
)

if TYPE_CHECKING:
    from market_monitor.official_store_base import OfficialStoreCrawler, OfficialStoreListing
    from .collab_outcomes_store import CollabOutcomesStore
    from .collab_similarity_provider import CollabSimilarityProvider

logger = logging.getLogger(__name__)

SOURCE_KIND = "official_store_preorder"

_STATUS_HEAT: dict[str, float] = {
    "lottery_open": 0.85,
    "preorder_open": 0.75,
    "available": 0.65,
    "coming_soon": 0.55,
}

_GAME_KW: dict[str, re.Pattern[str]] = {
    "pokemon_tcg": re.compile(
        r"ポケモンカード|pokemon card|ポケカ|pikachu|イーブイ|スカーレット|バイオレット|スターターデッキ",
        re.IGNORECASE,
    ),
    "union_arena": re.compile(
        r"UNION ARENA|ユニオンアリーナ|UA\b",
        re.IGNORECASE,
    ),
    "weiss_schwarz": re.compile(
        r"ヴァイスシュヴァルツ|Weiss Schwarz|ヴァイス",
        re.IGNORECASE,
    ),
    "yugioh": re.compile(r"遊戯王|YUGIOH|YU-GI-OH", re.IGNORECASE),
    "one_piece_tcg": re.compile(r"ワンピースカード|ONE PIECE CARD", re.IGNORECASE),
    "battle_spirits": re.compile(r"バトスピ|Battle Spirits", re.IGNORECASE),
}

_PRODUCT_TYPE_KW: dict[str, re.Pattern[str]] = {
    "sealed_box": re.compile(r"\d?BOX|1BOX|\d+ボックス", re.IGNORECASE),
    "booster_pack": re.compile(r"ブースターパック|ブースター|拡張パック|booster", re.IGNORECASE),
    "starter_deck": re.compile(r"スターターデッキ|スターター|starter", re.IGNORECASE),
    "promo": re.compile(r"プロモ|promo|特典|限定配布", re.IGNORECASE),
}

# Known TCG prefixes to strip when extracting IP canonical from title
_TCG_TITLE_PREFIXES: tuple[str, ...] = (
    "ヴァイスシュヴァルツ ",
    "Weiss Schwarz ",
    "ヴァイス ",
    "UNION ARENA ",
    "ユニオンアリーナ ",
    "UA ",
    "ポケモンカードゲーム ",
    "ポケモンカード ",
    "遊戯王 ",
    "ワンピースカードゲーム ",
    "ワンピースカード ",
    "バトルスピリッツ ",
    "バトスピ ",
)


class OfficialStoreCandidateProvider:
    """Discovers pre-order / lottery candidates from official store crawlers.

    Implements the CandidateProvider Protocol used by OpportunityPipeline."""

    def __init__(
        self,
        crawlers: list["OfficialStoreCrawler"],
        *,
        collab_store: "CollabOutcomesStore | None" = None,
        top_n: int = 5,
    ) -> None:
        self._crawlers = crawlers
        if collab_store is not None:
            from .collab_similarity_provider import CollabSimilarityProvider as _CSP
            self._collab_similarity: "CollabSimilarityProvider | None" = _CSP(
                collab_store, top_n=top_n
            )
        else:
            self._collab_similarity = None

    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        from market_monitor.official_store_base import ACTIVE_STATUSES
        candidates: list[OpportunityCandidate] = []
        for crawler in self._crawlers:
            try:
                listings = crawler.fetch_listings()
            except Exception:
                logger.exception(
                    "OfficialStoreCandidateProvider: crawler failed store=%s",
                    getattr(crawler, "store_name", "?"),
                )
                continue
            for listing in listings:
                if listing.status not in ACTIVE_STATUSES:
                    continue
                candidate = _listing_to_candidate(
                    listing, collab_similarity=self._collab_similarity
                )
                if candidate:
                    candidates.append(candidate)

        # Sort by heat_score descending, cap to limit
        candidates.sort(key=lambda c: c.heat_score, reverse=True)
        result = candidates[:limit]
        logger.info(
            "OfficialStoreCandidateProvider: discovered candidates=%d / found=%d",
            len(result), len(candidates),
        )
        return result


def _listing_to_candidate(
    listing: "OfficialStoreListing",
    *,
    collab_similarity: "CollabSimilarityProvider | None" = None,
) -> "OpportunityCandidate | None":
    game = _infer_game(listing)
    product_type = _infer_product_type(listing.title)
    heat_score = _STATUS_HEAT.get(listing.status, 0.45)

    search_query = _build_search_query(listing)
    candidate_id = build_candidate_id(
        game=game,
        product_type=product_type,
        title=listing.title,
        search_query=search_query,
        source_url=listing.url,
    )

    reason_parts: list[str] = [f"{listing.store_name}に{listing.status}"]
    if listing.deadline_iso:
        reason_parts.append(f"締切 {listing.deadline_iso[:10]}")
    if listing.price_jpy:
        reason_parts.append(f"定価 ¥{listing.price_jpy:,}")

    metadata: dict[str, object] = {
        "source_store": listing.store_name,
        "listing_status": listing.status,
        "listing_url": listing.url,
    }
    if listing.price_jpy is not None:
        metadata["official_price_jpy"] = listing.price_jpy
    if listing.deadline_iso:
        metadata["deadline_iso"] = listing.deadline_iso
    if listing.open_date_iso:
        metadata["open_date_iso"] = listing.open_date_iso
    if listing.product_code:
        metadata["product_code"] = listing.product_code

    # Attach collab inference if provider is wired in
    if collab_similarity is not None:
        ip_canonical = _infer_ip_canonical(listing.title, game)
        try:
            inference = collab_similarity.infer(ip_canonical, game)
            if inference.n_samples > 0:
                metadata["collab_inference_json"] = json.dumps(
                    _inference_to_compact_dict(inference),
                    ensure_ascii=False,
                )
        except Exception:
            logger.exception(
                "_listing_to_candidate: collab inference failed title=%r", listing.title
            )

    return OpportunityCandidate(
        candidate_id=candidate_id,
        game=game,
        product_type=product_type,
        title=listing.title,
        search_query=search_query,
        heat_score=heat_score,
        reason=", ".join(reason_parts),
        source_kind=SOURCE_KIND,
        source_url=listing.url,
        metadata=metadata,
        created_at=utc_now_iso(),
    )


def _infer_game(listing: "OfficialStoreListing") -> str:
    # Categories are most reliable
    for cat in listing.categories:
        if cat == "union_arena":
            return "union_arena"
        if cat in ("pokemon", "pokemon_tcg"):
            return "pokemon_tcg"

    # Fall back to title keyword scan
    for game, pattern in _GAME_KW.items():
        if pattern.search(listing.title):
            return game

    # Generic TCG fallback
    return "tcg"


def _infer_product_type(title: str) -> str:
    for product_type, pattern in _PRODUCT_TYPE_KW.items():
        if pattern.search(title):
            return product_type
    return normalize_product_type("other")


def _build_search_query(listing: "OfficialStoreListing") -> str:
    """Build a Mercari search query from the listing title.

    Strips store-specific suffixes and extracts the core product name."""
    title = listing.title
    # Remove trailing "1BOX" type suffixes for a cleaner secondary market query
    title = re.sub(r"\s+\d?BOX$", "", title, flags=re.IGNORECASE).strip()
    # Limit to ~60 chars for search queries
    return title[:60]


def _infer_ip_canonical(title: str, game: str) -> str:
    """Strip known TCG product prefixes from title to get a best-effort IP name.

    Returns a lowercase string suitable as ip_canonical for similarity lookup.
    """
    stripped = title.strip()
    for prefix in _TCG_TITLE_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    # Also try a case-insensitive strip for ASCII prefixes
    for prefix in _TCG_TITLE_PREFIXES:
        if stripped.lower().startswith(prefix.lower()) and not stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    # Remove product-type trailing words (BOX / ブースターパック / etc.) for cleaner IP name
    stripped = re.sub(
        r"\s+(?:\d?BOX|ブースターパック|ブースター|拡張パック|スターターデッキ|スターター|プロモ).*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    return stripped.lower()


def _inference_to_compact_dict(inference: object) -> dict:
    """Serialize a CollabInference to a compact JSON-safe dict for metadata storage."""
    # Access attributes via getattr to avoid hard import in this module
    return {
        "n_samples": getattr(inference, "n_samples", 0),
        "mean_profit_pct_180d": getattr(inference, "mean_profit_pct_180d", None),
        "win_rate_180d": getattr(inference, "win_rate_180d", None),
        "best_profit_pct_180d": getattr(inference, "best_profit_pct_180d", None),
        "worst_profit_pct_180d": getattr(inference, "worst_profit_pct_180d", None),
        "top_cases": [
            {
                "ip": getattr(c, "ip_canonical", ""),
                "tcg": getattr(c, "tcg_game", ""),
                "date": getattr(c, "announce_date", "")[:7],
                "p180": getattr(c, "profit_pct_180d", None),
            }
            for c in list(getattr(inference, "similar_cases", []))[:3]
        ],
    }
