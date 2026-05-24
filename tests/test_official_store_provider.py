"""Tests for OfficialStoreCandidateProvider (B5)."""

from __future__ import annotations

from openclaw_adapter.official_store_provider import (
    SOURCE_KIND,
    OfficialStoreCandidateProvider,
    _infer_game,
    _infer_product_type,
    _listing_to_candidate,
)
from market_monitor.official_store_base import (
    LOTTERY_OPEN,
    LOTTERY_CLOSED,
    PREORDER_OPEN,
    COMING_SOON,
    OfficialStoreListing,
)


def _listing(**kw) -> OfficialStoreListing:
    defaults = dict(
        store_name="joshin",
        item_key="joshinweb.jp/tcg/ua-csm",
        title="UNION ARENA エクストラブースター チェンソーマン 1BOX",
        url="https://joshinweb.jp/tcg/ua-csm",
        status=LOTTERY_OPEN,
        price_jpy=4180,
        deadline_iso="2026-06-15T23:59:00+09:00",
        open_date_iso="2026-06-01T10:00:00+09:00",
        categories=("tcg", "union_arena"),
    )
    defaults.update(kw)
    return OfficialStoreListing(**defaults)


# ── _infer_game ───────────────────────────────────────────────────────────────


def test_infer_game_from_category_union_arena():
    listing = _listing(categories=("tcg", "union_arena"))
    assert _infer_game(listing) == "union_arena"


def test_infer_game_from_category_pokemon():
    listing = _listing(categories=("pokemon", "tcg"), title="ポケモンカードゲーム スカーレット")
    assert _infer_game(listing) == "pokemon_tcg"


def test_infer_game_from_title_keyword_weiss():
    listing = _listing(categories=("tcg",), title="ヴァイスシュヴァルツ 鬼滅の刃 ブースター")
    assert _infer_game(listing) == "weiss_schwarz"


def test_infer_game_fallback_tcg():
    listing = _listing(categories=(), title="謎のカードゲーム セット")
    assert _infer_game(listing) == "tcg"


# ── _infer_product_type ───────────────────────────────────────────────────────


def test_infer_product_type_sealed_box():
    assert _infer_product_type("UNION ARENA チェンソーマン 1BOX") == "sealed_box"


def test_infer_product_type_booster_pack():
    assert _infer_product_type("ヴァイスシュヴァルツ ブースターパック 鬼滅") == "booster_pack"


def test_infer_product_type_starter_deck():
    assert _infer_product_type("ポケモンカード スターターデッキ 100") == "starter_deck"


def test_infer_product_type_fallback_other():
    assert _infer_product_type("謎の商品") == "other"


# ── _listing_to_candidate ─────────────────────────────────────────────────────


def test_listing_to_candidate_basic():
    candidate = _listing_to_candidate(_listing())
    assert candidate is not None
    assert candidate.game == "union_arena"
    assert candidate.product_type == "sealed_box"
    assert candidate.source_kind == SOURCE_KIND
    assert candidate.heat_score == 0.85  # lottery_open
    assert "joshin" in candidate.reason
    assert candidate.metadata["source_store"] == "joshin"
    assert candidate.metadata["deadline_iso"] == "2026-06-15T23:59:00+09:00"
    assert candidate.metadata["official_price_jpy"] == 4180


def test_listing_to_candidate_preorder_heat():
    listing = _listing(status=PREORDER_OPEN)
    candidate = _listing_to_candidate(listing)
    assert candidate is not None
    assert candidate.heat_score == 0.75


def test_listing_to_candidate_coming_soon_heat():
    listing = _listing(status=COMING_SOON)
    candidate = _listing_to_candidate(listing)
    assert candidate is not None
    assert candidate.heat_score == 0.55


def test_listing_to_candidate_id_stable():
    listing = _listing()
    c1 = _listing_to_candidate(listing)
    c2 = _listing_to_candidate(listing)
    assert c1 is not None and c2 is not None
    assert c1.candidate_id == c2.candidate_id


# ── OfficialStoreCandidateProvider ────────────────────────────────────────────


class _StubCrawler:
    store_name = "stub"
    def __init__(self, listings):
        self._listings = listings
    def fetch_listings(self, *, timeout_seconds=30):
        return self._listings


def test_provider_filters_inactive_statuses():
    listings = [
        _listing(status=LOTTERY_OPEN, item_key="k1"),
        _listing(status=LOTTERY_CLOSED, item_key="k2"),
        _listing(status=PREORDER_OPEN, item_key="k3"),
    ]
    provider = OfficialStoreCandidateProvider(crawlers=[_StubCrawler(listings)])
    candidates = provider.discover(limit=10)
    assert len(candidates) == 2
    statuses = {c.metadata["listing_status"] for c in candidates}
    assert LOTTERY_CLOSED not in statuses


def test_provider_sorts_by_heat_descending():
    # COMING_SOON is not in ACTIVE_STATUSES so it's filtered; only active ones appear
    from market_monitor.official_store_base import AVAILABLE
    listings = [
        _listing(status=AVAILABLE, item_key="k1"),      # heat=0.65
        _listing(status=LOTTERY_OPEN, item_key="k2"),   # heat=0.85
        _listing(status=PREORDER_OPEN, item_key="k3"),  # heat=0.75
    ]
    provider = OfficialStoreCandidateProvider(crawlers=[_StubCrawler(listings)])
    candidates = list(provider.discover(limit=10))
    assert len(candidates) == 3
    assert candidates[0].heat_score >= candidates[1].heat_score >= candidates[2].heat_score


def test_provider_respects_limit():
    listings = [_listing(item_key=f"k{i}", status=LOTTERY_OPEN) for i in range(10)]
    provider = OfficialStoreCandidateProvider(crawlers=[_StubCrawler(listings)])
    candidates = provider.discover(limit=3)
    assert len(candidates) == 3


def test_provider_chains_multiple_crawlers():
    crawlers = [
        _StubCrawler([_listing(store_name="joshin", item_key="j1", status=LOTTERY_OPEN)]),
        _StubCrawler([_listing(store_name="yodobashi", item_key="y1", status=PREORDER_OPEN)]),
    ]
    provider = OfficialStoreCandidateProvider(crawlers=crawlers)
    candidates = provider.discover(limit=10)
    stores = {c.metadata["source_store"] for c in candidates}
    assert "joshin" in stores
    assert "yodobashi" in stores


def test_provider_handles_crawler_exception():
    class _BrokenCrawler:
        store_name = "broken"
        def fetch_listings(self, *, timeout_seconds=30):
            raise RuntimeError("connection refused")

    provider = OfficialStoreCandidateProvider(crawlers=[
        _BrokenCrawler(),
        _StubCrawler([_listing(status=LOTTERY_OPEN)]),
    ])
    candidates = provider.discover(limit=10)
    assert len(candidates) == 1  # broken crawler skipped, good crawler still works
