from __future__ import annotations

import json
import logging
import math
import pathlib
import re
import statistics
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag
from market_monitor import browser_stealth as bs

from .knowledge_db import KnowledgeDatabase, KnowledgeEntry, is_source_id
from .market_title_corpus import record_titles as _record_market_titles
from .reputation_snapshot import SnapshotStillPending
from .scrape_subprocess import run_in_subprocess
from .web_search import WebSearchResult

logger = logging.getLogger(__name__)

# Per-scrape wall-clock budgets (seconds). Each Playwright scrape runs in its
# own killable subprocess; on expiry the process group is SIGKILLed and the
# stage degrades gracefully rather than hanging the whole /research job.
_ACTIVE_SCRAPE_TIMEOUT = 120.0
_SOLD_SCRAPE_TIMEOUT = 90.0
_SOLD_AVG_SCRAPE_TIMEOUT = 75.0
_SHOP_REF_SCRAPE_TIMEOUT = 75.0
_ITEM_HTML_SCRAPE_TIMEOUT = 90.0

# Cross-process yuyu-tei rate-limit state — written by the scrape subprocess when
# it receives a 429, read by the parent before launching the next subprocess so we
# don't naively retry a host that just told us to back off.  In-process circuit
# breaker resets on subprocess exit; this file keeps the signal alive across calls.
_YUYUTEI_CROSS_PROCESS_COOLDOWN_SECS = 300.0


def _yuyutei_cooldown_path() -> pathlib.Path:
    return pathlib.Path(tempfile.gettempdir()) / "openclaw_yuyutei_cooldown"


def _yuyutei_cooldown_remaining() -> float:
    try:
        age = time.time() - _yuyutei_cooldown_path().stat().st_mtime
        return max(0.0, _YUYUTEI_CROSS_PROCESS_COOLDOWN_SECS - age)
    except OSError:
        return 0.0


def _yuyutei_trip_cross_process_cooldown() -> None:
    try:
        _yuyutei_cooldown_path().touch()
    except OSError:
        pass

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
SemanticGateFn = Callable[
    [str, "int | None", "list[CandidateForSemanticRerank]"], "set[int] | None"
]
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


@dataclass(slots=True)
class ResearchBudget:
    max_searches: int = 5
    searches_used: int = 0
    # Guards searches_used so stages 3/4/6 running on parallel threads can't
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


def build_budgeted_search_fn(search_fn: SearchFn, budget: ResearchBudget) -> SearchFn:
    def budgeted(query: str, limit: int) -> tuple[object, ...]:
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
    current_stage: int = 0
    current_label: str = ""
    heartbeat_interval_seconds: float = 15.0
    stage_started_monotonic: float = 0.0
    last_heartbeat_monotonic: float = 0.0
    # Serialises section_results/warnings appends across stages 3/4/6 when they
    # run on parallel threads (Phase 2 parallelisation).
    _section_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def heartbeat(self, note: str = "仍在處理…") -> None:
        now = time.monotonic()
        if self.heartbeat_interval_seconds > 0:
            if self.stage_started_monotonic and now - self.stage_started_monotonic < self.heartbeat_interval_seconds:
                return
            if self.last_heartbeat_monotonic and now - self.last_heartbeat_monotonic < self.heartbeat_interval_seconds:
                return
        self.last_heartbeat_monotonic = now
        self.notifier.send(f"⏳ [{self.current_stage}/6] {self.current_label}：{note}")

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


class _NullResearchNotifier:
    def send(self, text: str) -> None:
        return None


_NOOP_SEARCH_FN: SearchFn = lambda query, limit: ()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_research_target(raw_input: str) -> ResearchTarget:
    cleaned = " ".join((raw_input or "").split()).strip()
    if not cleaned:
        raise ValueError("請提供商品名稱或 Mercari 商品網址。")
    mercari = normalize_mercari_item_url(cleaned)
    if mercari is not None:
        item_id = _extract_mercari_item_id(mercari)
        return ResearchTarget(
            mode="mercari_url",
            raw_input=cleaned,
            display_text=mercari,
            canonical_url=mercari,
            item_id=item_id,
        )
    shops = normalize_mercari_shops_url(cleaned)
    if shops is not None:
        canonical_url, token = shops
        return ResearchTarget(
            mode="mercari_url",
            raw_input=cleaned,
            display_text=canonical_url,
            canonical_url=canonical_url,
            item_id=token,
        )
    return ResearchTarget(mode="text_query", raw_input=cleaned, display_text=cleaned)


def normalize_mercari_item_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").lower()
    if host not in _MERCARI_HOSTS:
        return None
    match = _MERCARI_ITEM_PATH_RE.match(parsed.path or "")
    if not match:
        return None
    canonical_path = f"/item/{match.group(1).lower()}"
    return urlunsplit(("https", "jp.mercari.com", canonical_path, "", ""))


def _extract_mercari_item_id(url: str) -> str | None:
    match = _MERCARI_ITEM_PATH_RE.match(urlsplit(url).path or "")
    return match.group(1).lower() if match else None


def normalize_mercari_shops_url(url: str) -> tuple[str, str] | None:
    """Return (canonical_url, token) for a Mercari Shops product URL, else None.

    Shops pages render price client-side (absent from static HTML), but the
    product name is in og:title — enough to drive entity recognition + market
    search, so we route them through the same mercari_url fetch path.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").lower()
    if host not in _MERCARI_HOSTS:
        return None
    match = _MERCARI_SHOPS_PATH_RE.match(parsed.path or "")
    if not match:
        return None
    token = match.group(1)
    canonical_path = f"/shops/product/{token}"
    canonical_url = urlunsplit(("https", "jp.mercari.com", canonical_path, "", ""))
    return canonical_url, token


class MercariItemAdapter:
    def __init__(self, *, fetch_html_fn: FetchHtmlFn | None = None) -> None:
        self._fetch_html_fn = fetch_html_fn or _fetch_html

    def fetch(self, target: ResearchTarget) -> ItemData:
        if target.canonical_url is None or target.item_id is None:
            raise ValueError("MercariItemAdapter requires a canonical Mercari item URL.")
        html = self._fetch_html_fn(target.canonical_url)
        return self.parse_html(
            html,
            item_url=target.canonical_url,
            item_id=target.item_id,
        )

    def parse_html(self, html: str, *, item_url: str, item_id: str) -> ItemData:
        soup = BeautifulSoup(html, "html.parser")
        product = _extract_jsonld_product(soup)
        fallback_title = ""
        if soup.title:
            fallback_title = _compact_whitespace(
                _clean_item_title(soup.title.get_text(" ", strip=True))
            )
        title = (
            _compact_whitespace(str(product.get("name") or ""))
            or _extract_meta_content(soup, "property", "og:title")
            or fallback_title
        )
        title = _compact_whitespace(_clean_item_title(title))

        description = _compact_whitespace(str(product.get("description") or ""))
        if not description:
            description = _extract_meta_content(soup, "name", "description")

        listed_price = _extract_price_from_product(product)
        if listed_price is None:
            listed_price = _extract_meta_price(html)

        condition_label = _extract_detail_value_text(soup, "商品の状態")
        if not condition_label:
            condition_label = _extract_condition_from_embedded_json(html)
        if not condition_label:
            condition_label = _infer_condition_from_title(title)
        seller_url = _extract_seller_url(soup, base_url=item_url)
        seller_id = _extract_seller_id(seller_url)
        image_urls = _extract_image_urls(soup, product, item_id)
        fetched_at = _utc_now_iso()
        confidence = _score_item_data_confidence(
            title=title,
            listed_price_jpy=listed_price,
            description=description,
            condition_label=condition_label,
            seller_id=seller_id,
            image_urls=image_urls,
        )
        return ItemData(
            source_site="mercari",
            item_url=item_url,
            item_id=item_id,
            title=title,
            listed_price_jpy=listed_price,
            description=description,
            condition_label=condition_label,
            seller_id=seller_id,
            seller_url=seller_url,
            image_urls=image_urls,
            fetched_at=fetched_at,
            source_confidence=confidence,
        )


def _fetch_html(
    url: str,
    *,
    timeout_seconds: int = 15,
    user_agent: str = bs.MAC_CHROME_UA,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Mercari item fetch HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Mercari item fetch failed: {exc.reason}") from exc


def _playwright_page_content_settled(page) -> str:
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            return page.content()
        except Exception as exc:  # noqa: BLE001 — SPA mid-navigation; settle and retry
            last_exc = exc
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
    if last_exc is not None:
        raise last_exc
    return page.content()


def fetch_mercari_item_html_with_playwright(url: str) -> str:
    """Render a Mercari item page with headless Chromium so the JS-injected
    fields (商品の状態, 出品者) appear in the HTML. The plain-HTTP shell omits
    them — Mercari moved to client-side hydration. Same anti-detection launch
    profile reputation_agent uses for item pages. Raises on Playwright/render
    failure so callers can fall back to the cheap static fetch."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = bs.launch_stealth_chromium(p, headless=True, logger=logger)
        try:
            ctx = bs.new_stealth_context(browser)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("body", timeout=15000)
            page.wait_for_timeout(2000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            bs.humanize(page)
            return _playwright_page_content_settled(page)
        finally:
            browser.close()


def build_research_item_fetch_html(*, enable_playwright: bool = True) -> FetchHtmlFn:
    """Item HTML fetcher for /research: render with Playwright first (so the SPA
    condition/seller fields exist), fall back to the plain-HTTP fetch when the
    browser is unavailable or the render fails (still yields title+price)."""

    def _fetch(url: str) -> str:
        if enable_playwright:
            try:
                # Bounded so a wedged Playwright teardown can't hang the job; the
                # daemon thread is abandoned on timeout and we fall back to HTTP.
                html = _run_in_isolated_thread(
                    lambda: fetch_mercari_item_html_with_playwright(url),
                    timeout=_ITEM_HTML_SCRAPE_TIMEOUT,
                )
                if html and "出品者" in html:
                    return html
                logger.info("research item: Playwright shell missing 出品者, using static fetch url=%s", url)
            except Exception:  # noqa: BLE001 — fall back to cheap fetch on any browser error
                logger.warning("research item: Playwright fetch failed, falling back url=%s", url, exc_info=True)
        return _fetch_html(url)

    return _fetch


def _extract_jsonld_product(soup: BeautifulSoup) -> dict[str, object]:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        graph = payload.get("@graph") if isinstance(payload, dict) else None
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
        if isinstance(payload, dict) and payload.get("@type") == "Product":
            return payload
    return {}


def _extract_meta_content(soup: BeautifulSoup, attr_name: str, attr_value: str) -> str:
    tag = soup.find("meta", attrs={attr_name: attr_value})
    if not isinstance(tag, Tag):
        return ""
    return _compact_whitespace(str(tag.get("content") or ""))


def _extract_meta_price(html: str) -> int | None:
    match = _META_PRICE_RE.search(html)
    return int(match.group(1)) if match else None


def _extract_price_from_product(product: dict[str, object]) -> int | None:
    offers = product.get("offers")
    if isinstance(offers, dict):
        raw = offers.get("price")
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str):
            cleaned = raw.replace(",", "").strip()
            if cleaned.isdigit():
                return int(cleaned)
    return None


# Mercari's six fixed 商品の状態 labels — a closed protocol enum, safe to hardcode
# (Rule G permits closed-protocol values). Used to pick the condition out of the
# embedded JSON blob (__NEXT_DATA__/dehydrated state) that the SPA renders from,
# since the static HTML omits the field that JS injects at runtime.
_MERCARI_CONDITION_LABELS = frozenset(
    {
        "新品、未使用",
        "未使用に近い",
        "目立った傷や汚れなし",
        "やや傷や汚れあり",
        "傷や汚れあり",
        "全体的に状態が悪い",
    }
)


def _extract_condition_from_embedded_json(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    blobs: list[str] = []
    next_data = soup.find("script", id="__NEXT_DATA__")
    if isinstance(next_data, Tag):
        raw = next_data.string or next_data.get_text()
        if raw and raw.strip():
            blobs.append(raw)
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string or script.get_text()
        if raw and raw.strip():
            blobs.append(raw)
    for raw in blobs:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        found = _find_condition_label_in_json(payload)
        if found:
            return found
    return None


def _find_condition_label_in_json(node: object) -> str | None:
    if isinstance(node, str):
        return node if node in _MERCARI_CONDITION_LABELS else None
    if isinstance(node, dict):
        # Prefer values under condition-ish keys, but any matching label wins.
        for key, value in node.items():
            if "condition" in str(key).lower():
                if isinstance(value, str) and value in _MERCARI_CONDITION_LABELS:
                    return value
                if isinstance(value, dict):
                    name = value.get("name")
                    if isinstance(name, str) and name in _MERCARI_CONDITION_LABELS:
                        return name
        for value in node.values():
            found = _find_condition_label_in_json(value)
            if found:
                return found
        return None
    if isinstance(node, list):
        for value in node:
            found = _find_condition_label_in_json(value)
            if found:
                return found
    return None


def _extract_detail_value_text(soup: BeautifulSoup, label: str) -> str | None:
    direct = soup.find(attrs={"data-testid": label})
    if isinstance(direct, Tag):
        for child in direct.children:
            if isinstance(child, NavigableString):
                direct_text = _compact_whitespace(str(child))
                if direct_text:
                    return direct_text
        direct_text = _compact_whitespace(direct.get_text(" ", strip=True))
        if direct_text:
            return direct_text
    header = soup.find("h3", string=lambda text: isinstance(text, str) and text.strip() == label)
    if not isinstance(header, Tag):
        return _extract_adjacent_label_value(soup, label)
    row = header.find_parent("div", class_=lambda classes: classes and "merDisplayRow" in classes)
    if not isinstance(row, Tag):
        return _extract_adjacent_label_value(soup, label)
    body = row.find("div", class_=lambda classes: classes and any("body__" in cls for cls in classes))
    if not isinstance(body, Tag):
        return _extract_adjacent_label_value(soup, label)
    direct_parts: list[str] = []
    for child in body.children:
        if isinstance(child, NavigableString):
            text = _compact_whitespace(str(child))
            if text:
                direct_parts.append(text)
        elif isinstance(child, Tag):
            text = child.find(string=True, recursive=False)
            if isinstance(text, NavigableString):
                normalized = _compact_whitespace(str(text))
                if normalized:
                    direct_parts.append(normalized)
                    break
            else:
                normalized = _compact_whitespace(child.get_text(" ", strip=True))
                if normalized:
                    direct_parts.append(normalized)
                    break
    if direct_parts:
        return direct_parts[0]
    text = _compact_whitespace(body.get_text(" ", strip=True))
    if text:
        return text
    return _extract_adjacent_label_value(soup, label)


def _extract_seller_url(soup: BeautifulSoup, *, base_url: str) -> str | None:
    link = soup.select_one('a[data-location="item_details:seller_info"]')
    if isinstance(link, Tag):
        href = str(link.get("href") or "").strip()
        if href:
            return urljoin(base_url, href)

    prioritized: list[str] = []
    fallback: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        path = urlsplit(absolute).path or ""
        if not _MERCARI_PROFILE_PATH_RE.match(path):
            continue
        bucket = prioritized if _anchor_looks_like_seller_link(anchor) else fallback
        if absolute not in bucket:
            bucket.append(absolute)

    if prioritized:
        return prioritized[0]
    if fallback:
        return fallback[0]
    return None


def _extract_seller_id(seller_url: str | None) -> str | None:
    if not seller_url:
        return None
    match = _MERCARI_PROFILE_PATH_RE.match(urlsplit(seller_url).path or "")
    return match.group(1) if match else None


def _infer_condition_from_title(title: str) -> str | None:
    normalized = _compact_whitespace(title)
    if not normalized:
        return None
    if "新品、未使用" in normalized:
        return "新品、未使用"
    if "未使用に近い" in normalized:
        return "未使用に近い"
    if "目立った傷や汚れなし" in normalized:
        return "目立った傷や汚れなし"
    if "やや傷や汚れあり" in normalized:
        return "やや傷や汚れあり"
    if "傷や汚れあり" in normalized:
        return "傷や汚れあり"
    if "全体的に状態が悪い" in normalized:
        return "全体的に状態が悪い"
    if any(token in normalized for token in ("未開封", "新品", "未使用")):
        return "新品、未使用"
    return None


def _extract_adjacent_label_value(soup: BeautifulSoup, label: str) -> str | None:
    for label_tag in soup.find_all(
        lambda tag: isinstance(tag, Tag) and _compact_whitespace(tag.get_text(" ", strip=True)) == label
    ):
        candidate = _extract_value_near_label_tag(label_tag, label)
        if candidate:
            return candidate
    return None


def _extract_value_near_label_tag(label_tag: Tag, label: str) -> str | None:
    condition_hits: list[str] = []
    generic_hits: list[str] = []
    for candidate in _iter_label_neighbor_texts(label_tag):
        if candidate == label:
            continue
        if candidate in _MERCARI_ITEM_CONDITIONS:
            condition_hits.append(candidate)
            continue
        generic_hits.append(candidate)
    if condition_hits:
        return condition_hits[0]
    if generic_hits:
        return generic_hits[0]
    return None


def _iter_label_neighbor_texts(label_tag: Tag):
    seen: set[str] = set()

    def remember(value: str) -> str | None:
        normalized = _compact_whitespace(value)
        if not normalized or normalized in seen:
            return None
        seen.add(normalized)
        return normalized

    current = label_tag
    while isinstance(current, Tag):
        for sibling in current.next_siblings:
            text = _extract_first_meaningful_text(sibling)
            if text:
                remembered = remember(text)
                if remembered:
                    yield remembered
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        for sibling in parent.children:
            if sibling is current:
                continue
            text = _extract_first_meaningful_text(sibling)
            if text:
                remembered = remember(text)
                if remembered:
                    yield remembered
        current = parent


def _extract_first_meaningful_text(node: object) -> str | None:
    if isinstance(node, NavigableString):
        text = _compact_whitespace(str(node))
        return text or None
    if not isinstance(node, Tag):
        return None
    if node.name in {"script", "style"}:
        return None
    text = _compact_whitespace(node.get_text(" ", strip=True))
    return text or None


def _anchor_looks_like_seller_link(anchor: Tag) -> bool:
    anchor_text = _compact_whitespace(anchor.get_text(" ", strip=True))
    if anchor_text in {"出品者", "판매자", "seller"}:
        return True
    for candidate in (anchor, anchor.parent, anchor.find_parent()):
        if not isinstance(candidate, Tag):
            continue
        nearby_text = _compact_whitespace(candidate.get_text(" ", strip=True))
        if "出品者" in nearby_text:
            return True
    return False


def _extract_image_urls(
    soup: BeautifulSoup,
    product: dict[str, object],
    item_id: str,
) -> tuple[str, ...]:
    urls: list[str] = []
    raw_images = product.get("image")
    if isinstance(raw_images, list):
        urls.extend(str(value).strip() for value in raw_images if str(value).strip())
    elif isinstance(raw_images, str) and raw_images.strip():
        urls.append(raw_images.strip())
    og_image = _extract_meta_content(soup, "property", "og:image")
    if og_image:
        urls.append(og_image)
    urls.extend(match.group(0) for match in _MERCARI_ORIG_IMAGE_RE.finditer(str(soup)))

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = _normalize_image_url(url)
        # Shops product images live on a separate CDN with their own ids that
        # don't echo the product token, so the item_id guard can't apply there.
        is_shops_image = "mercari-shops-static.com" in normalized
        if not is_shops_image and item_id not in normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return tuple(deduped)


def _normalize_image_url(url: str) -> str:
    return str(url or "").strip().replace("\\/", "/").rstrip("\\")


def _score_item_data_confidence(
    *,
    title: str,
    listed_price_jpy: int | None,
    description: str,
    condition_label: str | None,
    seller_id: str | None,
    image_urls: tuple[str, ...],
) -> float:
    present = 0
    total = 6
    if title:
        present += 1
    if listed_price_jpy is not None:
        present += 1
    if description:
        present += 1
    if condition_label:
        present += 1
    if seller_id:
        present += 1
    if image_urls:
        present += 1
    return round(max(0.2, present / total), 2)


def _compact_whitespace(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _clean_item_title(text: str) -> str:
    cleaned = _TITLE_SUFFIX_RE.sub("", text or "")
    cleaned = _SHOPS_TITLE_SUFFIX_RE.sub("", cleaned)
    return cleaned


def _run_in_isolated_thread(func: Callable[[], object], *, timeout: float | None = None) -> object:
    result_box: dict[str, object] = {}
    error_box: dict[str, BaseException] = {}
    done = threading.Event()

    def runner() -> None:
        try:
            result_box["value"] = func()
        except BaseException as exc:  # pragma: no cover - re-raised to caller
            error_box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # A bounded wait is the universal backstop: a scrape wedged in Playwright
    # teardown (browser.close() can block forever when headless chromium dies —
    # page-level timeouts don't cover it) must not hang the whole job. The
    # thread is daemon, so abandoning it is safe; callers catch the TimeoutError
    # and degrade that stage gracefully. Subprocess isolation (run_in_subprocess)
    # is the leak-free first line of defence; this guards anything still in-thread.
    if not done.wait(timeout):
        raise TimeoutError(f"isolated scrape exceeded {timeout}s budget")
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


class ResearchCommandService:
    _STAGES: tuple[tuple[int, str], ...] = (
        (0, "解析輸入"),
        (1, "取得商品資料"),
        (2, "實體辨識"),
        (3, "增值潛力分析"),
        (4, "合理市價分析"),
        (5, "流動性分析"),
        (6, "賣家風險分析"),
    )
    _MILESTONE_STAGES: dict[int, str] = {
        1: "已抓到商品頁",
        4: "已完成市場比價",
    }

    def __init__(
        self,
        *,
        notifier_factory: Callable[[str], ResearchNotifier] | None = None,
        search_fn: SearchFn | None = None,
        stage_runners: Sequence[ResearchStageRunner] | None = None,
        max_searches: int = 5,
        item_fetcher: MercariItemAdapter | None = None,
        knowledge_db_path: str | None = None,
        seller_snapshot_lookup_fn: SellerSnapshotLookupFn | None = None,
        seller_snapshot_followup_fn: "SellerSnapshotFollowupFn | None" = None,
        active_market_search_fn: ActiveMarketSearchFn | None = None,
        sold_market_search_fn: SoldMarketSearchFn | None = None,
        sold_average_lookup_fn: SoldAverageLookupFn | None = None,
        shop_reference_fn: ShopReferenceFn | None = None,
        game_code_resolver_fn: GameCodeResolverFn | None = None,
        cache_enricher_fn: "CacheEnricherFn | None" = None,
        ip_heat_lookup_fn: IpHeatLookupFn | None = None,
        entity_recognizer_fn: EntityRecognizerFn | None = None,
        appreciation_enricher_fn: AppreciationEnricherFn | None = None,
        semantic_gate_fn: "SemanticGateFn | None" = None,
        final_formatter: Callable[[ResearchReport], object] | None = None,
        heartbeat_interval_seconds: float = 15.0,
    ) -> None:
        self._notifier_factory = notifier_factory or (lambda chat_id: _NullResearchNotifier())
        self._search_fn = search_fn or _NOOP_SEARCH_FN
        self._max_searches = max_searches
        self._item_fetcher = item_fetcher or MercariItemAdapter()
        self._knowledge_db_path = knowledge_db_path
        self._seller_snapshot_lookup_fn = seller_snapshot_lookup_fn
        self._seller_snapshot_followup_fn = seller_snapshot_followup_fn
        self._active_market_search_fn = active_market_search_fn or _default_active_market_search
        self._sold_market_search_fn = sold_market_search_fn or _default_sold_market_search
        self._sold_average_lookup_fn = sold_average_lookup_fn or _default_sold_average_lookup
        self._shop_reference_fn = shop_reference_fn or build_shop_reference_fn(
            game_code_resolver_fn, cache_enricher_fn
        )
        self._ip_heat_lookup_fn = ip_heat_lookup_fn or (lambda canonicals: {})
        self._entity_recognizer_fn = entity_recognizer_fn
        self._appreciation_enricher_fn = appreciation_enricher_fn
        self._semantic_gate_fn = semantic_gate_fn
        self._final_formatter = final_formatter or format_research_full_report
        self._heartbeat_interval_seconds = max(0.0, heartbeat_interval_seconds)
        self._lock = threading.Lock()
        self._active_chat_ids: set[str] = set()
        self._stage_runners = tuple(stage_runners or self._build_default_stage_runners())
        if len(self._stage_runners) != len(self._STAGES):
            raise ValueError(
                f"stage_runners count mismatch: expected {len(self._STAGES)}, got {len(self._stage_runners)}"
            )

    def run(self, raw_input: str, chat_id: str) -> str:
        chat_key = str(chat_id)
        if not raw_input or not raw_input.strip():
            return "用法：/research <Mercari 商品網址或商品名稱>"
        if not self._try_acquire_chat(chat_key):
            return "同一個聊天室目前已有 /research 在執行中，請等上一個研究完成。"

        notifier = self._notifier_factory(chat_key)
        budget = ResearchBudget(max_searches=self._max_searches)
        budgeted_search_fn = build_budgeted_search_fn(self._search_fn, budget)
        ctx = ResearchJobContext(
            raw_input=raw_input,
            chat_id=chat_key,
            notifier=notifier,
            budget=budget,
            search_fn=budgeted_search_fn,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )
        try:
            notifier.send("⏳ /research 已開始，先抓商品頁與市場資料…")
            stage_by_no = {
                stage_no: (label, runner)
                for (stage_no, label), runner in zip(
                    self._STAGES, self._stage_runners, strict=True
                )
            }

            def _run_tracked(stage_no: int) -> str:
                label, runner = stage_by_no[stage_no]
                ctx.current_stage = stage_no
                ctx.current_label = label
                ctx.stage_started_monotonic = time.monotonic()
                ctx.last_heartbeat_monotonic = 0.0
                note = runner(ctx)
                milestone = self._MILESTONE_STAGES.get(stage_no)
                if milestone:
                    notifier.send(f"✅ {milestone}：{note}")
                return note

            # Stages 0-2 are a true chain: parse → fetch item → entity profile,
            # and stages 3/4/6 all read their outputs. Run them in order first.
            for stage_no in (0, 1, 2):
                _run_tracked(stage_no)

            # Stages 3 (增值潛力), 4 (合理市價) and 6 (賣家風險) are mutually
            # independent — each reads only stage 0-2 outputs and writes disjoint
            # ctx fields. Run them concurrently so a cloud-offloaded appreciation
            # enricher overlaps the local price gate instead of queueing behind it.
            parallel_notes: dict[int, str] = {}
            with ThreadPoolExecutor(
                max_workers=3, thread_name_prefix="research-stage"
            ) as pool:
                futures = {
                    pool.submit(stage_by_no[stage_no][1], ctx): stage_no
                    for stage_no in (3, 4, 6)
                }
                for future in as_completed(futures):
                    parallel_notes[futures[future]] = future.result()
            milestone = self._MILESTONE_STAGES.get(4)
            if milestone:
                notifier.send(f"✅ {milestone}：{parallel_notes.get(4, '')}")

            # Stage 5 (流動性) consumes stage 4's price evidence, so it must run
            # after the parallel batch completes.
            _run_tracked(5)

            report = build_research_report(ctx)
            return self._final_formatter(report)
        finally:
            self._release_chat(chat_key)

    def _try_acquire_chat(self, chat_id: str) -> bool:
        with self._lock:
            if chat_id in self._active_chat_ids:
                return False
            self._active_chat_ids.add(chat_id)
            return True

    def _release_chat(self, chat_id: str) -> None:
        with self._lock:
            self._active_chat_ids.discard(chat_id)

    def _build_default_stage_runners(self) -> tuple[ResearchStageRunner, ...]:
        return (
            self._stage_parse_input,
            self._stage_fetch_item_data,
            self._stage_identify_entities,
            self._stage_appreciation_placeholder,
            self._stage_price_placeholder,
            self._stage_liquidity_placeholder,
            self._stage_seller_placeholder,
        )

    def _stage_parse_input(self, ctx: ResearchJobContext) -> str:
        ctx.target = parse_research_target(ctx.raw_input)
        if ctx.target.mode == "mercari_url":
            return f"已正規化 Mercari 商品網址（{ctx.target.item_id}）"
        return "已辨識為商品名稱研究"

    def _stage_fetch_item_data(self, ctx: ResearchJobContext) -> str:
        assert ctx.target is not None
        if ctx.target.mode != "mercari_url":
            result = ResearchSectionResult(
                section_name="取得商品資料",
                status="unavailable",
                confidence=1.0,
                sample_count=0,
                evidence_count=0,
                summary="名稱模式暫不抓單一商品頁。",
            )
            ctx.add_section_result(result)
            return result.summary
        try:
            item = self._item_fetcher.fetch(ctx.target)
        except Exception as exc:
            message = f"Mercari 商品頁抓取失敗：{exc}"
            result = ResearchSectionResult(
                section_name="取得商品資料",
                status="unavailable",
                confidence=0.0,
                sample_count=0,
                evidence_count=0,
                summary=message,
                evidence_urls=(ctx.target.canonical_url or "",),
                warnings=(
                    message,
                    f"建議跟進：/new 抓取 mercari 商品 {ctx.target.item_id} 的完整欄位與圖片清單",
                ),
            )
            ctx.add_section_result(result)
            return message
        ctx.item_data = item
        warnings: list[str] = []
        status = "ok"
        if item.seller_id is None or item.condition_label is None:
            status = "partial"
            warnings.append("Mercari 頁面部分欄位缺漏，商品資料只有部分可信。")
        result = ResearchSectionResult(
            section_name="取得商品資料",
            status=status,
            confidence=item.source_confidence,
            sample_count=1,
            evidence_count=1 + len(item.image_urls),
            summary=(
                f"已抓到商品頁：{item.title} / ¥{item.listed_price_jpy:,}" if item.listed_price_jpy is not None
                else f"已抓到商品頁：{item.title} / 價格缺失"
            ),
            evidence_urls=(item.item_url, *item.image_urls[:2]),
            warnings=tuple(warnings),
        )
        ctx.add_section_result(result)
        return (
            f"標題「{item.title}」，價格 ¥{item.listed_price_jpy:,}，"
            f"狀態 {item.condition_label or '未知'}，賣家 {item.seller_id or '未知'}"
            if item.listed_price_jpy is not None
            else f"標題「{item.title}」，但未抓到價格"
        )

    def _stage_identify_entities(self, ctx: ResearchJobContext) -> str:
        if ctx.item_data is not None:
            self._persist_item_knowledge(ctx.item_data)
            profile = self._recognize_entity(ctx.item_data)
            if profile is not None:
                ctx.entity_profile = profile
                self._persist_entity_aliases(ctx.item_data, profile)
                identity = " / ".join(
                    part for part in (
                        profile.card_name, profile.series, profile.character, profile.rarity,
                    ) if part
                ) or profile.canonical_query
                summary = (
                    f"已辨識實體：{identity}；canonical 查詢「{profile.canonical_query}」"
                    f"（alias {len(profile.aliases)} 筆）已寫入 knowledge DB。"
                )
                result = ResearchSectionResult(
                    section_name="實體辨識",
                    status="ok",
                    confidence=min(0.88, ctx.item_data.source_confidence + 0.1),
                    sample_count=1,
                    evidence_count=1,
                    summary=summary,
                    evidence_urls=(ctx.item_data.item_url,),
                    warnings=(),
                )
                ctx.add_section_result(result)
                return summary
            summary = "已把商品基礎事實寫入 knowledge DB（origin=research_command）"
            result = ResearchSectionResult(
                section_name="實體辨識",
                status="partial",
                confidence=min(0.85, ctx.item_data.source_confidence),
                sample_count=1,
                evidence_count=1,
                summary=summary,
                evidence_urls=(ctx.item_data.item_url,),
                warnings=("M2 僅寫入商品頁基礎事實，LLM 實體辨識未能定位 canonical 卡名（資料不足或不確定）。",),
            )
            ctx.add_section_result(result)
            return summary
        warning = "沒有商品頁基礎資料可供實體辨識，knowledge DB 寫回略過。"
        result = ResearchSectionResult(
            section_name="實體辨識",
            status="unavailable",
            confidence=0.0,
            sample_count=0,
            evidence_count=0,
            summary=warning,
            warnings=(warning,),
        )
        ctx.add_section_result(result)
        return warning

    def _persist_item_knowledge(self, item: ItemData) -> None:
        if not self._knowledge_db_path:
            return
        summary_parts = [f"Mercari 商品頁資料：{item.title}。"]
        if item.listed_price_jpy is not None:
            summary_parts.append(f"標示價格 ¥{item.listed_price_jpy:,}。")
        if item.condition_label:
            summary_parts.append(f"商品狀態：{item.condition_label}。")
        if item.seller_id:
            summary_parts.append(f"賣家 ID：{item.seller_id}。")
        summary_parts.append(f"參考：{item.item_url}")
        db = KnowledgeDatabase(self._knowledge_db_path)
        entity_canonical = f"{item.source_site}:{item.item_id}"
        aliases = tuple(
            alias for alias in (item.title, item.item_id, item.item_url) if alias
        )
        db.upsert_entry(
            entity_canonical=entity_canonical,
            entity_type="product",
            summary=" ".join(summary_parts),
            source_urls=(item.item_url, *item.image_urls[:2]),
            confidence=min(0.85, item.source_confidence),
            origin="research_command",
            aliases=aliases,
        )

    def _recognize_entity(self, item: ItemData) -> EntityProfile | None:
        if self._entity_recognizer_fn is None:
            return None
        try:
            profile = self._entity_recognizer_fn(item)
        except Exception:  # noqa: BLE001 — recognizer is best-effort; never break the run
            logger.warning("entity recognizer failed for %s", item.item_url, exc_info=True)
            return None
        if profile is None or not (profile.canonical_query or "").strip():
            return None
        return profile

    def _persist_entity_aliases(self, item: ItemData, profile: EntityProfile) -> None:
        if not self._knowledge_db_path:
            return
        entity_canonical = f"{item.source_site}:{item.item_id}"
        candidates = [profile.canonical_query, profile.card_name, *profile.aliases]
        db = KnowledgeDatabase(self._knowledge_db_path)
        for alias in candidates:
            alias = (alias or "").strip()
            if alias:
                db.add_alias(alias, entity_canonical)

    def _stage_appreciation_placeholder(self, ctx: ResearchJobContext) -> str:
        entries = self._lookup_appreciation_entries(ctx)
        heat_by_canonical = self._ip_heat_lookup_fn(tuple(entry.entity_canonical for entry in entries))
        search_results = ()
        if _should_enrich_appreciation(entries, heat_by_canonical):
            search_results = _collect_appreciation_search_results(ctx)
            ctx.appreciation_search_results = search_results
        enrichment = self._enrich_appreciation(_build_price_query(ctx), search_results)
        ctx.appreciation_enrichment = enrichment
        db = (
            KnowledgeDatabase(self._knowledge_db_path)
            if self._knowledge_db_path
            else None
        )
        result = _build_appreciation_section_result(
            query=_build_price_query(ctx),
            entries=entries,
            heat_by_canonical=heat_by_canonical,
            search_results=search_results,
            enrichment=enrichment,
            db=db,
        )
        ctx.add_section_result(result)
        return result.summary

    def _enrich_appreciation(
        self, query: str, search_results: tuple[WebSearchResult, ...]
    ) -> str | None:
        if self._appreciation_enricher_fn is None or not search_results or not query:
            return None
        try:
            summary = self._appreciation_enricher_fn(query, search_results)
        except Exception:  # noqa: BLE001 — enrichment is best-effort; keep the snippet fallback
            logger.warning("appreciation enrichment failed query=%s", query, exc_info=True)
            return None
        summary = (summary or "").strip()
        return summary or None

    def _lookup_appreciation_entries(self, ctx: ResearchJobContext) -> tuple[KnowledgeEntry, ...]:
        if not self._knowledge_db_path:
            return ()
        db = KnowledgeDatabase(self._knowledge_db_path)
        haystacks = (
            _normalize_alias_text(ctx.target.display_text) if ctx.target is not None else "",
            _normalize_alias_text(ctx.item_data.title) if ctx.item_data is not None else "",
            _normalize_alias_text(ctx.item_data.description) if ctx.item_data is not None else "",
        )
        current_product_key = (
            f"{ctx.item_data.source_site}:{ctx.item_data.item_id}" if ctx.item_data is not None else None
        )
        matches: dict[str, int] = {}
        for alias, canonical in db.all_aliases():
            alias_text = _normalize_alias_text(alias)
            if len(alias_text) < 3:
                continue
            if current_product_key and canonical == current_product_key:
                continue
            if any(alias_text in haystack for haystack in haystacks if haystack):
                matches[canonical] = max(matches.get(canonical, 0), len(alias_text))

        ranked = sorted(matches.items(), key=lambda item: (-item[1], item[0]))
        entries: list[KnowledgeEntry] = []
        seen: set[str] = set()
        for canonical, _score in ranked:
            entry = db.get_entry(canonical)
            if entry is None:
                continue
            entries.append(entry)
            seen.add(canonical)
            db.mark_referenced(canonical)
            if len(entries) >= 3:
                break

        # Substring (lexical) under-filled → semantic fallback. Catches cross-
        # language / paraphrase matches the alias scan misses (e.g. product text
        # "藍色牢籠" → KB entry whose alias is "藍色監獄"). Exact matches above
        # keep priority; this only tops up the remaining slots.
        if len(entries) < 3:
            query = " ".join(h for h in haystacks if h)
            for canonical, _sim in db.search_semantic("entry", query, 3):
                if len(entries) >= 3:
                    break
                if canonical in seen or canonical == current_product_key:
                    continue
                entry = db.get_entry(canonical)
                if entry is None:
                    continue
                entries.append(entry)
                seen.add(canonical)
                db.mark_referenced(canonical)
        return tuple(entries)

    def _stage_price_placeholder(self, ctx: ResearchJobContext) -> str:
        query = _build_price_query(ctx)
        if not query:
            summary = "缺少可用的商品名稱，無法進行市價分析。"
            result = ResearchSectionResult(
                section_name="合理市價分析",
                status="unavailable",
                confidence=0.0,
                sample_count=0,
                evidence_count=0,
                summary=summary,
                warnings=(summary,),
            )
            ctx.add_section_result(result)
            return summary

        listed_price = ctx.item_data.listed_price_jpy if ctx.item_data is not None else None
        reference_title = ctx.item_data.title if ctx.item_data is not None and ctx.item_data.title else query
        backend_warnings: list[str] = []
        # Sold first: its average sets the active price cap so a high-value item
        # (no listed price on a bare keyword query) doesn't get its active
        # listings filtered out by the low default cap.
        try:
            sold_raw_all = self._sold_market_search_fn(query, 8)
        except Exception as exc:
            logger.exception("Research sold market search failed query=%s", query)
            sold_raw_all = []
            backend_warnings.append(f"Mercari sold 比價抓取失敗：{exc}")
        sold_raw, sold_dropped = _filter_market_items_with_semantic_gate(
            reference_title=reference_title,
            reference_price=listed_price,
            items=sold_raw_all,
            min_similarity=0.32,
            semantic_gate_fn=self._semantic_gate_fn,
        )
        sold_evidence = tuple(_price_evidence_from_market_item(item, sold_status="sold") for item in sold_raw)
        sold_evidence, sold_outliers = _drop_price_outliers(sold_evidence)
        sold_avg = _average_price_from_evidence(sold_evidence)
        if sold_avg is None:
            try:
                sold_avg = self._sold_average_lookup_fn(query)
            except Exception as exc:
                logger.exception("Research sold average lookup failed query=%s", query)
                sold_avg = None
                backend_warnings.append(f"Mercari sold 均價查詢失敗：{exc}")
        price_cap = _derive_active_price_cap(listed_price, sold_avg)
        try:
            active_raw_all = self._active_market_search_fn(query, price_cap, 8)
        except Exception as exc:
            logger.exception("Research active market search failed query=%s", query)
            active_raw_all = []
            backend_warnings.append(f"Mercari active 比價抓取失敗：{exc}")
        active_raw, active_dropped = _filter_market_items_with_semantic_gate(
            reference_title=reference_title,
            reference_price=listed_price,
            items=active_raw_all,
            min_similarity=0.32,
            semantic_gate_fn=self._semantic_gate_fn,
        )
        # Harvest every comp title we already fetched into the historical title
        # corpus (free byproduct — no extra external queries). Fail-safe inside.
        _record_market_titles(
            [
                str(item.get("title") or "")
                for item in (*sold_raw_all, *active_raw_all)
            ],
            source="research",
        )
        active_evidence = tuple(_price_evidence_from_market_item(item, sold_status="active") for item in active_raw)
        active_evidence, active_outliers = _drop_price_outliers(active_evidence)
        try:
            shop_reference = self._shop_reference_fn(query, price_cap)
        except Exception as exc:
            logger.exception("Research shop reference lookup failed query=%s", query)
            shop_reference = None
            backend_warnings.append(f"店舗參考價抓取失敗：{exc}")
        ctx.active_price_evidence = active_evidence
        ctx.sold_price_evidence = sold_evidence
        ctx.sold_average_jpy = sold_avg
        ctx.shop_reference = shop_reference
        listed_condition_label = (
            _classify_condition_class(ctx.item_data.title)
            if ctx.item_data is not None and ctx.item_data.title
            else None
        )
        result = _build_price_section_result(
            query=query,
            listed_price_jpy=listed_price,
            active_evidence=active_evidence,
            sold_evidence=sold_evidence,
            sold_average_jpy=sold_avg,
            listed_condition_label=listed_condition_label,
            shop_reference=shop_reference,
            active_dropped=active_dropped,
            sold_dropped=sold_dropped,
            active_outliers=active_outliers,
            sold_outliers=sold_outliers,
            backend_warnings=tuple(backend_warnings),
        )
        ctx.add_section_result(result)
        return result.summary

    def _stage_liquidity_placeholder(self, ctx: ResearchJobContext) -> str:
        result = _build_liquidity_section_result(
            query=_build_price_query(ctx),
            active_evidence=ctx.active_price_evidence,
            sold_evidence=ctx.sold_price_evidence,
            sold_average_jpy=ctx.sold_average_jpy,
        )
        ctx.add_section_result(result)
        return result.summary

    def _stage_seller_placeholder(self, ctx: ResearchJobContext) -> str:
        if ctx.target and ctx.target.mode != "mercari_url":
            summary = "名稱模式首版不做賣家風險。"
            result = ResearchSectionResult(
                section_name="賣家風險分析",
                status="unavailable",
                confidence=1.0,
                sample_count=0,
                evidence_count=0,
                summary=summary,
            )
            ctx.add_section_result(result)
            return summary
        if ctx.item_data is None:
            summary = "尚未取得商品頁資料，無法建立 reputation snapshot。"
            result = ResearchSectionResult(
                section_name="賣家風險分析",
                status="unavailable",
                confidence=0.0,
                sample_count=0,
                evidence_count=0,
                summary=summary,
                warnings=(summary,),
            )
            ctx.add_section_result(result)
            return summary
        if ctx.item_data.seller_url is None and _MERCARI_SHOPS_PATH_RE.match(
            urlsplit(ctx.item_data.item_url).path or ""
        ):
            summary = "Mercari Shops 商品頁無個人賣家檔案，不適用賣家風險分析。"
            result = ResearchSectionResult(
                section_name="賣家風險分析",
                status="unavailable",
                confidence=1.0,
                sample_count=0,
                evidence_count=0,
                summary=summary,
            )
            ctx.add_section_result(result)
            return summary
        snapshot_query_url = ctx.item_data.seller_url or ctx.item_data.item_url
        if self._seller_snapshot_lookup_fn is None:
            summary = f"已抓到賣家 ID {ctx.item_data.seller_id or '未知'}，但 reputation snapshot 未啟用。"
            result = ResearchSectionResult(
                section_name="賣家風險分析",
                status="partial",
                confidence=0.2,
                sample_count=1 if ctx.item_data.seller_id else 0,
                evidence_count=1,
                summary=summary,
                evidence_urls=(snapshot_query_url,),
                warnings=("賣家 snapshot adapter 尚未注入；可單獨執行 /snapshot 驗證。",),
            )
            ctx.add_section_result(result)
            return summary

        try:
            snapshot = self._seller_snapshot_lookup_fn(snapshot_query_url)
        except SnapshotStillPending as exc:
            if self._seller_snapshot_followup_fn is not None:
                self._seller_snapshot_followup_fn(
                    snapshot_query_url, exc.poll_fn, ctx.notifier
                )
            summary = (
                f"賣家快照處理中（Mercari 評價頁載入慢，job={exc.job_id}），"
                "完成後自動補送結果。"
            )
            result = ResearchSectionResult(
                section_name="賣家風險分析",
                status="partial",
                confidence=0.2,
                sample_count=0,
                evidence_count=1,
                summary=summary,
                evidence_urls=(snapshot_query_url,),
                warnings=(
                    summary,
                    f"建議跟進：/snapshot {snapshot_query_url}",
                ),
            )
            ctx.add_section_result(result)
            return summary
        except Exception as exc:
            summary = f"賣家 reputation snapshot 失敗：{exc}"
            result = ResearchSectionResult(
                section_name="賣家風險分析",
                status="partial",
                confidence=0.2,
                sample_count=1 if ctx.item_data.seller_id else 0,
                evidence_count=1,
                summary=summary,
                evidence_urls=(snapshot_query_url,),
                warnings=(
                    summary,
                    f"建議跟進：/snapshot {snapshot_query_url}",
                ),
            )
            ctx.add_section_result(result)
            return summary

        ctx.seller_snapshot = snapshot
        result = _build_seller_snapshot_section_result(snapshot)
        ctx.add_section_result(result)
        return result.summary

def build_research_handler(
    *,
    notifier_factory: Callable[[str], ResearchNotifier] | None = None,
    search_fn: SearchFn | None = None,
    stage_runners: Sequence[ResearchStageRunner] | None = None,
    max_searches: int = 5,
    item_fetcher: MercariItemAdapter | None = None,
    knowledge_db_path: str | None = None,
    seller_snapshot_lookup_fn: SellerSnapshotLookupFn | None = None,
    seller_snapshot_followup_fn: "SellerSnapshotFollowupFn | None" = None,
    active_market_search_fn: ActiveMarketSearchFn | None = None,
    sold_market_search_fn: SoldMarketSearchFn | None = None,
    sold_average_lookup_fn: SoldAverageLookupFn | None = None,
    shop_reference_fn: ShopReferenceFn | None = None,
    game_code_resolver_fn: GameCodeResolverFn | None = None,
    cache_enricher_fn: "CacheEnricherFn | None" = None,
    ip_heat_lookup_fn: IpHeatLookupFn | None = None,
    entity_recognizer_fn: EntityRecognizerFn | None = None,
    appreciation_enricher_fn: AppreciationEnricherFn | None = None,
    semantic_gate_fn: "SemanticGateFn | None" = None,
    final_formatter: Callable[[ResearchReport], object] | None = None,
    heartbeat_interval_seconds: float = 15.0,
) -> Callable[[str, str], object]:
    service = ResearchCommandService(
        notifier_factory=notifier_factory,
        search_fn=search_fn,
        stage_runners=stage_runners,
        max_searches=max_searches,
        item_fetcher=item_fetcher,
        knowledge_db_path=knowledge_db_path,
        seller_snapshot_lookup_fn=seller_snapshot_lookup_fn,
        seller_snapshot_followup_fn=seller_snapshot_followup_fn,
        active_market_search_fn=active_market_search_fn,
        sold_market_search_fn=sold_market_search_fn,
        sold_average_lookup_fn=sold_average_lookup_fn,
        shop_reference_fn=shop_reference_fn,
        game_code_resolver_fn=game_code_resolver_fn,
        cache_enricher_fn=cache_enricher_fn,
        ip_heat_lookup_fn=ip_heat_lookup_fn,
        entity_recognizer_fn=entity_recognizer_fn,
        appreciation_enricher_fn=appreciation_enricher_fn,
        semantic_gate_fn=semantic_gate_fn,
        final_formatter=final_formatter,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    return service.run


# Canonical report ordering. Stages 3/4/6 may complete out of order under Phase 2
# parallelisation, so section_results is sorted by this map before formatting to
# keep the report deterministic regardless of thread finish order. Unknown names
# sort to the end, preserving their relative insertion order (stable sort).
_SECTION_ORDER: dict[str, int] = {
    "取得商品資料": 1,
    "實體辨識": 2,
    "增值潛力分析": 3,
    "合理市價分析": 4,
    "流動性分析": 5,
    "賣家風險分析": 6,
}


def build_research_report(ctx: ResearchJobContext) -> ResearchReport:
    assert ctx.target is not None
    mode_label = "Mercari 商品網址" if ctx.target.mode == "mercari_url" else "商品名稱"
    ordered_sections = sorted(
        ctx.section_results,
        key=lambda result: _SECTION_ORDER.get(result.section_name, len(_SECTION_ORDER) + 1),
    )
    return ResearchReport(
        chat_id=ctx.chat_id,
        mode_label=mode_label,
        target_display_text=ctx.target.display_text,
        budget_used=ctx.budget.searches_used,
        budget_max=ctx.budget.max_searches,
        item_data=ctx.item_data,
        seller_snapshot=ctx.seller_snapshot,
        section_results=tuple(ordered_sections),
        warnings=tuple(dict.fromkeys(ctx.warnings)),
    )


def format_research_full_report(report: ResearchReport) -> str:
    lines = [
        "龍蝦 /research 已完成目前可用流程。",
        f"研究模式：{report.mode_label}",
        f"研究目標：{report.target_display_text}",
        f"搜尋預算：{report.budget_used}/{report.budget_max}",
    ]
    if report.item_data is not None:
        item = report.item_data
        price_text = f"¥{item.listed_price_jpy:,}" if item.listed_price_jpy is not None else "未知"
        seller_text = _resolve_report_seller_label(report)
        lines.append(
            f"商品頁資料：{item.title} / {price_text} / 狀態 {item.condition_label or '未知'} / "
            f"賣家 {seller_text} / 圖片 {len(item.image_urls)} 張"
        )
    lines.append("")
    lines.append("各節結果：")
    for result in report.section_results:
        lines.append(
            f"- {result.section_name} [{result.status}] "
            f"confidence={result.confidence:.2f} sample={result.sample_count}: {result.summary}"
        )
        if result.evidence_urls:
            lines.extend(f"  source: {url}" for url in result.evidence_urls[:4])
    if report.warnings:
        lines.append("")
        lines.append("Warnings：")
        lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines)


def format_research_compact_report(report: ResearchReport) -> str:
    item = report.item_data
    price_text = "未知"
    title_text = report.target_display_text
    condition_text = "未知"
    seller_text = _resolve_report_seller_label(report)
    if item is not None:
        title_text = item.title or title_text
        if item.listed_price_jpy is not None:
            price_text = f"¥{item.listed_price_jpy:,}"
        condition_text = item.condition_label or "未知"
    lines = [
        "/research 摘要",
        f"商品：{title_text}",
        f"開價：{price_text}",
        f"狀態：{condition_text}",
        f"賣家：{seller_text}",
        "",
        "重點：",
    ]
    for bullet in _build_compact_report_bullets(report):
        lines.append(f"- {bullet}")
    return "\n".join(lines)


def format_research_detail_report(report: ResearchReport, *, view: str) -> str:
    normalized = (view or "summary").strip().lower()
    if normalized == "price":
        return _format_research_price_detail(report)
    if normalized == "seller":
        return _format_research_seller_detail(report)
    if normalized == "sources":
        return _format_research_sources_detail(report)
    if normalized == "warnings":
        return _format_research_warnings_detail(report)
    return format_research_compact_report(report)


def _build_compact_report_bullets(report: ResearchReport) -> tuple[str, ...]:
    bullets: list[str] = []
    price = _find_section_result(report, "合理市價分析")
    liquidity = _find_section_result(report, "流動性分析")
    seller = _find_section_result(report, "賣家風險分析")
    appreciation = _find_section_result(report, "增值潛力分析")
    if price is not None:
        bullets.append("市價：" + _compact_price_summary(price))
    if liquidity is not None:
        bullets.append("流動性：" + _compact_liquidity_summary(liquidity))
    if seller is not None:
        bullets.append("賣家：" + _compact_seller_summary(seller))
    if appreciation is not None and appreciation.status != "unavailable":
        bullets.append("增值：" + _compact_appreciation_summary(appreciation))
    focus_warnings = _select_compact_warnings(report.warnings)
    if focus_warnings:
        bullets.append("注意：" + " / ".join(focus_warnings))
    if not bullets:
        bullets.append("目前只取得基礎商品資料，尚無更多研究結論。")
    return tuple(bullets[:5])


def _select_compact_warnings(warnings: Sequence[str]) -> tuple[str, ...]:
    picked: list[str] = []
    for warning in warnings:
        normalized = _compact_warning_label(warning)
        if normalized:
            picked.append(normalized)
        if len(picked) >= 2:
            break
    return tuple(picked)


def _compact_warning_label(warning: str) -> str | None:
    normalized = _compact_whitespace(warning)
    if "商品資料只有部分可信" in normalized or "欄位缺漏" in normalized:
        return "商品頁欄位仍有缺漏"
    if "sold 樣本少於" in normalized or "active 樣本少於" in normalized:
        return "市價樣本偏少"
    if "snapshot 失敗" in normalized:
        return "賣家快照暫時失敗"
    if "賣家評價樣本偏少" in normalized:
        return "賣家評價樣本偏少"
    if "店舗參考價" in normalized:
        return "遊々亭：無法取得店舗參考"
    return None


def _compact_price_summary(result: ResearchSectionResult) -> str:
    parts = [part for part in result.summary.split("；") if part]
    selected: list[str] = []
    for part in parts:
        text = _compact_whitespace(part)
        if text.startswith("目前開價"):
            selected.append(text)
        elif "均價約" in text and "sold" in text:
            selected.append(text)
        elif text.startswith("active 樣本") and len(selected) < 2:
            selected.append(text)
        elif text.startswith("遊々亭参考："):
            selected.append(text)
    if not selected:
        selected = [_truncate_research_text(result.summary, 68)]
    return _truncate_research_text("；".join(selected), 76)


def _compact_liquidity_summary(result: ResearchSectionResult) -> str:
    parts = [_compact_whitespace(part) for part in result.summary.split("；") if part]
    if not parts:
        return _truncate_research_text(result.summary, 68)
    selected = parts[:2]
    return _truncate_research_text("；".join(selected), 76)


def _compact_seller_summary(result: ResearchSectionResult) -> str:
    parts = [_compact_whitespace(part) for part in result.summary.split("；") if part]
    for part in parts:
        if "快照顯示" in part or "快照資料不足" in part or "snapshot 失敗" in part:
            return _truncate_research_text(part, 76)
    if parts:
        return _truncate_research_text(parts[0], 76)  # verdict is always first
    return _truncate_research_text(result.summary, 68)


def _compact_appreciation_summary(result: ResearchSectionResult) -> str:
    parts = [_compact_whitespace(part) for part in result.summary.split("。") if part]
    selected: list[str] = []
    for part in parts:
        if part.startswith("命中知識庫") or part.startswith("外部補證"):
            selected.append(part)
    if not selected:
        selected = [_truncate_research_text(result.summary, 68)]
    return _truncate_research_text("。".join(selected), 76)


def _format_research_price_detail(report: ResearchReport) -> str:
    lines = _detail_header(report, "市價細節")
    price = _find_section_result(report, "合理市價分析")
    liquidity = _find_section_result(report, "流動性分析")
    if price is not None:
        lines.extend(_render_detail_section(price))
    if liquidity is not None:
        if price is not None:
            lines.append("")
        lines.extend(_render_detail_section(liquidity))
    warnings = _collect_section_warnings((price, liquidity))
    if warnings:
        lines.append("")
        lines.append("提醒：")
        lines.extend(f"- {warning}" for warning in warnings)
    urls = _collect_section_urls((price, liquidity))
    if urls:
        lines.append("")
        lines.append("來源：")
        lines.extend(f"- {url}" for url in urls)
    return "\n".join(lines)


def _format_research_seller_detail(report: ResearchReport) -> str:
    lines = _detail_header(report, "賣家細節")
    seller = _find_section_result(report, "賣家風險分析")
    if seller is not None:
        lines.extend(_render_detail_section(seller))
    else:
        lines.append("目前沒有賣家風險資料。")
    warnings = _collect_section_warnings((seller,))
    if warnings:
        lines.append("")
        lines.append("提醒：")
        lines.extend(f"- {warning}" for warning in warnings)
    urls = _collect_section_urls((seller,))
    if urls:
        lines.append("")
        lines.append("來源：")
        lines.extend(f"- {url}" for url in urls)
    return "\n".join(lines)


def _format_research_sources_detail(report: ResearchReport) -> str:
    lines = _detail_header(report, "來源")
    any_source = False
    for result in report.section_results:
        urls = tuple(dict.fromkeys(url for url in result.evidence_urls if url))
        if not urls:
            continue
        any_source = True
        lines.append(f"{result.section_name}：")
        lines.extend(f"- {url}" for url in urls[:6])
    if not any_source:
        lines.append("目前沒有額外來源可顯示。")
    return "\n".join(lines)


def _format_research_warnings_detail(report: ResearchReport) -> str:
    lines = _detail_header(report, "警告")
    if not report.warnings:
        lines.append("目前沒有額外警告。")
        return "\n".join(lines)
    lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines)


def _detail_header(report: ResearchReport, title: str) -> list[str]:
    lines = [f"/research {title}"]
    if report.item_data is not None:
        item = report.item_data
        price_text = f"¥{item.listed_price_jpy:,}" if item.listed_price_jpy is not None else "未知"
        lines.append(f"商品：{item.title}")
        lines.append(
            f"開價：{price_text} / 狀態：{item.condition_label or '未知'} / 賣家：{_resolve_report_seller_label(report)}"
        )
    else:
        lines.append(f"研究目標：{report.target_display_text}")
    lines.append("")
    return lines


def _render_detail_section(result: ResearchSectionResult) -> list[str]:
    # One claim per line so the conclusion-first ordering is actually legible,
    # instead of a single 「；」-joined wall of text. Parts already prefixed with
    # 「・」 are sub-breakdowns (新品／中古) and get nested deeper under their parent.
    lines = [f"{result.section_name} [{result.status}]"]
    parts = [part.strip() for part in result.summary.split("；") if part.strip()]
    if not parts:
        return lines
    for part in parts:
        if part.startswith("・"):
            lines.append(f"      {part}")
        else:
            lines.append(f"  ‣ {part}")
    return lines


def _collect_section_urls(results: Sequence[ResearchSectionResult | None]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for result in results:
        if result is None:
            continue
        for url in result.evidence_urls:
            if url:
                seen[url] = None
    return tuple(seen)[:8]


def _collect_section_warnings(results: Sequence[ResearchSectionResult | None]) -> tuple[str, ...]:
    warnings: list[str] = []
    for result in results:
        if result is None:
            continue
        for warning in result.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return tuple(warnings)


def _find_section_result(report: ResearchReport, section_name: str) -> ResearchSectionResult | None:
    for result in report.section_results:
        if result.section_name == section_name:
            return result
    return None


def _truncate_research_text(text: str, limit: int) -> str:
    normalized = _compact_whitespace(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _resolve_report_seller_label(report: ResearchReport) -> str:
    item = report.item_data
    if item is not None:
        if item.seller_id:
            return item.seller_id
        if item.seller_url:
            extracted = _extract_seller_id(item.seller_url)
            if extracted:
                return extracted
    snapshot = report.seller_snapshot
    if snapshot is not None:
        if snapshot.display_name:
            return snapshot.display_name
        extracted = _extract_seller_id(snapshot.seller_url)
        if extracted:
            return extracted
        if snapshot.seller_url:
            return snapshot.seller_url
    return "未知"


def _build_seller_snapshot_section_result(snapshot: SellerReputationSnapshot) -> ResearchSectionResult:
    sample_count = 0
    if snapshot.seller_positive is not None or snapshot.seller_negative is not None:
        sample_count = int(snapshot.seller_positive or 0) + int(snapshot.seller_negative or 0)
    elif snapshot.total_reviews is not None:
        sample_count = snapshot.total_reviews

    evidence_urls = tuple(url for url in (snapshot.seller_url, snapshot.proof_url) if url)
    warnings: list[str] = []
    status = "ok"

    meta_bits: list[str] = []
    if snapshot.display_name:
        meta_bits.append(f"賣家 {snapshot.display_name}")
    if snapshot.total_reviews is not None:
        meta_bits.append(f"總評價 {snapshot.total_reviews}")
    if snapshot.listing_count is not None:
        meta_bits.append(f"刊登 {snapshot.listing_count}")

    seller_bits: list[str] = []
    if snapshot.seller_positive is not None:
        seller_bits.append(f"好評 {snapshot.seller_positive}")
    if snapshot.seller_negative is not None:
        seller_bits.append(f"差評 {snapshot.seller_negative}")
    if snapshot.seller_rate is not None:
        seller_bits.append(f"好評率 {snapshot.seller_rate:.1f}%")

    risk_text = "快照資料不足，需人工檢查 proof。"
    if snapshot.seller_rate is None:
        status = "partial"
        warnings.append("reputation snapshot 缺少賣家面向好評率，只能提供部分統計。")
    else:
        negative = int(snapshot.seller_negative or 0)
        if snapshot.seller_rate < 90 or negative >= 5:
            risk_text = "快照顯示賣家風險偏高。"
        elif snapshot.seller_rate < 98 or negative >= 1:
            risk_text = "快照顯示賣家風險中等，建議人工查看差評內容。"
        else:
            risk_text = "快照顯示賣家風險偏低。"

    if sample_count and sample_count < 10:
        if status == "ok":
            status = "partial"
        warnings.append("賣家評價樣本偏少，風險判讀可信度有限。")

    negative_review_summary, negative_review_warning = _summarize_negative_reviews(
        snapshot.seller_negative_excerpts
    )
    if negative_review_warning:
        warnings.append(negative_review_warning)

    confidence = 0.35
    if snapshot.proof_url:
        confidence += 0.15
    if snapshot.total_reviews is not None:
        confidence += 0.1
    if snapshot.seller_rate is not None:
        confidence += 0.2
    if sample_count >= 20:
        confidence += 0.1
    elif sample_count >= 5:
        confidence += 0.05
    if snapshot.display_name:
        confidence += 0.05
    confidence = round(min(0.9, confidence), 2)

    # verdict first so summary reads: risk → stats → meta
    summary_parts: list[str] = [risk_text]
    if negative_review_summary:
        summary_parts.append(negative_review_summary)
    if meta_bits:
        summary_parts.append(" / ".join(meta_bits))
    if seller_bits:
        summary_parts.append("身為賣家：" + " / ".join(seller_bits))
    if snapshot.captured_at:
        summary_parts.append(f"快照時間 {snapshot.captured_at}")

    return ResearchSectionResult(
        section_name="賣家風險分析",
        status=status,
        confidence=confidence,
        sample_count=sample_count,
        evidence_count=len(evidence_urls),
        summary="；".join(summary_parts),
        evidence_urls=evidence_urls,
        warnings=tuple(warnings),
    )


def _summarize_negative_reviews(excerpts: Sequence[str]) -> tuple[str | None, str | None]:
    cleaned = _normalize_negative_review_excerpts(excerpts)
    if not cleaned:
        return None, None

    theme_order = (
        "発送遲延",
        "商品狀態落差",
        "梱包問題",
        "溝通回覆問題",
        "售後爭議",
    )
    theme_counts = {name: 0 for name in theme_order}
    for excerpt in cleaned:
        lower = excerpt.lower()
        if any(token in lower for token in ("発送", "到着", "届", "遅")):
            theme_counts["発送遲延"] += 1
        if any(token in lower for token in ("状態", "説明", "写真", "傷", "汚れ", "破損", "欠品")):
            theme_counts["商品狀態落差"] += 1
        if any(token in lower for token in ("梱包", "箱", "封筒", "折れ", "凹", "潰", "濡")):
            theme_counts["梱包問題"] += 1
        if any(token in lower for token in ("連絡", "返信", "対応", "メッセージ")):
            theme_counts["溝通回覆問題"] += 1
        if any(token in lower for token in ("キャンセル", "返金", "受取", "評価")):
            theme_counts["售後爭議"] += 1

    ranked_themes = sorted(
        (name for name, count in theme_counts.items() if count > 0),
        key=lambda name: (-theme_counts[name], theme_order.index(name)),
    )
    if ranked_themes:
        summary = f"差評重點：{' / '.join(ranked_themes[:3])}。"
    else:
        summary = "已有具體差評內容，建議人工查看 proof 原文。"

    quoted = " / ".join(f"「{_shorten_review_excerpt(excerpt)}」" for excerpt in cleaned[:2])
    warning = f"最近差評例：{quoted}"
    return summary, warning


def _normalize_negative_review_excerpts(excerpts: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for excerpt in excerpts:
        text = _REVIEW_WHITESPACE_RE.sub(" ", str(excerpt or "")).strip(" \n\t;；")
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def _shorten_review_excerpt(text: str, limit: int = 34) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _resolve_evidence_url(ref: str, db: "KnowledgeDatabase | None") -> str:
    """Resolve a stored source ref to a usable URL.

    Source registry ids (``S<n>``) resolve to their canonical URL so /research
    evidence shows a real link, not the opaque ``S1`` token. Legacy raw URLs
    pass through unchanged."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    if db is not None and is_source_id(ref):
        rec = db.get_source(ref)
        if rec is not None:
            return rec.canonical_url
    return ref


def _build_appreciation_section_result(
    *,
    query: str,
    entries: Sequence[KnowledgeEntry],
    heat_by_canonical: dict[str, tuple[object, ...]],
    search_results: Sequence[WebSearchResult],
    enrichment: str | None = None,
    db: "KnowledgeDatabase | None" = None,
) -> ResearchSectionResult:
    enrichment = (enrichment or "").strip() or None
    if not entries:
        summary = f"查詢「{query}」目前只拿到商品頁事實，尚未命中可用的 IP / 作者知識。"
        if enrichment:
            summary += " web 催化劑摘要：" + enrichment
        elif search_results:
            rendered = _render_appreciation_search_results(search_results)
            summary += " " + rendered
        if enrichment:
            no_entry_warning = "增值潛力尚未命中 knowledge DB 既有 entity；判讀以 web 催化劑摘要為主、信心有限。"
        else:
            no_entry_warning = "增值潛力尚未命中 knowledge DB 既有 entity；目前只提供 search snippet 級 evidence。"
        return ResearchSectionResult(
            section_name="增值潛力分析",
            status="partial" if (search_results or enrichment) else "unavailable",
            confidence=(0.3 if enrichment else 0.2) if search_results else 0.1,
            sample_count=0,
            evidence_count=len(search_results),
            summary=summary,
            evidence_urls=tuple(result.url for result in search_results[:4]),
            warnings=(no_entry_warning,),
        )

    evidence_urls: list[str] = []
    matched_labels: list[str] = []
    summary_parts: list[str] = []
    warnings: list[str] = []
    heat_lines: list[str] = []
    heat_hit = False

    for entry in entries:
        matched_labels.append(f"{entry.entity_canonical}({entry.entity_type})")
        evidence_urls.extend(
            url for url in (_resolve_evidence_url(r, db) for r in entry.source_urls[:2]) if url
        )
        summary_parts.append(_summarize_knowledge_entry(entry))
        signals = tuple(heat_by_canonical.get(entry.entity_canonical) or ())
        if signals:
            rendered = _render_heat_summary(entry.entity_canonical, signals)
            if rendered:
                heat_lines.append(rendered)
                heat_hit = True

    summary = f"命中知識庫 {len(entries)} 筆：{'、'.join(matched_labels)}。"
    if heat_lines:
        summary += " " + " ".join(heat_lines)
    summary += " " + " ".join(summary_parts)
    if enrichment:
        summary += " web 催化劑摘要：" + enrichment
    elif search_results:
        summary += " " + _render_appreciation_search_results(search_results)

    status = "ok" if heat_hit else "partial"
    if not heat_hit:
        warnings.append("尚未命中 IP heat 訊號；目前僅能根據既有知識摘要做弱判讀。")
    if enrichment:
        pass  # page fetch + LLM catalyst summary done — no enrichment-gap warning
    elif search_results:
        warnings.append("外部搜尋結果目前只使用 snippet，尚未做頁面抓取與 LLM 催化劑摘要。")
    else:
        warnings.append("作者軌跡 / 再販 / 官方催化劑的 web enrichment 尚未接入。")
    confidence = (
        0.35
        + min(0.25, 0.1 * len(entries))
        + (0.15 if heat_hit else 0.0)
        + (0.1 if enrichment else 0.05 if search_results else 0.0)
    )
    return ResearchSectionResult(
        section_name="增值潛力分析",
        status=status,
        confidence=round(min(0.85, confidence), 2),
        sample_count=len(entries),
        evidence_count=len(tuple(dict.fromkeys([*evidence_urls, *(result.url for result in search_results)]))),
        summary=summary.strip(),
        evidence_urls=tuple(dict.fromkeys([*evidence_urls, *(result.url for result in search_results)]))[:4],
        warnings=tuple(warnings),
    )


def _summarize_knowledge_entry(entry: KnowledgeEntry) -> str:
    compact = _compact_whitespace(entry.summary)
    if len(compact) > 90:
        compact = compact[:89].rstrip() + "…"
    return f"{entry.entity_canonical}：{compact}"


def _render_heat_summary(canonical: str, signals: Sequence[object]) -> str | None:
    rendered_bits: list[str] = []
    peak_percentile: float | None = None
    for signal in signals:
        source = getattr(signal, "source", None)
        percentile = getattr(signal, "percentile", None)
        if source is None or percentile is None:
            continue
        percentile_value = float(percentile)
        rendered_bits.append(f"{source} {percentile_value:.0f}pct")
        peak_percentile = percentile_value if peak_percentile is None else max(peak_percentile, percentile_value)
    if not rendered_bits:
        return None
    heat_label = "熱度高" if (peak_percentile or 0.0) >= 80 else "熱度中等" if (peak_percentile or 0.0) >= 60 else "熱度普通"
    return f"{canonical} 近期 {heat_label}（{' / '.join(rendered_bits)}）。"


def _normalize_alias_text(text: str) -> str:
    return _compact_whitespace(text).lower()


def _should_enrich_appreciation(
    entries: Sequence[KnowledgeEntry],
    heat_by_canonical: dict[str, tuple[object, ...]],
) -> bool:
    return not entries or not heat_by_canonical


def _collect_appreciation_search_results(ctx: ResearchJobContext) -> tuple[WebSearchResult, ...]:
    query = _build_price_query(ctx)
    if not query:
        return ()
    try:
        raw_results = tuple(ctx.search_fn(query, 3))
    except BudgetExhaustedError:
        return ()
    except Exception:
        logger.exception("Research appreciation web search failed query=%s", query)
        return ()
    return _filter_appreciation_search_results(raw_results)


def _filter_appreciation_search_results(results: Sequence[WebSearchResult]) -> tuple[WebSearchResult, ...]:
    filtered: list[WebSearchResult] = []
    seen_urls: set[str] = set()
    for result in results:
        url = str(result.url or "").strip()
        if not url or url in seen_urls:
            continue
        host = (urlsplit(url).netloc or "").lower()
        if host in _MERCARI_HOSTS:
            continue
        seen_urls.add(url)
        filtered.append(result)
        if len(filtered) >= 3:
            break
    return tuple(filtered)


def _render_appreciation_search_results(results: Sequence[WebSearchResult]) -> str:
    if not results:
        return ""
    labels = []
    for result in results[:2]:
        title = _compact_whitespace(result.title)
        if len(title) > 42:
            title = title[:41].rstrip() + "…"
        labels.append(title)
    return f"外部補證 {len(results)} 筆：{' / '.join(labels)}。"


_ENTITY_RECOGNITION_PROMPT = """\
你是日本卡牌/周邊二手交易的實體辨識助手。下面是一筆 Mercari 商品標題（可能含賣家錯字、雜訊、emoji、促銷詞）。
請辨識它指的是「哪一張卡/哪一個商品」，並輸出乾淨、可用於搜尋比價的 canonical 查詢字串。

規則：
- canonical_query：修正錯字（特別是片假名外來語拼法，如 ヴァイスシュバルツ→ヴァイスシュヴァルツ）、移除雜訊/促銷詞/emoji，保留可辨識卡名+系列+角色+稀有度。
- aliases：列出其他可能拼法，務必包含賣家原始標題中的拼法。
- 若你無法有把握辨識（資料不足、太模糊），confident 設為 false，不要硬猜。
- 只能依據標題與下方「已知實體」提示，不要編造不存在的系列或角色。

已知實體提示（RAG，可能為空）：
{grounding}

商品標題：
{title}

只輸出 JSON，格式：
{{"confident": true/false, "canonical_query": "...", "card_name": "...", "series": "...", "character": "...", "rarity": "...", "aliases": ["...", "..."]}}
"""


def build_ollama_entity_recognizer(
    *,
    endpoint: str,
    model: str,
    knowledge_db_path: str | None = None,
    timeout_seconds: int = 90,
) -> EntityRecognizerFn:
    """Default M2 recognizer: one local qwen3:14b call extracting a canonical
    search query + aliases from the (typo-prone) listing title, RAG-grounded by
    the knowledge DB. Returns None when the model is not confident (Rule G)."""

    generate_url = endpoint.rstrip("/")
    if not generate_url.endswith("/api/generate"):
        generate_url = f"{generate_url}/api/generate"

    def _grounding(title: str) -> str:
        if not knowledge_db_path:
            return "（無）"
        try:
            db = KnowledgeDatabase(knowledge_db_path)
            hits = db.search_semantic("entry", title, k=5)
            names: list[str] = []
            for canonical, _score in hits:
                entry = db.get_entry(canonical)
                if entry is not None and entry.summary:
                    names.append(entry.summary[:120])
            return "\n".join(f"- {name}" for name in names) if names else "（無）"
        except Exception:  # noqa: BLE001 — grounding is best-effort
            return "（無）"

    def _recognize(item: ItemData) -> EntityProfile | None:
        title = (item.title or "").strip()
        if not title:
            return None
        prompt = _ENTITY_RECOGNITION_PROMPT.format(grounding=_grounding(title), title=title)
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0, "num_predict": 500},
        }
        request = Request(
            generate_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = str(data.get("response") or "").strip()
        return _parse_entity_profile(raw)

    return _recognize


def _parse_entity_profile(raw: str) -> EntityProfile | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not parsed.get("confident"):
        return None
    canonical = str(parsed.get("canonical_query") or "").strip()
    if not canonical:
        return None
    raw_aliases = parsed.get("aliases") or []
    aliases = tuple(
        a.strip() for a in raw_aliases if isinstance(a, str) and a.strip()
    ) if isinstance(raw_aliases, list) else ()

    def _opt(key: str) -> str | None:
        value = str(parsed.get(key) or "").strip()
        return value or None

    return EntityProfile(
        canonical_query=canonical,
        card_name=_opt("card_name"),
        series=_opt("series"),
        character=_opt("character"),
        rarity=_opt("rarity"),
        aliases=aliases,
    )


def build_appreciation_enricher(
    *,
    fetch_page_fn: Callable[[str], str],
    summarize_fn: Callable[[str, tuple[WebSearchResult, ...]], str],
    relevance_fn: Callable[[str, tuple[WebSearchResult, ...]], tuple[WebSearchResult, ...]] | None = None,
    max_pages: int = 3,
) -> AppreciationEnricherFn:
    """A4: turn snippet-only appreciation evidence into a grounded catalyst
    summary by fetching the top result pages and running the same LLM summariser
    the web-research renderer uses. fetch/summarize are injected so this stays
    decoupled from web_search/playwright and unit-testable. Reuses the existing
    search results — no extra search calls (Rule C7). When ``relevance_fn`` is
    given, off-topic results are dropped before any page is fetched; the gate is
    a safety net and falls back to the original results on failure/empty."""

    def _enrich(query: str, search_results: tuple[WebSearchResult, ...]) -> str | None:
        if not search_results or not query:
            return None
        relevant = search_results
        if relevance_fn is not None:
            try:
                filtered = tuple(relevance_fn(query, search_results))
            except Exception:  # noqa: BLE001 — gate must never lose good results
                logger.warning("appreciation relevance gate failed query=%s", query, exc_info=True)
                filtered = search_results
            relevant = filtered or search_results
        sources: list[WebSearchResult] = []
        for result in relevant[:max_pages]:
            try:
                content = fetch_page_fn(result.url)
            except Exception:  # noqa: BLE001 — fall back to snippet on any fetch failure
                logger.warning("appreciation page fetch failed url=%s", result.url, exc_info=True)
                content = ""
            sources.append(
                WebSearchResult(
                    title=result.title,
                    url=result.url,
                    snippet=result.snippet,
                    content=content or result.content or result.snippet,
                )
            )
        return summarize_fn(query, tuple(sources))

    return _enrich


def _build_price_query(ctx: ResearchJobContext) -> str:
    if ctx.entity_profile is not None and ctx.entity_profile.canonical_query.strip():
        return ctx.entity_profile.canonical_query.strip()
    if ctx.item_data is not None and ctx.item_data.title:
        return ctx.item_data.title
    if ctx.target is not None:
        return ctx.target.display_text
    return ""


def _derive_active_price_cap(listed_price_jpy: int | None, sold_average_jpy: int | None = None) -> int:
    if listed_price_jpy is not None and listed_price_jpy > 0:
        return max(5_000, int(listed_price_jpy * 2.0))
    if sold_average_jpy is not None and sold_average_jpy > 0:
        return max(5_000, int(sold_average_jpy * 2.0))
    return 50_000


def _classify_condition_class(title: str) -> str:
    """Binary 新品/中古 class for a C2C search-result listing.

    The marketplace search returns only title+price+url (Mercari's structured
    商品の状態 is a *filter* input, not a result field), so we infer from the
    title via the existing condition inferrer. Only an explicit 新品/未開封/
    未使用 claim counts as 新品; 未使用に近い and every unlabeled resale fall to
    中古 (C2C default). Heuristic — a mislabeled listing can land in the wrong
    bucket, but it cleanly separates the bulk sealed-stock band from genuine
    secondhand prices."""
    return "新品" if _infer_condition_from_title(title) == "新品、未使用" else "中古"


def _price_evidence_from_market_item(item: dict[str, object], *, sold_status: str) -> PriceEvidence:
    source_url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    raw_price = item.get("price_jpy")
    price_jpy = int(raw_price) if isinstance(raw_price, (int, float)) or str(raw_price).isdigit() else None
    return PriceEvidence(
        source_site=str(item.get("source") or "mercari"),
        source_url=source_url,
        title=title,
        price_jpy=price_jpy,
        sold_status=sold_status,
        condition_label=_classify_condition_class(title) if title else None,
        shipping_note=None,
        excluded_reason=None,
        observed_at=_utc_now_iso(),
    )


def _prices_by_condition(evidence: tuple[PriceEvidence, ...]) -> dict[str, list[int]]:
    """Bucket priced evidence into 中古 / 新品 (unlabeled defaults to 中古)."""
    buckets: dict[str, list[int]] = {"中古": [], "新品": []}
    for e in evidence:
        if e.price_jpy is None:
            continue
        buckets.setdefault(e.condition_label or "中古", []).append(e.price_jpy)
    return buckets


def _condition_average(evidence: tuple[PriceEvidence, ...], label: str) -> float | None:
    """Average sold/active price for one condition class, or None when absent."""
    prices = _prices_by_condition(evidence).get(label) or []
    return sum(prices) / len(prices) if prices else None


def _condition_split_lines(evidence: tuple[PriceEvidence, ...], *, kind: str) -> list[str]:
    """Per-condition (中古 / 新品) median+range lines for a price sample.

    Returns lines only when BOTH classes are present — when the whole sample is
    one condition, the headline line already conveys it, so we skip the noise.
    ``kind`` is the band label (e.g. ``active``) shown in each line."""
    buckets = _prices_by_condition(evidence)
    present = [(label, buckets[label]) for label in ("中古", "新品") if buckets[label]]
    if len(present) < 2:
        return []
    lines: list[str] = []
    for label, prices in present:
        ordered = sorted(prices)
        median = statistics.median(ordered)
        lines.append(
            f"・{label} {kind} {len(ordered)} 筆，中位數 ¥{median:,.0f}，"
            f"區間 ¥{min(ordered):,}–¥{max(ordered):,}"
        )
    return lines


def _format_price_band(low: int | None, high: int | None) -> str:
    """Render a min–max range as ``¥low〜¥high``; collapse to a single ``¥x``
    when the bounds coincide (or only one is known)."""
    lo = low if low is not None else high
    hi = high if high is not None else low
    if lo is None:
        return ""
    if hi is None or hi == lo:
        return f"¥{lo:,}"
    return f"¥{lo:,}〜¥{hi:,}"


def _format_shop_reference(ref: ShopReference) -> str:
    """One-line shop band for the price summary. 買取 = lower (liquidation),
    in-stock 販売 = upper (acquisition). Both sides are shown as a min–max
    range so the band conveys an actual upper/lower spread, not a single point.
    販売 only appears when stock-backed, so a 庫存0 card never poses as an upper
    bound."""
    has_buy = ref.buy_reference is not None
    has_sell = ref.sell_reference is not None
    buy_band = _format_price_band(ref.buy_min, ref.buy_max) or (
        f"¥{ref.buy_reference:,}" if has_buy else ""
    )
    sell_band = _format_price_band(ref.sell_min, ref.sell_max) or (
        f"¥{ref.sell_reference:,}" if has_sell else ""
    )
    stock_note = f"（在庫{ref.stock_total}点）" if ref.stock_total else ""
    if has_buy and has_sell:
        return f"{ref.label}参考帯 買取{buy_band}／販売{sell_band}{stock_note}"
    if has_sell:
        return f"{ref.label}参考 販売{sell_band}{stock_note}"
    if has_buy:
        return f"{ref.label}参考 買取{buy_band}（販売在庫なし、上限參考弱）"
    return ""


def _build_price_section_result(
    *,
    query: str,
    listed_price_jpy: int | None,
    active_evidence: tuple[PriceEvidence, ...],
    sold_evidence: tuple[PriceEvidence, ...],
    sold_average_jpy: float | None,
    listed_condition_label: str | None = None,
    shop_reference: ShopReference | None = None,
    active_dropped: int = 0,
    sold_dropped: int = 0,
    active_outliers: int = 0,
    sold_outliers: int = 0,
    backend_warnings: tuple[str, ...] = (),
) -> ResearchSectionResult:
    active_prices = [e.price_jpy for e in active_evidence if e.price_jpy is not None]
    sold_prices = [e.price_jpy for e in sold_evidence if e.price_jpy is not None]
    evidence_urls = tuple(
        e.source_url
        for e in (*sold_evidence[:3], *active_evidence[:3])
        if e.source_url
    )
    if shop_reference is not None and shop_reference.sample_urls:
        evidence_urls = (*evidence_urls, *shop_reference.sample_urls)
    warnings: list[str] = []
    status = "ok"
    summary_parts: list[str] = []

    if listed_price_jpy is not None:
        summary_parts.append(f"賣家開價 ¥{listed_price_jpy:,}")

    if sold_average_jpy is not None and sold_average_jpy > 0:
        sold_label = f"Mercari sold 樣本 {len(sold_prices)} 筆" if sold_prices else "Mercari sold 均價"
        summary_parts.append(f"{sold_label}，均價約 ¥{sold_average_jpy:,.0f}")
        summary_parts.extend(_condition_split_lines(sold_evidence, kind="sold"))
    else:
        status = "partial"
        warnings.append("Mercari sold 價目前只拿到平均值接口；此查詢未回傳可用 sold avg。")

    if active_prices:
        active_median = statistics.median(active_prices)
        source_breakdown = _active_source_breakdown(active_evidence)
        breakdown_note = f"（{source_breakdown}）" if source_breakdown else ""
        summary_parts.append(
            f"active 樣本 {len(active_prices)} 筆{breakdown_note}，中位數 ¥{active_median:,.0f}，區間 ¥{min(active_prices):,}–¥{max(active_prices):,}"
        )
        summary_parts.extend(_condition_split_lines(active_evidence, kind="active"))
    else:
        status = "partial" if summary_parts else "unavailable"
        warnings.append("active 比價樣本不足（Mercari / Rakuma 均未取得）。")

    if shop_reference is not None:
        shop_band_text = _format_shop_reference(shop_reference)
    elif any("店舗參考價" in w for w in backend_warnings):
        shop_band_text = "遊々亭参考：暫無法取得（rate-limited）"
    else:
        shop_band_text = ""
    if shop_band_text:
        summary_parts.append(shop_band_text)
        if status == "unavailable":
            status = "partial"

    # ── verdict (conclusion) — prepended so summary reads verdict first ─────
    verdict_parts: list[str] = []
    if listed_price_jpy is not None and sold_average_jpy is not None and sold_average_jpy > 0:
        # Compare like-for-like: a 中古 listing must be measured against 中古 sold
        # comps, not a pooled average that 新品 sealed stock inflates.
        sold_classes = [
            label for label in ("中古", "新品") if _prices_by_condition(sold_evidence).get(label)
        ]
        compare_avg: float | None = None
        cond_note = ""
        compare_label: str | None = None
        if not sold_classes:
            # Only a pooled average is available (sold avg came from the lookup
            # endpoint with no per-item evidence to split) — be honest it's mixed.
            compare_avg = sold_average_jpy
            cond_note = "整體（未分新品／中古）"
        elif listed_condition_label is not None:
            compare_avg = _condition_average(sold_evidence, listed_condition_label)
            if compare_avg is not None:
                cond_note = f"同條件（{listed_condition_label}）"
                compare_label = listed_condition_label
            # else: no same-condition comp → withhold below rather than cross-compare.
        elif len(sold_classes) == 1:
            # Listed condition unknown but the sample is one class → pooled IS it.
            compare_avg = sold_average_jpy
            cond_note = f"同條件（{sold_classes[0]}）"
            compare_label = sold_classes[0]
        # else: listed condition unknown AND sample mixed → no single fair number.

        if compare_avg is not None and compare_avg > 0:
            ratio = listed_price_jpy / compare_avg
            diff_pct = abs(ratio - 1.0) * 100
            if ratio <= 0.85:
                verdict_parts.append(f"目前開價低於{cond_note} sold 均價約 {diff_pct:.0f}%")
            elif ratio >= 1.10:
                verdict_parts.append(f"目前開價高於{cond_note} sold 均價約 {diff_pct:.0f}%")
            else:
                verdict_parts.append(f"目前開價接近{cond_note} sold 均價")
            driving = _driving_comp_line(sold_evidence, label=compare_label)
            if driving:
                verdict_parts.append(driving)
        elif listed_condition_label is not None and sold_classes:
            # Listed condition known but no same-condition comp; a cross-condition
            # % is exactly the 新品／中古 mix the pooled average produces, so we
            # withhold it rather than mislead.
            verdict_parts.append(
                f"無同條件（{listed_condition_label}）sold 樣本，未做價差比較（避免新品／中古混比）"
            )
    # conclusion first, then evidence stats
    summary_parts = verdict_parts + summary_parts

    if sold_average_jpy is None and not active_prices:
        if shop_band_text:
            # No C2C data, but the shop band still gives a usable reference.
            status = "partial"
            summary_parts = [f"查詢「{query}」無 Mercari/Rakuma 樣本，僅店舗參考：", shop_band_text]
        else:
            status = "unavailable"
            summary_parts = [f"查詢「{query}」未取得可用的 sold 或 active 樣本。"]

    warnings.extend(backend_warnings)
    if sold_dropped:
        warnings.append(f"sold 候選排除了 {sold_dropped} 筆低相關樣本。")
    if active_dropped:
        warnings.append(f"active 候選排除了 {active_dropped} 筆低相關樣本。")
    if sold_outliers:
        warnings.append(f"sold 候選再排除了 {sold_outliers} 筆價格離群樣本（MAD）。")
    if active_outliers:
        warnings.append(f"active 候選再排除了 {active_outliers} 筆價格離群樣本（MAD）。")
    if sold_prices and len(sold_prices) < 3:
        if status == "ok":
            status = "partial"
        warnings.append("sold 樣本少於 3 筆，成交均價可信度有限。")
    if active_prices and len(active_prices) < 3:
        if status == "ok":
            status = "partial"
        warnings.append("active 樣本少於 3 筆，市價判讀可信度有限。")

    confidence = 0.0
    if active_prices:
        confidence += 0.25
        if len(active_prices) >= 3:
            confidence += 0.15
    if sold_average_jpy is not None and sold_average_jpy > 0:
        confidence += 0.25
    if listed_price_jpy is not None:
        confidence += 0.1
    confidence = round(min(0.75, confidence), 2)

    return ResearchSectionResult(
        section_name="合理市價分析",
        status=status,
        confidence=confidence,
        sample_count=len(active_prices) + len(sold_prices),
        evidence_count=len(active_evidence) + len(sold_evidence) + (1 if sold_average_jpy is not None and sold_average_jpy > 0 else 0),
        summary="；".join(summary_parts),
        evidence_urls=evidence_urls,
        warnings=tuple(warnings),
    )


def _build_liquidity_section_result(
    *,
    query: str,
    active_evidence: tuple[PriceEvidence, ...],
    sold_evidence: tuple[PriceEvidence, ...],
    sold_average_jpy: float | None,
) -> ResearchSectionResult:
    active_count = len([e for e in active_evidence if e.price_jpy is not None])
    sold_count = len([e for e in sold_evidence if e.price_jpy is not None])
    sample_count = active_count + sold_count
    evidence_urls = tuple(
        e.source_url
        for e in (*sold_evidence[:3], *active_evidence[:3])
        if e.source_url
    )

    if sample_count == 0:
        summary = f"查詢「{query}」尚未取得可用的 active / sold 樣本，無法判讀流動性。"
        return ResearchSectionResult(
            section_name="流動性分析",
            status="unavailable",
            confidence=0.0,
            sample_count=0,
            evidence_count=0,
            summary=summary,
            warnings=("流動性分析缺少 active / sold 樣本。",),
        )

    ratio = float(sold_count) if active_count == 0 else sold_count / active_count
    warnings: list[str] = []
    status = "ok"

    if sold_count >= 5 and ratio >= 1.0:
        liquidity_text = "樣本顯示流動性偏高，近期換手速度看起來不慢。"
    elif sold_count >= 2 and ratio >= 0.5:
        liquidity_text = "樣本顯示流動性中等，仍有一定成交速度。"
    elif sold_count == 0 and active_count >= 3:
        liquidity_text = "只看到在售、沒看到同款成交，流動性偏弱。"
    elif active_count == 0 and sold_count >= 2:
        liquidity_text = "成交樣本存在但當前在售很少，可能是換手快，也可能是供給薄。"
        status = "partial"
    else:
        liquidity_text = "樣本偏少，流動性暫時只能做弱判讀。"
        status = "partial"

    # verdict first, then sample counts and ratio
    summary_parts = [
        liquidity_text,
        f"active {active_count} 筆（跨平台）/ Mercari sold {sold_count} 筆",
        f"sold/active 比 {ratio:.2f}",
    ]
    if sold_average_jpy is not None and sold_average_jpy > 0:
        summary_parts.append(f"參考 sold 均價約 ¥{sold_average_jpy:,.0f}")

    if sold_count < 2:
        warnings.append("sold 樣本少於 2 筆，流動性判讀可信度有限。")
    if active_count < 2:
        warnings.append("active 樣本少於 2 筆，供給側觀察有限。")
    if sample_count < 4 and status == "ok":
        status = "partial"

    confidence = 0.2
    if sold_count >= 2:
        confidence += 0.2
    if active_count >= 2:
        confidence += 0.2
    if ratio >= 0.5 and sold_count >= 2:
        confidence += 0.1
    if sold_count >= 5:
        confidence += 0.1
    confidence = round(min(0.8, confidence), 2)

    return ResearchSectionResult(
        section_name="流動性分析",
        status=status,
        confidence=confidence,
        sample_count=sample_count,
        evidence_count=len(evidence_urls),
        summary="；".join(summary_parts),
        evidence_urls=evidence_urls,
        warnings=tuple(warnings),
    )


def _active_market_scrape_impl(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
    """Raw cross-platform active listing scrape (runs inside the scrape worker).

    Reuses the marketplace registry that the watchlist monitor uses
    (Mercari + Rakuma + Yuyutei, and any future source registered there) so
    /research reference pricing reflects every platform the user already
    monitors — not just Mercari. Each source's failure is isolated: a dead
    scraper logs and is skipped, the others still contribute evidence."""
    from price_monitor_bot.watch_monitor import default_marketplace_clients

    clients = default_marketplace_clients()
    merged: list[dict[str, object]] = []
    for source_name, client in clients.items():
        # Yuyutei is a shop, not C2C — its 買取/販売 prices are surfaced as a
        # separate reference band (see _default_shop_reference_fn) so they
        # don't get averaged into the Mercari/Rakuma C2C median.
        if source_name == "yuyutei":
            continue
        try:
            listings = client.search(query, price_max=price_cap, max_results=max_results)
        except Exception:
            logger.exception(
                "Research active market search failed source=%s query=%s",
                source_name, query,
            )
            continue
        for listing in listings:
            merged.append(
                {
                    "source": getattr(listing, "source", source_name) or source_name,
                    "item_id": getattr(listing, "item_id", ""),
                    "title": getattr(listing, "title", ""),
                    "price_jpy": getattr(listing, "price_jpy", None),
                    "url": getattr(listing, "url", ""),
                    "thumbnail_url": getattr(listing, "thumbnail_url", None),
                }
            )
    return merged


def _sold_market_scrape_impl(query: str, max_results: int) -> list[dict[str, object]]:
    """Raw Mercari sold-listing scrape (runs inside the scrape worker)."""
    from market_monitor.mercari_search import search_mercari_sold

    return search_mercari_sold(query, max_results=max_results)


def _sold_average_scrape_impl(query: str) -> float | None:
    """Raw Mercari sold-average scrape (runs inside the scrape worker)."""
    from market_monitor.mercari_search import fetch_avg_sold_price

    return fetch_avg_sold_price(query)


def _default_active_market_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
    result = run_in_subprocess(
        "active",
        {"query": query, "price_cap": price_cap, "max_results": max_results},
        timeout=_ACTIVE_SCRAPE_TIMEOUT,
    )
    return list(result or [])


def _default_sold_market_search(query: str, max_results: int) -> list[dict[str, object]]:
    result = run_in_subprocess(
        "sold",
        {"query": query, "max_results": max_results},
        timeout=_SOLD_SCRAPE_TIMEOUT,
    )
    return list(result or [])


def _default_sold_average_lookup(query: str) -> float | None:
    result = run_in_subprocess("sold_avg", {"query": query}, timeout=_SOLD_AVG_SCRAPE_TIMEOUT)
    return float(result) if isinstance(result, (int, float)) else None


def _shop_reference_to_dict(ref: ShopReference) -> dict[str, object]:
    return {
        "label": ref.label,
        "buy_reference": ref.buy_reference,
        "sell_reference": ref.sell_reference,
        "stock_total": ref.stock_total,
        "buy_count": ref.buy_count,
        "sell_count": ref.sell_count,
        "sample_urls": list(ref.sample_urls),
        "buy_min": ref.buy_min,
        "buy_max": ref.buy_max,
        "sell_min": ref.sell_min,
        "sell_max": ref.sell_max,
        "sample_titles": list(ref.sample_titles),
    }


def _shop_reference_from_dict(data: dict[str, object]) -> ShopReference:
    return ShopReference(
        label=str(data["label"]),
        buy_reference=data.get("buy_reference"),
        sell_reference=data.get("sell_reference"),
        stock_total=int(data.get("stock_total") or 0),
        buy_count=int(data.get("buy_count") or 0),
        sell_count=int(data.get("sell_count") or 0),
        sample_urls=tuple(data.get("sample_urls") or ()),
        buy_min=data.get("buy_min"),
        buy_max=data.get("buy_max"),
        sell_min=data.get("sell_min"),
        sell_max=data.get("sell_max"),
        sample_titles=tuple(data.get("sample_titles") or ()),
    )


def _shop_reference_scrape_impl(
    query: str, price_cap: int, source_options: dict[str, object] | None
) -> ShopReference | None:
    """Raw Yuyu亭 reference-band scrape (runs inside the scrape worker)."""
    from market_monitor.yuyutei_search import YuyuteiMarketplaceSearchClient

    band = YuyuteiMarketplaceSearchClient().reference_band(
        query, price_max=price_cap, source_options=source_options
    )
    if band is None or not band.has_data:
        # Persist a cross-process hint when the in-process circuit was tripped by a
        # 429, so the parent's next call skips yuyu-tei until the cooldown clears
        # instead of naively spawning another subprocess that will also get rate-limited.
        try:
            from market_monitor.http import _circuit_remaining
            if _circuit_remaining("yuyu-tei.jp") > 0:
                _yuyutei_trip_cross_process_cooldown()
        except Exception:  # noqa: BLE001
            pass
        return None
    return ShopReference(
        label="遊々亭",
        buy_reference=band.buy_reference,
        sell_reference=band.sell_reference,
        stock_total=band.sell_stock_total,
        buy_count=len(band.buy_prices),
        sell_count=len(band.sell_prices),
        sample_urls=band.sample_urls,
        buy_min=band.buy_min,
        buy_max=band.buy_max,
        sell_min=band.sell_min,
        sell_max=band.sell_max,
        sample_titles=getattr(band, "sample_titles", ()),
    )


def _shop_reference_from_band(
    query: str, price_cap: int, source_options: dict[str, object] | None
) -> ShopReference | None:
    # Skip the subprocess entirely if the cross-process cooldown is still active,
    # saving one wasted Playwright spawn that would just 429 again immediately.
    remaining = _yuyutei_cooldown_remaining()
    if remaining > 0:
        raise RuntimeError(f"yuyu-tei.jp rate-limited ({remaining:.0f}s cross-process cooldown)")
    result = run_in_subprocess(
        "shop_reference",
        {"query": query, "price_cap": price_cap, "source_options": source_options},
        timeout=_SHOP_REF_SCRAPE_TIMEOUT,
    )
    if not isinstance(result, dict):
        # The subprocess may have just written the cooldown file on a fresh 429;
        # surface it as an exception so the caller adds a user-visible warning.
        remaining = _yuyutei_cooldown_remaining()
        if remaining > 0:
            raise RuntimeError(f"yuyu-tei.jp rate-limited ({remaining:.0f}s cross-process cooldown)")
        return None
    return _shop_reference_from_dict(result)


def build_shop_reference_fn(
    game_code_resolver_fn: GameCodeResolverFn | None = None,
    cache_enricher_fn: "CacheEnricherFn | None" = None,
) -> ShopReferenceFn:
    """Build the Yuyu亭 shop-band fetcher. When a ``game_code_resolver_fn`` is
    supplied, it resolves a query's yuyutei game code (e.g. プロセカ card →
    ``ws``) so the band appears even for bare card names with no game keyword.
    The resolver returns ``None`` when it can't identify a TCG game, in which
    case Yuyutei is skipped (no fan-out, no wasted request).

    When a ``cache_enricher_fn`` is supplied, the verbatim matched-listing titles
    on a successful band are handed back to it so the yuyutei code cache can record
    the item's real identity — from data already fetched, no extra request."""

    def fn(query: str, price_cap: int) -> ShopReference | None:
        source_options: dict[str, object] | None = None
        if game_code_resolver_fn is not None:
            try:
                code = game_code_resolver_fn(query)
            except Exception:
                logger.exception("Yuyutei game-code resolver failed query=%s", query)
                code = None
            if not code:
                return None
            source_options = {"game_code": code}
        ref = _shop_reference_from_band(query, price_cap, source_options)
        if ref is not None and cache_enricher_fn is not None and ref.sample_titles:
            try:
                cache_enricher_fn(query, ref.sample_titles)
            except Exception:
                logger.exception("Yuyutei cache enrichment hook failed query=%s", query)
        return ref

    return fn


# Default (no resolver): only resolves codes a query already spells out (game
# word or explicit code). Real wiring injects an LLM/RAG resolver via
# build_research_handler so bare card names route correctly.
_default_shop_reference_fn: ShopReferenceFn = build_shop_reference_fn(None)


def _active_source_breakdown(evidence: tuple[PriceEvidence, ...]) -> str:
    """Render a per-platform count AND representative price, e.g.
    "mercari 2筆 中位¥3,500 / rakuma 1筆 ¥79,900", so each source's reference
    price shows up inline in the summary text — not only as a clickable link.
    Rendered even for a single source: when active is e.g. Rakuma-only, the
    user still needs to see which platform and at what price, instead of a
    bare "active 樣本 1 筆" that forces them to open the link."""
    prices_by_source: dict[str, list[int]] = {}
    for item in evidence:
        if item.price_jpy is None:
            continue
        prices_by_source.setdefault(item.source_site, []).append(item.price_jpy)
    if not prices_by_source:
        return ""
    parts: list[str] = []
    for source, prices in sorted(prices_by_source.items()):
        if len(prices) == 1:
            parts.append(f"{source} 1筆 ¥{prices[0]:,}")
        else:
            parts.append(f"{source} {len(prices)}筆 中位¥{statistics.median(prices):,.0f}")
    return " / ".join(parts)


def _average_price_from_evidence(evidence: tuple[PriceEvidence, ...]) -> float | None:
    prices = [price for price in (item.price_jpy for item in evidence) if price is not None]
    if not prices:
        return None
    return sum(prices) / len(prices)


def _driving_comp_line(
    sold_evidence: tuple[PriceEvidence, ...], *, label: str | None, head: int = 3
) -> str:
    """A '結論依據' line naming the sold comps the verdict's average rests on, so
    the price call is traceable rather than a bare number. ``label`` narrows to
    the condition class actually compared (None = the whole priced sample)."""
    comps = [
        e
        for e in sold_evidence
        if e.price_jpy is not None and (label is None or (e.condition_label or "中古") == label)
    ]
    if not comps:
        return ""
    comps.sort(key=lambda e: e.price_jpy or 0)
    parts: list[str] = []
    for e in comps[:head]:
        price = f"¥{e.price_jpy:,}"
        parts.append(f"{price} {e.source_url}" if e.source_url else price)
    extra = f"…(+{len(comps) - head})" if len(comps) > head else ""
    return f"結論依據 {len(comps)} 筆 sold comp：" + "；".join(parts) + extra


def _drop_price_outliers(
    evidence: tuple[PriceEvidence, ...], *, threshold: float = 3.5
) -> tuple[tuple[PriceEvidence, ...], int]:
    """Remove price outliers via the median-absolute-deviation (MAD) modified
    z-score, so one mis-listed ¥1 or ¥999,999 comp can't drag the median/mean.

    Pure numeric (robust to small, skewed samples) — not a keyword filter, so it
    composes with the existing similarity gate without touching open-world
    recognition (Rule G). No-ops below 4 priced comps (too few to call an
    outlier) and when MAD is 0 (a tied cluster). Unpriced evidence is kept."""
    prices = [e.price_jpy for e in evidence if e.price_jpy is not None]
    if len(prices) < 4:
        return evidence, 0
    median = statistics.median(prices)
    mad = statistics.median([abs(p - median) for p in prices])
    if mad == 0:
        return evidence, 0
    kept: list[PriceEvidence] = []
    dropped = 0
    for e in evidence:
        if e.price_jpy is None:
            kept.append(e)
            continue
        modified_z = 0.6745 * (e.price_jpy - median) / mad
        if abs(modified_z) > threshold:
            dropped += 1
            continue
        kept.append(e)
    return tuple(kept), dropped


# PR3 semantic rerank gate tuning. Gray zone = the lexical band just below the
# keep threshold where a cross-script / paraphrased *same sellable unit* (Mode 2)
# can hide; we hand that band to the LLM instead of dropping it outright.
_SEMANTIC_FLOOR = 0.18
_MAX_SEMANTIC_CANDIDATES = 20


@dataclass(frozen=True)
class CandidateForSemanticRerank:
    """One marketplace comp offered to the PR3 semantic gate.

    ``index`` is the candidate's position in the bounded rerank pool — the gate
    returns the indices it wants to keep.
    """

    index: int
    title: str
    price: int | None
    lexical_score: float
    item: dict[str, object]


def _coerce_price_jpy(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def _classify_market_items_for_price(
    *,
    reference_title: str,
    items: list[dict[str, object]],
    min_similarity: float,
    semantic_floor: float,
    idf_stats: "TitleIdfStats | None" = None,
) -> tuple[list[tuple[dict[str, object], float]], list[tuple[dict[str, object], float]], int]:
    """Split candidates into (lexical-kept, gray-zone, hard-dropped count).

    Hard drops (empty title / graded-vs-raw / anchor-token miss) never reach the
    semantic gate. Lexical-kept = score >= ``min_similarity``. Gray zone =
    ``semantic_floor`` <= score < ``min_similarity``. Each surviving candidate is
    paired with its lexical score so the rerank pool can be bounded by score.
    """
    # Load the historical IDF table once for the whole batch (the comparison runs
    # per candidate item). When no DF file exists the loader returns None and we
    # fall back to PR1's unweighted Jaccard — cold start must never break filtering.
    if idf_stats is None:
        idf_stats = _default_title_idf_stats()
    if idf_stats is None:
        token_idf = bigram_idf = None
        default_token_idf = default_bigram_idf = 1.0
    else:
        token_idf = idf_stats.token_idf
        bigram_idf = idf_stats.bigram_idf
        default_token_idf = idf_stats.default_token_idf
        default_bigram_idf = idf_stats.default_bigram_idf
    specific_tokens = _specific_reference_tokens(reference_title)
    anchor_tokens = set(specific_tokens[1:] if len(specific_tokens) >= 2 else specific_tokens)
    kept: list[tuple[dict[str, object], float]] = []
    gray: list[tuple[dict[str, object], float]] = []
    hard_dropped = 0
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            hard_dropped += 1
            continue
        if _looks_graded_title(title) and not _looks_graded_title(reference_title):
            hard_dropped += 1
            continue
        candidate_tokens = set(_market_title_tokens(_normalize_market_title(title)))
        # Anchor-token miss is NOT an absolute hard drop: a cross-script /
        # paraphrased *same sellable unit* (Mode 2, e.g. THE BOOK II vs ザ・ブック2)
        # can share no anchor token yet still be the same product. Such a
        # candidate stays out of the lexical-kept set (PR2 behaviour unchanged)
        # but is routed to the gray zone so the semantic gate can rescue it. The
        # semantic_floor below still keeps obvious noise out of the LLM call.
        anchor_ok = (not anchor_tokens) or bool(anchor_tokens & candidate_tokens)
        similarity = _title_similarity_score(
            reference_title,
            title,
            token_idf=token_idf,
            bigram_idf=bigram_idf,
            default_token_idf=default_token_idf,
            default_bigram_idf=default_bigram_idf,
        )
        if anchor_ok and similarity >= min_similarity:
            kept.append((item, similarity))
        elif similarity >= semantic_floor:
            gray.append((item, similarity))
        else:
            hard_dropped += 1
    return kept, gray, hard_dropped


def _filter_market_items_for_price(
    *,
    reference_title: str,
    items: list[dict[str, object]],
    min_similarity: float,
    idf_stats: "TitleIdfStats | None" = None,
) -> tuple[list[dict[str, object]], int]:
    """PR2 lexical filter (unchanged contract): returns (kept, dropped_count).

    Setting ``semantic_floor == min_similarity`` collapses the gray zone, so this
    behaves exactly as before — every candidate below the threshold counts as
    dropped. The PR3 path uses :func:`_classify_market_items_for_price` directly to
    rescue the gray zone via the semantic gate.
    """
    kept, _gray, _hard = _classify_market_items_for_price(
        reference_title=reference_title,
        items=items,
        min_similarity=min_similarity,
        semantic_floor=min_similarity,
        idf_stats=idf_stats,
    )
    kept_items = [item for item, _score in kept]
    return kept_items, len(items) - len(kept_items)


def _select_semantic_rerank_candidates(
    scored: list[tuple[dict[str, object], float]],
    *,
    max_candidates: int,
) -> list[CandidateForSemanticRerank]:
    """Bound the pool sent to the LLM: highest lexical score first, then capped.

    Score-descending ordering means lexical-kept items (score >= threshold) are
    always retained ahead of weaker gray-zone items when the cap bites — so the
    cap only ever truncates the speculative gray zone, never the solid keeps.
    """
    ordered = sorted(scored, key=lambda pair: pair[1], reverse=True)[:max_candidates]
    return [
        CandidateForSemanticRerank(
            index=position,
            title=str(item.get("title") or "").strip(),
            price=_coerce_price_jpy(item.get("price")),
            lexical_score=score,
            item=item,
        )
        for position, (item, score) in enumerate(ordered)
    ]


def _filter_market_items_with_semantic_gate(
    *,
    reference_title: str,
    reference_price: int | None,
    items: list[dict[str, object]],
    min_similarity: float,
    idf_stats: "TitleIdfStats | None" = None,
    semantic_gate_fn: "SemanticGateFn | None" = None,
    semantic_floor: float = _SEMANTIC_FLOOR,
    max_semantic_candidates: int = _MAX_SEMANTIC_CANDIDATES,
) -> tuple[list[dict[str, object]], int]:
    """PR3: PR2 lexical coarse filter + a semantic 'same sellable unit' gate.

    ``semantic_gate_fn(reference_title, reference_price, candidates)`` returns the
    set of candidate indices to keep, or ``None`` to signal it could not decide
    (timeout / bad JSON / unexpectedly empty) → fall back to the lexical-kept
    result. When ``semantic_gate_fn`` is ``None`` this is exactly the PR2 filter.
    """
    kept, gray, _hard = _classify_market_items_for_price(
        reference_title=reference_title,
        items=items,
        min_similarity=min_similarity,
        semantic_floor=semantic_floor,
        idf_stats=idf_stats,
    )
    lexical_kept_items = [item for item, _score in kept]

    def _lexical_result() -> tuple[list[dict[str, object]], int]:
        return lexical_kept_items, len(items) - len(lexical_kept_items)

    if semantic_gate_fn is None or (not kept and not gray):
        return _lexical_result()

    pool = _select_semantic_rerank_candidates(
        kept + gray, max_candidates=max_semantic_candidates
    )
    try:
        keep_positions = semantic_gate_fn(reference_title, reference_price, pool)
    except Exception:
        logger.exception("Semantic rerank gate raised; falling back to lexical result")
        return _lexical_result()
    if keep_positions is None:
        # Gate could not decide → safe fallback to the lexical-kept result.
        return _lexical_result()
    final_items = [cand.item for cand in pool if cand.index in keep_positions]
    return final_items, len(items) - len(final_items)


# Validated against the live qwen3:14b probe (cross-script rescue + high-overlap
# accessory traps). Emphasises *same sellable unit*, not same product family.
_SELLABLE_UNIT_GATE_PROMPT = """あなたは中古マーケットの比較対象を選ぶ審査員です。
「参照商品」と全く同じ『売買単位(sellable unit)』であるものだけを keep します。
同じ商品ファミリーでも、売買単位が違うものは drop してください。

keep する例:
- 表記ゆれ・別の書記体系(漢字/カナ/ローマ字)で同一商品
- 言い回しが少し違うだけの同一商品
- 同じ版・同じ梱包・同じ物理単位

drop する例:
- 単品 vs BOX/セット、付属品のみ vs 本体
- 鑑定品(PSA/BGS)vs 生品、開封済み/傷あり vs 未開封
- 別の版/別のモデル/別の巻/別の数量

参照商品:
{reference}

候補(index: 内容):
{candidates}

厳密なJSONのみを返す。説明は書かない。
形式: {{"keep": [keepするindexの配列]}}"""


def _format_gate_line(candidate: "CandidateForSemanticRerank") -> str:
    if candidate.price is not None:
        return f"{candidate.index}: {candidate.title} / ¥{candidate.price:,}"
    return f"{candidate.index}: {candidate.title}"


def _run_sellable_unit_gate(
    reference_title: str,
    reference_price: int | None,
    candidates: "list[CandidateForSemanticRerank]",
    *,
    endpoint: str,
    model: str,
    timeout_seconds: int,
    ssl_context=None,
) -> "set[int] | None":
    """One batched local-LLM call judging same sellable unit.

    Returns the kept candidate indices, or ``None`` on any failure (network, bad
    JSON) or an unexpectedly empty keep-list — the caller then falls back to the
    lexical result so a model hiccup never wipes out every comp.
    """
    if not candidates:
        return None
    reference = reference_title
    if reference_price is not None:
        reference = f"{reference_title} / ¥{reference_price:,}"
    prompt = _SELLABLE_UNIT_GATE_PROMPT.format(
        reference=reference,
        candidates="\n".join(_format_gate_line(c) for c in candidates),
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }
    generate_url = endpoint.rstrip("/")
    if not generate_url.endswith("/api/generate"):
        generate_url = f"{generate_url}/api/generate"
    request = Request(
        generate_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(json.loads(body).get("response", "{}"))
        keep_raw = parsed.get("keep", [])
    except Exception:
        logger.exception("Sellable-unit gate request failed; falling back to lexical")
        return None
    valid = {c.index for c in candidates}
    keep: set[int] = set()
    for value in keep_raw if isinstance(keep_raw, list) else ():
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if idx in valid:
            keep.add(idx)
    if not keep:
        logger.info("Sellable-unit gate kept nothing; falling back to lexical")
        return None
    return keep


def build_ollama_sellable_unit_gate(
    *,
    endpoint: str,
    model: str,
    timeout_seconds: int = 60,
    ssl_context=None,
) -> SemanticGateFn:
    """Production PR3 gate backed by local Ollama (qwen3:14b)."""

    def gate(
        reference_title: str,
        reference_price: int | None,
        candidates: "list[CandidateForSemanticRerank]",
    ) -> "set[int] | None":
        return _run_sellable_unit_gate(
            reference_title,
            reference_price,
            candidates,
            endpoint=endpoint,
            model=model,
            timeout_seconds=timeout_seconds,
            ssl_context=ssl_context,
        )

    return gate


def weighted_jaccard(
    reference_tokens: set[str],
    candidate_tokens: set[str],
    idf: dict[str, float] | None = None,
    default_idf: float = 1.0,
) -> float:
    """IDF-weighted Jaccard. With idf=None this degenerates to ordinary Jaccard.
    The idf interface is intentionally preserved for PR2, where historical DF/IDF
    weights will down-weight shared product-name tokens against missing specs."""
    union = reference_tokens | candidate_tokens
    intersection = reference_tokens & candidate_tokens
    if not union:
        return 0.0

    def weight(token: str) -> float:
        if idf is None:
            return default_idf
        return idf.get(token, default_idf)

    numerator = sum(weight(token) for token in intersection)
    denominator = sum(weight(token) for token in union)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _title_similarity_score(
    reference: str,
    candidate: str,
    *,
    token_idf: dict[str, float] | None = None,
    bigram_idf: dict[str, float] | None = None,
    default_token_idf: float = 1.0,
    default_bigram_idf: float = 1.0,
) -> float:
    # Deliberately no coverage path and no containment bonus: both rewarded
    # `candidate ⊂ reference`, inflating single-card subsets into false comps.
    # Aggregation is max(token weighted_jaccard, bigram weighted_jaccard); the
    # prior 0.55/0.45 token+bigram blend is dropped on purpose.
    # PR2: historical IDF maps (when supplied) make rare, high-information
    # attributes (BOX/シュリンク付き/完全生産限定盤) outweigh generic family
    # tokens, so a single card sharing only the product name scores low. With
    # idf=None this stays identical to PR1's plain weighted Jaccard.
    ref = _normalize_market_title(reference)
    cand = _normalize_market_title(candidate)
    if not ref or not cand:
        return 0.0
    if ref == cand:
        return 1.0

    ref_tokens = set(_market_title_tokens(ref))
    cand_tokens = set(_market_title_tokens(cand))
    ref_bigrams = _char_ngrams(ref, 2)
    cand_bigrams = _char_ngrams(cand, 2)

    score = max(
        weighted_jaccard(
            ref_tokens, cand_tokens, idf=token_idf, default_idf=default_token_idf
        ),
        weighted_jaccard(
            ref_bigrams, cand_bigrams, idf=bigram_idf, default_idf=default_bigram_idf
        ),
    )
    return round(score, 4)


# Loanword orthography folding: the ヴ-row (ヴァ/ヴィ/ヴ…) is routinely written
# with the b-row (バ/ビ/ブ…) in Japanese listings, e.g. ヴァイスシュヴァルツ vs.
# ヴァイスシュバルツ for "Weiß Schwarz". Fold ヴ-row → b-row so the two spellings
# normalize identically. This is deterministic orthographic normalization (akin to
# NFKC), not open-world entity recognition — "which card is this" stays with LLM+RAG.
_KATAKANA_VU_FOLD = (
    ("ヴァ", "バ"),
    ("ヴィ", "ビ"),
    ("ヴェ", "ベ"),
    ("ヴォ", "ボ"),
    ("ヴュ", "ビュ"),
    ("ヴャ", "ビャ"),
    ("ヴョ", "ビョ"),
    ("ヴ", "ブ"),
)


def _fold_katakana_variants(text: str) -> str:
    folded = text
    for src, dst in _KATAKANA_VU_FOLD:
        folded = folded.replace(src, dst)
    return folded


def _normalize_market_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    normalized = _fold_katakana_variants(normalized)
    for src, dst in (
        ("　", " "),
        ("・", " "),
        ("【", " "),
        ("】", " "),
        ("（", " "),
        ("）", " "),
        ("(", " "),
        (")", " "),
        ("「", " "),
        ("」", " "),
        ("[", " "),
        ("]", " "),
        ("-", " "),
        ("_", " "),
        ("/", " "),
    ):
        normalized = normalized.replace(src, dst)
    return " ".join(normalized.split()).strip()


def _market_title_tokens(text: str) -> tuple[str, ...]:
    tokens = tuple(token for token in re.split(r"\s+", text) if len(token) >= 2)
    return tokens


def _char_ngrams(text: str, size: int) -> set[str]:
    compact = text.replace(" ", "")
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _looks_graded_title(text: str) -> bool:
    return bool(_GRADED_TITLE_RE.search(text or ""))


def _specific_reference_tokens(text: str) -> tuple[str, ...]:
    specific: list[str] = []
    for token in _market_title_tokens(_normalize_market_title(text)):
        if len(token) < 4:
            continue
        if _GENERIC_PROMO_TOKEN_RE.search(token):
            continue
        if token not in specific:
            specific.append(token)
    return tuple(specific)


# ---------------------------------------------------------------------------
# PR2: historical document-frequency / IDF weighting for title similarity.
#
# Pure-statistical only (Rule G): there is no hand-written keyword or alias
# table here. Discriminative power is learned from how often a token/bigram
# appears across accumulated marketplace titles — generic family words
# (ポケモンカード / cd) land in many titles → low IDF; rare attributes
# (box / シュリンク付き / 完全生産限定盤) land in few → high IDF.
# ---------------------------------------------------------------------------

_TITLE_DF_VERSION = 1
_TITLE_DF_PATH = pathlib.Path(__file__).resolve().parents[2] / "data" / "market_title_df.json"
# Cap so a small corpus can't hand any single rare term a runaway weight; with a
# few hundred docs an unseen token would otherwise dominate the union sum.
_MAX_TITLE_IDF = 8.0


@dataclass(frozen=True)
class TitleIdfStats:
    """Precomputed IDF lookups for title tokens and character bigrams.

    `default_*_idf` is the weight for a term absent from the corpus (treated as
    document frequency 0 → the most discriminative, but bounded by the cap)."""

    total_docs: int
    token_idf: dict[str, float]
    bigram_idf: dict[str, float]
    default_token_idf: float
    default_bigram_idf: float


def _df_terms_for_title(title: str) -> tuple[set[str], set[str]]:
    """Token + bigram sets for one title, using the exact same normalization and
    tokenization as scoring so DF keys line up with lookup keys at query time."""
    normalized = _normalize_market_title(title)
    tokens = set(_market_title_tokens(normalized))
    bigrams = _char_ngrams(normalized, 2)
    return tokens, bigrams


def build_title_df_from_titles(titles: Iterable[str]) -> dict[str, object]:
    """Build a document-frequency payload from raw marketplace titles.

    Each title is one document; a term contributes at most 1 to its DF per title
    (set semantics). Blank titles that yield no terms are skipped entirely."""
    token_df: dict[str, int] = {}
    bigram_df: dict[str, int] = {}
    total_docs = 0
    for title in titles:
        tokens, bigrams = _df_terms_for_title(str(title or ""))
        if not tokens and not bigrams:
            continue
        total_docs += 1
        for token in tokens:
            token_df[token] = token_df.get(token, 0) + 1
        for bigram in bigrams:
            bigram_df[bigram] = bigram_df.get(bigram, 0) + 1
    return {
        "version": _TITLE_DF_VERSION,
        "total_docs": total_docs,
        "token_df": token_df,
        "bigram_df": bigram_df,
    }


def _idf_from_df(df: int, total_docs: int, max_idf: float = _MAX_TITLE_IDF) -> float:
    # Smoothed, monotonically decreasing in df, always >= 1.0, never divides by
    # zero. df == 0 (unseen) gives the largest value, then capped.
    value = math.log((total_docs + 1) / (df + 1)) + 1.0
    return min(value, max_idf)


def compute_idf_map(
    df_map: dict[str, int], total_docs: int, max_idf: float = _MAX_TITLE_IDF
) -> dict[str, float]:
    return {
        term: _idf_from_df(int(count), total_docs, max_idf)
        for term, count in df_map.items()
    }


def title_idf_stats_from_df(
    payload: dict[str, object], max_idf: float = _MAX_TITLE_IDF
) -> TitleIdfStats | None:
    """Turn a DF payload into ready-to-use IDF lookups, or None if it carries no
    documents (an empty corpus must degrade to PR1 behaviour, not zero weights)."""
    total_docs = int(payload.get("total_docs") or 0)
    if total_docs <= 0:
        return None
    token_df = payload.get("token_df") or {}
    bigram_df = payload.get("bigram_df") or {}
    default_idf = _idf_from_df(0, total_docs, max_idf)
    return TitleIdfStats(
        total_docs=total_docs,
        token_idf=compute_idf_map(token_df, total_docs, max_idf),  # type: ignore[arg-type]
        bigram_idf=compute_idf_map(bigram_df, total_docs, max_idf),  # type: ignore[arg-type]
        default_token_idf=default_idf,
        default_bigram_idf=default_idf,
    )


def build_title_idf_stats_from_titles(
    titles: Iterable[str], max_idf: float = _MAX_TITLE_IDF
) -> TitleIdfStats | None:
    """Convenience for tests/offline tooling: titles → DF → IDF stats in one hop."""
    return title_idf_stats_from_df(build_title_df_from_titles(titles), max_idf)


def load_title_idf_stats(path: pathlib.Path | None = None) -> TitleIdfStats | None:
    """Load IDF stats from the DF JSON file. Returns None when the file is absent
    or malformed so /research silently falls back to PR1's unweighted Jaccard."""
    target = pathlib.Path(path) if path is not None else _TITLE_DF_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning(
            "Malformed market title DF at %s; using unweighted title similarity", target
        )
        return None
    if not isinstance(payload, dict):
        return None
    return title_idf_stats_from_df(payload)


# Activation gate. A DF table is only trusted once it is BOTH thick enough and
# behaviourally sane, otherwise /research silently stays on PR1 plain Jaccard:
#   1. min docs — a thin/narrow corpus learns the wrong "which words are common"
#      and can shift the PR1 threshold unpredictably (tunable; raise as the
#      corpus grows).
#   2. canary self-check — a big-but-off-domain table can still pass (1); these
#      two synthetic cases verify the learned weights preserve the known-good
#      ordering (subset card DROPs, reordered same unit KEEPs) before we trust it.
_MIN_TITLE_CORPUS_DOCS = 3000
_ACTIVATION_CANARY_DROP = ("黒炎の支配者 box シュリンク付き 未開封", "黒炎の支配者")
_ACTIVATION_CANARY_KEEP = (
    "黒炎の支配者 box シュリンク付き 未開封",
    "黒炎の支配者 box 未開封 シュリンク付き",
)
_ACTIVATION_CANARY_THRESHOLD = 0.32


def _passes_activation_canary(stats: TitleIdfStats) -> bool:
    drop_score = _title_similarity_score(
        *_ACTIVATION_CANARY_DROP,
        token_idf=stats.token_idf,
        bigram_idf=stats.bigram_idf,
        default_token_idf=stats.default_token_idf,
        default_bigram_idf=stats.default_bigram_idf,
    )
    keep_score = _title_similarity_score(
        *_ACTIVATION_CANARY_KEEP,
        token_idf=stats.token_idf,
        bigram_idf=stats.bigram_idf,
        default_token_idf=stats.default_token_idf,
        default_bigram_idf=stats.default_bigram_idf,
    )
    return drop_score < _ACTIVATION_CANARY_THRESHOLD <= keep_score


def gate_title_idf_stats(
    stats: TitleIdfStats | None, *, min_docs: int | None = None
) -> TitleIdfStats | None:
    """Return stats only if they clear the activation gate, else None (→ PR1)."""
    if stats is None:
        return None
    if min_docs is None:
        min_docs = _MIN_TITLE_CORPUS_DOCS
    if stats.total_docs < min_docs:
        logger.info(
            "Title IDF table too thin (%d < %d docs); staying on plain Jaccard",
            stats.total_docs,
            min_docs,
        )
        return None
    if not _passes_activation_canary(stats):
        logger.warning(
            "Title IDF table failed activation canary (%d docs); staying on plain Jaccard",
            stats.total_docs,
        )
        return None
    return stats


def describe_title_idf_activation(
    stats: TitleIdfStats | None, *, min_docs: int | None = None
) -> dict[str, object]:
    """Explain whether *stats* would activate, for the weekly rebuild report.

    Mirrors :func:`gate_title_idf_stats` but returns the decision plus *why*
    (docs vs threshold, canary pass/fail) so the Telegram notice can say more
    than "on" / "off".
    """
    if min_docs is None:
        min_docs = _MIN_TITLE_CORPUS_DOCS
    if stats is None:
        return {
            "activated": False,
            "reason": "no_table",
            "total_docs": 0,
            "min_docs": min_docs,
            "enough_docs": False,
            "canary_pass": False,
        }
    enough = stats.total_docs >= min_docs
    canary = _passes_activation_canary(stats)
    if enough and canary:
        reason = "activated"
    elif not enough:
        reason = "too_thin"
    else:
        reason = "canary_failed"
    return {
        "activated": enough and canary,
        "reason": reason,
        "total_docs": stats.total_docs,
        "min_docs": min_docs,
        "enough_docs": enough,
        "canary_pass": canary,
    }


# Hot-reload cache keyed on the DF file's mtime: the long-running bot (龍蝦) picks
# up a freshly rebuilt table on the next /research with no restart, while an
# unchanged file is served from memory (no per-item disk reads). When the file is
# absent the key is None and we serve the cached cold-start result.
_idf_cache: dict[str, object] = {"key": "__unset__", "stats": None}


def _default_title_idf_stats() -> TitleIdfStats | None:
    try:
        key: object = _TITLE_DF_PATH.stat().st_mtime_ns
    except OSError:
        key = None
    if _idf_cache["key"] == key:
        return _idf_cache["stats"]  # type: ignore[return-value]
    stats = gate_title_idf_stats(load_title_idf_stats())
    _idf_cache["key"] = key
    _idf_cache["stats"] = stats
    return stats


def explain_title_similarity(
    reference: str,
    candidate: str,
    *,
    idf_stats: TitleIdfStats | None = None,
) -> dict[str, object]:
    """Developer-only diagnostics: show which tokens matched, which high-IDF
    attributes the candidate is missing, and the resulting component scores.
    Not surfaced to end users — purely for validating/tuning the IDF weighting."""
    if idf_stats is None:
        idf_stats = _default_title_idf_stats()
    if idf_stats is None:
        token_idf = bigram_idf = None
        default_token_idf = default_bigram_idf = 1.0
    else:
        token_idf = idf_stats.token_idf
        bigram_idf = idf_stats.bigram_idf
        default_token_idf = idf_stats.default_token_idf
        default_bigram_idf = idf_stats.default_bigram_idf

    ref_norm = _normalize_market_title(reference)
    cand_norm = _normalize_market_title(candidate)
    ref_tokens = set(_market_title_tokens(ref_norm))
    cand_tokens = set(_market_title_tokens(cand_norm))

    def _w(term: str) -> float:
        if token_idf is None:
            return default_token_idf
        return token_idf.get(term, default_token_idf)

    matched = {term: _w(term) for term in sorted(ref_tokens & cand_tokens)}
    missing = {term: _w(term) for term in sorted(ref_tokens - cand_tokens)}
    token_score = weighted_jaccard(
        ref_tokens, cand_tokens, idf=token_idf, default_idf=default_token_idf
    )
    bigram_score = weighted_jaccard(
        _char_ngrams(ref_norm, 2),
        _char_ngrams(cand_norm, 2),
        idf=bigram_idf,
        default_idf=default_bigram_idf,
    )
    return {
        "matched_tokens": matched,
        "missing_from_candidate": missing,
        "token_score": round(token_score, 4),
        "bigram_score": round(bigram_score, 4),
        "final_score": round(max(token_score, bigram_score), 4),
    }
