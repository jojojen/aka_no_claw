"""Typed research-pipeline contracts and shared budget state (R3.1)."""

from __future__ import annotations

import threading
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

from ..item_condition import ConditionAssessment
from ..web_search import WebSearchResult

SearchFn = Callable[[str, int], tuple[object, ...]]
FetchHtmlFn = Callable[[str], str]
SellerSnapshotLookupFn = Callable[[str], "SellerReputationSnapshot"]
SellerSnapshotFollowupFn = Callable[
    [str, Callable, "ResearchNotifier"],
    None,
]
ActiveMarketSearchFn = Callable[[str, int, int], list[dict[str, object]]]
SoldMarketSearchFn = Callable[[str, int], list[dict[str, object]]]
SoldAverageLookupFn = Callable[[str], float | None]
ShopReferenceFn = Callable[[str, int], "ShopReference | None"]
GameCodeResolverFn = Callable[[str], "str | None"]
# (query, matched_listing_titles) -> None. Records the item's real identity onto
# the yuyutei code cache from titles already fetched — no extra request.
CacheEnricherFn = Callable[[str, tuple[str, ...]], None]
IpHeatLookupFn = Callable[[tuple[str, ...]], dict[str, tuple[object, ...]]]
EntityRecognizerFn = Callable[["ItemData"], "EntityProfile | None"]
AppreciationEnricherFn = Callable[[str, tuple[WebSearchResult, ...]], "str | None"]
# PR3 semantic gate: (reference_title, reference_price, candidates) -> set of kept
# candidate indices, or None when the gate cannot decide (→ fall back to lexical).
SemanticGateFn = Callable[[str, "int | None", list[object]], "set[int] | None"]
ConditionAssessorFn = Callable[[str, "str | None", "Sequence[str]"], "ConditionAssessment | None"]
ResearchStageRunner = Callable[["ResearchJobContext"], str]

_MERCARI_ITEM_PATH_RE = re.compile(r"^/item/(m\d+)/?$", re.IGNORECASE)
_MERCARI_SHOPS_PATH_RE = re.compile(r"^/shops/product/([A-Za-z0-9]+)/?$")
_MERCARI_PROFILE_PATH_RE = re.compile(r"^/user/profile/(\d+)/?$", re.IGNORECASE)
_MERCARI_HOSTS = frozenset({"jp.mercari.com", "www.mercari.com", "mercari.com"})
_TITLE_SUFFIX_RE = re.compile(r"\s+by メルカリ$", re.IGNORECASE)
# Mercari Shops og:title ends with " - <shop name> メルカリ店"; strip it.
# The separator is a spaced hyphen, so internal hyphens (e.g. "K-ON!") are kept.
_SHOPS_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+\S.*?メルカリ店\s*$")
_MERCARI_ORIG_IMAGE_RE = re.compile(
    r"https://static\.mercdn\.net/item/detail/orig/photos/(m\d+)_\d+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'>]+)?",
    re.IGNORECASE,
)
_META_PRICE_RE = re.compile(
    r'<meta[^>]+name=["\']product:price:amount["\'][^>]+content=["\'](\d+)["\']',
    re.IGNORECASE,
)
_GRADED_TITLE_RE = re.compile(r"\b(?:psa|bgs|ars)(?:\s*\d{1,2})?\b|鑑定", re.IGNORECASE)
_GENERIC_PROMO_TOKEN_RE = re.compile(r"\d+|周年|限定|フェス|記念|特典|入場者", re.IGNORECASE)
_REVIEW_WHITESPACE_RE = re.compile(r"\s+")
_MERCARI_ITEM_CONDITIONS = frozenset(
    {
        "新品、未使用",
        "未使用に近い",
        "目立った傷や汚れなし",
        "やや傷や汚れあり",
        "傷や汚れあり",
        "全体的に状態が悪い",
    }
)


class ResearchNotifier(Protocol):
    def send(self, text: str) -> None: ...


class BudgetExhaustedError(RuntimeError):
    """Raised when a /research job tries to exceed its shared Yahoo budget."""


class ResearchCancelledError(RuntimeError):
    """Raised at a cooperative checkpoint when the run's cancel probe fires
    (issue #81). Checkpoints live in the orchestration skeleton only — stage
    starts, heartbeats, and each budgeted search — so every stage inherits
    mid-step cancellation without any per-domain wiring."""

    def __init__(self, message: str = "任務已取消。") -> None:
        super().__init__(message)


@dataclass(slots=True)
class ResearchBudget:
    max_searches: int = 5
    searches_used: int = 0
    # Guards searches_used so stages 3/4/5/7 running on parallel threads can't
    # race the counter (Phase 2 parallelisation).
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def remaining(self) -> int:
        return max(0, self.max_searches - self.searches_used)

    def consume(self) -> None:
        with self._lock:
            if self.searches_used >= self.max_searches:
                raise BudgetExhaustedError(
                    f"Yahoo 搜尋預算已用盡（{self.searches_used}/{self.max_searches}）。"
                )
            self.searches_used += 1


def build_budgeted_search_fn(
    search_fn: SearchFn,
    budget: ResearchBudget,
    cancel_check: Callable[[], bool] | None = None,
) -> SearchFn:
    def budgeted(query: str, limit: int) -> tuple[object, ...]:
        if cancel_check is not None and cancel_check():
            raise ResearchCancelledError()
        budget.consume()
        return search_fn(query, limit)

    return budgeted


@dataclass(frozen=True, slots=True)
class ResearchTarget:
    mode: str
    raw_input: str
    display_text: str
    canonical_url: str | None = None
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class ItemData:
    source_site: str
    item_url: str
    item_id: str
    title: str
    listed_price_jpy: int | None
    description: str
    condition_label: str | None
    seller_id: str | None
    seller_url: str | None
    image_urls: tuple[str, ...]
    fetched_at: str
    source_confidence: float


@dataclass(frozen=True, slots=True)
class EntityProfile:
    """LLM+RAG entity recognition output for M2. ``canonical_query`` is the
    cleaned, correctly-spelled search query (typos/noise removed) used to drive
    sold/active comp retrieval; ``aliases`` are alternate spellings (incl. the
    seller's original) folded into the knowledge DB so future lookups hit."""

    canonical_query: str
    card_name: str | None = None
    series: str | None = None
    character: str | None = None
    rarity: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PriceEvidence:
    source_site: str
    source_url: str
    title: str
    price_jpy: int | None
    sold_status: str
    condition_label: str | None
    shipping_note: str | None
    excluded_reason: str | None
    observed_at: str


@dataclass(frozen=True, slots=True)
class ShopReference:
    """Shop-price reference band (e.g. Yuyu亭): 買取 (what the shop pays — lower
    side) and in-stock 販売 (what the shop charges — upper side). Surfaced as a
    distinct band in the price section, NOT folded into the C2C active median."""

    label: str
    buy_reference: int | None
    sell_reference: int | None
    stock_total: int
    buy_count: int
    sell_count: int
    sample_urls: tuple[str, ...]
    buy_min: int | None = None
    buy_max: int | None = None
    sell_min: int | None = None
    sell_max: int | None = None
    # Verbatim matched-listing titles from the band (card number / rarity / set /
    # box name), threaded through so the yuyutei code cache can record the item's
    # real identity from data already fetched — no extra request.
    sample_titles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SellerReputationSnapshot:
    seller_url: str
    proof_url: str
    proof_id: str | None
    reused: bool
    display_name: str | None
    captured_at: str | None
    total_reviews: int | None
    listing_count: int | None
    followers_count: int | None
    following_count: int | None
    seller_positive: int | None
    seller_negative: int | None
    seller_rate: float | None
    buyer_positive: int | None = None
    buyer_negative: int | None = None
    buyer_rate: float | None = None
    overall_rate: float | None = None
    seller_negative_excerpts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchSectionResult:
    section_name: str
    status: str
    confidence: float
    sample_count: int
    evidence_count: int
    summary: str
    evidence_urls: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class ResearchJobContext:
    raw_input: str
    chat_id: str
    notifier: ResearchNotifier
    budget: ResearchBudget
    search_fn: SearchFn
    target: ResearchTarget | None = None
    item_data: ItemData | None = None
    entity_profile: EntityProfile | None = None
    section_results: list[ResearchSectionResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    active_price_evidence: tuple[PriceEvidence, ...] = ()
    sold_price_evidence: tuple[PriceEvidence, ...] = ()
    sold_average_jpy: float | None = None
    shop_reference: ShopReference | None = None
    appreciation_search_results: tuple[WebSearchResult, ...] = ()
    appreciation_enrichment: str | None = None
    seller_snapshot: SellerReputationSnapshot | None = None
    condition_assessment: "ConditionAssessment | None" = None
    current_stage: int = 0
    current_label: str = ""
    heartbeat_interval_seconds: float = 15.0
    stage_started_monotonic: float = 0.0
    last_heartbeat_monotonic: float = 0.0
    # Set to True when the overall marketplace budget is exhausted before all
    # parallel stages (3/4/5/7) return.  build_research_report appends a note so
    # the user knows the answer is based on partial market data.
    marketplace_timed_out: bool = False
    # Serialises section_results/warnings appends across stages 3/4/5/7 when they
    # run on parallel threads (Phase 2 parallelisation).
    _section_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # Cooperative cancel probe (issue #81): consulted at stage starts, every
    # heartbeat call, and each budgeted search, so a cancel lands mid-stage —
    # not only between stages. None (Telegram path) means "never cancelled".
    cancel_check: Callable[[], bool] | None = None

    def check_cancelled(self) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise ResearchCancelledError()

    def heartbeat(self, note: str = "仍在處理…") -> None:
        # Heartbeats are emitted from inside long stage loops (scrape retries,
        # comp pagination), which makes them natural mid-step cancel points.
        self.check_cancelled()
        now = time.monotonic()
        if self.heartbeat_interval_seconds > 0:
            if self.stage_started_monotonic and now - self.stage_started_monotonic < self.heartbeat_interval_seconds:
                return
            if self.last_heartbeat_monotonic and now - self.last_heartbeat_monotonic < self.heartbeat_interval_seconds:
                return
        self.last_heartbeat_monotonic = now
        self.notifier.send(f"⏳ [{self.current_stage}/7] {self.current_label}：{note}")

    def add_section_result(self, result: ResearchSectionResult) -> None:
        with self._section_lock:
            self.section_results.append(result)
            self.warnings.extend(result.warnings)


@dataclass(frozen=True, slots=True)
class ResearchReport:
    chat_id: str
    mode_label: str
    target_display_text: str
    budget_used: int
    budget_max: int
    item_data: ItemData | None
    seller_snapshot: SellerReputationSnapshot | None
    section_results: tuple[ResearchSectionResult, ...]
    warnings: tuple[str, ...]
    marketplace_timed_out: bool = False
