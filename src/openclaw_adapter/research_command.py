from __future__ import annotations

import json
import logging
import re
import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag

from .knowledge_db import KnowledgeDatabase, KnowledgeEntry
from .web_search import WebSearchResult

logger = logging.getLogger(__name__)

SearchFn = Callable[[str, int], tuple[object, ...]]
FetchHtmlFn = Callable[[str], str]
SellerSnapshotLookupFn = Callable[[str], "SellerReputationSnapshot"]
ActiveMarketSearchFn = Callable[[str, int, int], list[dict[str, object]]]
SoldMarketSearchFn = Callable[[str, int], list[dict[str, object]]]
SoldAverageLookupFn = Callable[[str], float | None]
IpHeatLookupFn = Callable[[tuple[str, ...]], dict[str, tuple[object, ...]]]
ResearchStageRunner = Callable[["ResearchJobContext"], str]

_MERCARI_ITEM_PATH_RE = re.compile(r"^/item/(m\d+)/?$", re.IGNORECASE)
_MERCARI_PROFILE_PATH_RE = re.compile(r"^/user/profile/(\d+)/?$", re.IGNORECASE)
_MERCARI_HOSTS = frozenset({"jp.mercari.com", "www.mercari.com", "mercari.com"})
_TITLE_SUFFIX_RE = re.compile(r"\s+by メルカリ$", re.IGNORECASE)
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

    @property
    def remaining(self) -> int:
        return max(0, self.max_searches - self.searches_used)

    def consume(self) -> None:
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
    section_results: list[ResearchSectionResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    active_price_evidence: tuple[PriceEvidence, ...] = ()
    sold_price_evidence: tuple[PriceEvidence, ...] = ()
    sold_average_jpy: float | None = None
    appreciation_search_results: tuple[WebSearchResult, ...] = ()
    current_stage: int = 0
    current_label: str = ""

    def heartbeat(self, note: str = "仍在處理…") -> None:
        self.notifier.send(f"⏳ [{self.current_stage}/6] {self.current_label}：{note}")

    def add_section_result(self, result: ResearchSectionResult) -> None:
        self.section_results.append(result)
        self.warnings.extend(result.warnings)


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
                _TITLE_SUFFIX_RE.sub("", soup.title.get_text(" ", strip=True))
            )
        title = (
            _compact_whitespace(str(product.get("name") or ""))
            or _extract_meta_content(soup, "property", "og:title")
            or fallback_title
        )
        title = _compact_whitespace(_TITLE_SUFFIX_RE.sub("", title))

        description = _compact_whitespace(str(product.get("description") or ""))
        if not description:
            description = _extract_meta_content(soup, "name", "description")

        listed_price = _extract_price_from_product(product)
        if listed_price is None:
            listed_price = _extract_meta_price(html)

        condition_label = _extract_detail_value_text(soup, "商品の状態")
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
    user_agent: str = "OpenClawResearch/0.1 (+https://local-dev)",
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
        if item_id not in normalized:
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


def _run_in_isolated_thread(func: Callable[[], object]) -> object:
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
    done.wait()
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
        active_market_search_fn: ActiveMarketSearchFn | None = None,
        sold_market_search_fn: SoldMarketSearchFn | None = None,
        sold_average_lookup_fn: SoldAverageLookupFn | None = None,
        ip_heat_lookup_fn: IpHeatLookupFn | None = None,
    ) -> None:
        self._notifier_factory = notifier_factory or (lambda chat_id: _NullResearchNotifier())
        self._search_fn = search_fn or _NOOP_SEARCH_FN
        self._max_searches = max_searches
        self._item_fetcher = item_fetcher or MercariItemAdapter()
        self._knowledge_db_path = knowledge_db_path
        self._seller_snapshot_lookup_fn = seller_snapshot_lookup_fn
        self._active_market_search_fn = active_market_search_fn or _default_active_market_search
        self._sold_market_search_fn = sold_market_search_fn or _default_sold_market_search
        self._sold_average_lookup_fn = sold_average_lookup_fn or _default_sold_average_lookup
        self._ip_heat_lookup_fn = ip_heat_lookup_fn or (lambda canonicals: {})
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
        )
        try:
            for (stage_no, label), runner in zip(self._STAGES, self._stage_runners, strict=True):
                ctx.current_stage = stage_no
                ctx.current_label = label
                notifier.send(f"⏳ [{stage_no}/6] {label}中…")
                note = runner(ctx)
                notifier.send(f"✅ [{stage_no}/6] 完成（{note}）")
            return self._format_final_report(ctx)
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
            summary = "已把商品基礎事實寫入 knowledge DB（origin=research_command）"
            result = ResearchSectionResult(
                section_name="實體辨識",
                status="partial",
                confidence=min(0.85, ctx.item_data.source_confidence),
                sample_count=1,
                evidence_count=1,
                summary=summary,
                evidence_urls=(ctx.item_data.item_url,),
                warnings=("M2 僅寫入商品頁基礎事實，LLM 實體辨識與 alias 展開仍待補。",),
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

    def _stage_appreciation_placeholder(self, ctx: ResearchJobContext) -> str:
        entries = self._lookup_appreciation_entries(ctx)
        heat_by_canonical = self._ip_heat_lookup_fn(tuple(entry.entity_canonical for entry in entries))
        search_results = ()
        if _should_enrich_appreciation(entries, heat_by_canonical):
            search_results = _collect_appreciation_search_results(ctx)
            ctx.appreciation_search_results = search_results
        result = _build_appreciation_section_result(
            query=_build_price_query(ctx),
            entries=entries,
            heat_by_canonical=heat_by_canonical,
            search_results=search_results,
        )
        ctx.add_section_result(result)
        return result.summary

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
        for canonical, _score in ranked:
            entry = db.get_entry(canonical)
            if entry is None:
                continue
            entries.append(entry)
            db.mark_referenced(canonical)
            if len(entries) >= 3:
                break
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
        price_cap = _derive_active_price_cap(listed_price)
        backend_warnings: list[str] = []
        try:
            active_raw_all = self._active_market_search_fn(query, price_cap, 8)
        except Exception as exc:
            logger.exception("Research active market search failed query=%s", query)
            active_raw_all = []
            backend_warnings.append(f"Mercari active 比價抓取失敗：{exc}")
        active_raw, active_dropped = _filter_market_items_for_price(
            reference_title=reference_title,
            items=active_raw_all,
            min_similarity=0.32,
        )
        active_evidence = tuple(_price_evidence_from_market_item(item, sold_status="active") for item in active_raw)
        try:
            sold_raw_all = self._sold_market_search_fn(query, 8)
        except Exception as exc:
            logger.exception("Research sold market search failed query=%s", query)
            sold_raw_all = []
            backend_warnings.append(f"Mercari sold 比價抓取失敗：{exc}")
        sold_raw, sold_dropped = _filter_market_items_for_price(
            reference_title=reference_title,
            items=sold_raw_all,
            min_similarity=0.32,
        )
        sold_evidence = tuple(_price_evidence_from_market_item(item, sold_status="sold") for item in sold_raw)
        sold_avg = _average_price_from_evidence(sold_evidence)
        if sold_avg is None:
            try:
                sold_avg = self._sold_average_lookup_fn(query)
            except Exception as exc:
                logger.exception("Research sold average lookup failed query=%s", query)
                sold_avg = None
                backend_warnings.append(f"Mercari sold 均價查詢失敗：{exc}")
        ctx.active_price_evidence = active_evidence
        ctx.sold_price_evidence = sold_evidence
        ctx.sold_average_jpy = sold_avg
        result = _build_price_section_result(
            query=query,
            listed_price_jpy=listed_price,
            active_evidence=active_evidence,
            sold_evidence=sold_evidence,
            sold_average_jpy=sold_avg,
            active_dropped=active_dropped,
            sold_dropped=sold_dropped,
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

        result = _build_seller_snapshot_section_result(snapshot)
        ctx.add_section_result(result)
        return result.summary

    def _format_final_report(self, ctx: ResearchJobContext) -> str:
        assert ctx.target is not None
        mode_label = "Mercari 商品網址" if ctx.target.mode == "mercari_url" else "商品名稱"
        lines = [
            "龍蝦 /research 已完成目前可用流程。",
            f"研究模式：{mode_label}",
            f"研究目標：{ctx.target.display_text}",
            f"搜尋預算：{ctx.budget.searches_used}/{ctx.budget.max_searches}",
        ]
        if ctx.item_data is not None:
            item = ctx.item_data
            price_text = f"¥{item.listed_price_jpy:,}" if item.listed_price_jpy is not None else "未知"
            lines.append(
                f"商品頁資料：{item.title} / {price_text} / 狀態 {item.condition_label or '未知'} / "
                f"賣家 {item.seller_id or '未知'} / 圖片 {len(item.image_urls)} 張"
            )
        lines.append("")
        lines.append("各節結果：")
        for result in ctx.section_results:
            lines.append(
                f"- {result.section_name} [{result.status}] "
                f"confidence={result.confidence:.2f} sample={result.sample_count}: {result.summary}"
            )
            if result.evidence_urls:
                lines.extend(f"  source: {url}" for url in result.evidence_urls[:4])
        if ctx.warnings:
            deduped = list(dict.fromkeys(ctx.warnings))
            lines.append("")
            lines.append("Warnings：")
            lines.extend(f"- {warning}" for warning in deduped)
        return "\n".join(lines)


def build_research_handler(
    *,
    notifier_factory: Callable[[str], ResearchNotifier] | None = None,
    search_fn: SearchFn | None = None,
    stage_runners: Sequence[ResearchStageRunner] | None = None,
    max_searches: int = 5,
    item_fetcher: MercariItemAdapter | None = None,
    knowledge_db_path: str | None = None,
    seller_snapshot_lookup_fn: SellerSnapshotLookupFn | None = None,
    active_market_search_fn: ActiveMarketSearchFn | None = None,
    sold_market_search_fn: SoldMarketSearchFn | None = None,
    sold_average_lookup_fn: SoldAverageLookupFn | None = None,
    ip_heat_lookup_fn: IpHeatLookupFn | None = None,
) -> Callable[[str, str], str]:
    service = ResearchCommandService(
        notifier_factory=notifier_factory,
        search_fn=search_fn,
        stage_runners=stage_runners,
        max_searches=max_searches,
        item_fetcher=item_fetcher,
        knowledge_db_path=knowledge_db_path,
        seller_snapshot_lookup_fn=seller_snapshot_lookup_fn,
        active_market_search_fn=active_market_search_fn,
        sold_market_search_fn=sold_market_search_fn,
        sold_average_lookup_fn=sold_average_lookup_fn,
        ip_heat_lookup_fn=ip_heat_lookup_fn,
    )
    return service.run


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

    summary_parts: list[str] = []
    if meta_bits:
        summary_parts.append(" / ".join(meta_bits))
    if seller_bits:
        summary_parts.append("身為賣家：" + " / ".join(seller_bits))
    if snapshot.captured_at:
        summary_parts.append(f"快照時間 {snapshot.captured_at}")
    summary_parts.append(risk_text)
    if negative_review_summary:
        summary_parts.append(negative_review_summary)

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


def _build_appreciation_section_result(
    *,
    query: str,
    entries: Sequence[KnowledgeEntry],
    heat_by_canonical: dict[str, tuple[object, ...]],
    search_results: Sequence[WebSearchResult],
) -> ResearchSectionResult:
    if not entries:
        summary = f"查詢「{query}」目前只拿到商品頁事實，尚未命中可用的 IP / 作者知識。"
        if search_results:
            rendered = _render_appreciation_search_results(search_results)
            summary += " " + rendered
        return ResearchSectionResult(
            section_name="增值潛力分析",
            status="partial" if search_results else "unavailable",
            confidence=0.2 if search_results else 0.1,
            sample_count=0,
            evidence_count=len(search_results),
            summary=summary,
            evidence_urls=tuple(result.url for result in search_results[:4]),
            warnings=(
                "增值潛力尚未命中 knowledge DB 既有 entity；目前只提供 search snippet 級 evidence。",
            ),
        )

    evidence_urls: list[str] = []
    matched_labels: list[str] = []
    summary_parts: list[str] = []
    warnings: list[str] = []
    heat_lines: list[str] = []
    heat_hit = False

    for entry in entries:
        matched_labels.append(f"{entry.entity_canonical}({entry.entity_type})")
        evidence_urls.extend(url for url in entry.source_urls[:2] if url)
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
    if search_results:
        summary += " " + _render_appreciation_search_results(search_results)

    status = "ok" if heat_hit else "partial"
    if not heat_hit:
        warnings.append("尚未命中 IP heat 訊號；目前僅能根據既有知識摘要做弱判讀。")
    if search_results:
        warnings.append("外部搜尋結果目前只使用 snippet，尚未做頁面抓取與 LLM 催化劑摘要。")
    else:
        warnings.append("作者軌跡 / 再販 / 官方催化劑的 web enrichment 尚未接入。")
    confidence = 0.35 + min(0.25, 0.1 * len(entries)) + (0.15 if heat_hit else 0.0) + (0.05 if search_results else 0.0)
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


def _build_price_query(ctx: ResearchJobContext) -> str:
    if ctx.item_data is not None and ctx.item_data.title:
        return ctx.item_data.title
    if ctx.target is not None:
        return ctx.target.display_text
    return ""


def _derive_active_price_cap(listed_price_jpy: int | None) -> int:
    if listed_price_jpy is None or listed_price_jpy <= 0:
        return 50_000
    return max(5_000, int(listed_price_jpy * 2.0))


def _price_evidence_from_market_item(item: dict[str, object], *, sold_status: str) -> PriceEvidence:
    source_url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    raw_price = item.get("price_jpy")
    price_jpy = int(raw_price) if isinstance(raw_price, (int, float)) or str(raw_price).isdigit() else None
    return PriceEvidence(
        source_site="mercari",
        source_url=source_url,
        title=title,
        price_jpy=price_jpy,
        sold_status=sold_status,
        condition_label=None,
        shipping_note=None,
        excluded_reason=None,
        observed_at=_utc_now_iso(),
    )


def _build_price_section_result(
    *,
    query: str,
    listed_price_jpy: int | None,
    active_evidence: tuple[PriceEvidence, ...],
    sold_evidence: tuple[PriceEvidence, ...],
    sold_average_jpy: float | None,
    active_dropped: int = 0,
    sold_dropped: int = 0,
    backend_warnings: tuple[str, ...] = (),
) -> ResearchSectionResult:
    active_prices = [e.price_jpy for e in active_evidence if e.price_jpy is not None]
    sold_prices = [e.price_jpy for e in sold_evidence if e.price_jpy is not None]
    evidence_urls = tuple(
        e.source_url
        for e in (*sold_evidence[:3], *active_evidence[:3])
        if e.source_url
    )
    warnings: list[str] = []
    status = "ok"
    summary_parts: list[str] = []

    if listed_price_jpy is not None:
        summary_parts.append(f"賣家開價 ¥{listed_price_jpy:,}")

    if sold_average_jpy is not None and sold_average_jpy > 0:
        sold_label = f"Mercari sold 樣本 {len(sold_prices)} 筆" if sold_prices else "Mercari sold 均價"
        summary_parts.append(f"{sold_label}，均價約 ¥{sold_average_jpy:,.0f}")
    else:
        status = "partial"
        warnings.append("Mercari sold 價目前只拿到平均值接口；此查詢未回傳可用 sold avg。")

    if active_prices:
        active_median = statistics.median(active_prices)
        summary_parts.append(
            f"active 樣本 {len(active_prices)} 筆，中位數 ¥{active_median:,.0f}，區間 ¥{min(active_prices):,}–¥{max(active_prices):,}"
        )
    else:
        status = "partial" if summary_parts else "unavailable"
        warnings.append("Mercari active 比價樣本不足。")

    if listed_price_jpy is not None and sold_average_jpy is not None and sold_average_jpy > 0:
        ratio = listed_price_jpy / sold_average_jpy
        diff_pct = abs(ratio - 1.0) * 100
        if ratio <= 0.85:
            summary_parts.append(f"目前開價低於 sold 均價約 {diff_pct:.0f}%")
        elif ratio >= 1.10:
            summary_parts.append(f"目前開價高於 sold 均價約 {diff_pct:.0f}%")
        else:
            summary_parts.append("目前開價接近 sold 均價")

    if sold_average_jpy is None and not active_prices:
        status = "unavailable"
        summary_parts = [f"查詢「{query}」未取得可用的 sold 或 active 樣本。"]

    warnings.extend(backend_warnings)
    if sold_dropped:
        warnings.append(f"sold 候選排除了 {sold_dropped} 筆低相關樣本。")
    if active_dropped:
        warnings.append(f"active 候選排除了 {active_dropped} 筆低相關樣本。")
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
    summary_parts = [
        f"Mercari active {active_count} 筆 / sold {sold_count} 筆",
        f"sold/active 比 {ratio:.2f}",
    ]

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

    summary_parts.append(liquidity_text)
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


def _default_active_market_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
    from market_monitor.mercari_search import search_mercari

    return _run_in_isolated_thread(
        lambda: search_mercari(query, price_max=price_cap, max_results=max_results)
    )


def _default_sold_market_search(query: str, max_results: int) -> list[dict[str, object]]:
    from market_monitor.mercari_search import search_mercari_sold

    return _run_in_isolated_thread(
        lambda: search_mercari_sold(query, max_results=max_results)
    )


def _default_sold_average_lookup(query: str) -> float | None:
    from market_monitor.mercari_search import fetch_avg_sold_price

    return _run_in_isolated_thread(lambda: fetch_avg_sold_price(query))


def _average_price_from_evidence(evidence: tuple[PriceEvidence, ...]) -> float | None:
    prices = [price for price in (item.price_jpy for item in evidence) if price is not None]
    if not prices:
        return None
    return sum(prices) / len(prices)


def _filter_market_items_for_price(
    *,
    reference_title: str,
    items: list[dict[str, object]],
    min_similarity: float,
) -> tuple[list[dict[str, object]], int]:
    kept: list[dict[str, object]] = []
    dropped = 0
    specific_tokens = _specific_reference_tokens(reference_title)
    anchor_tokens = set(specific_tokens[1:] if len(specific_tokens) >= 2 else specific_tokens)
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            dropped += 1
            continue
        if _looks_graded_title(title) and not _looks_graded_title(reference_title):
            dropped += 1
            continue
        candidate_tokens = set(_market_title_tokens(_normalize_market_title(title)))
        if anchor_tokens and not (anchor_tokens & candidate_tokens):
            dropped += 1
            continue
        similarity = _title_similarity_score(reference_title, title)
        if similarity < min_similarity:
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def _title_similarity_score(reference: str, candidate: str) -> float:
    ref = _normalize_market_title(reference)
    cand = _normalize_market_title(candidate)
    if not ref or not cand:
        return 0.0
    if ref == cand:
        return 1.0

    ref_tokens = set(_market_title_tokens(ref))
    cand_tokens = set(_market_title_tokens(cand))
    token_score = 0.0
    token_coverage = 0.0
    if ref_tokens and cand_tokens:
        overlap = ref_tokens & cand_tokens
        token_score = len(overlap) / len(ref_tokens | cand_tokens)
        token_coverage = len(overlap) / len(cand_tokens)

    ref_bigrams = _char_ngrams(ref, 2)
    cand_bigrams = _char_ngrams(cand, 2)
    bigram_score = 0.0
    bigram_coverage = 0.0
    if ref_bigrams and cand_bigrams:
        overlap = ref_bigrams & cand_bigrams
        bigram_score = len(overlap) / len(ref_bigrams | cand_bigrams)
        bigram_coverage = len(overlap) / len(cand_bigrams)

    containment_bonus = 0.0
    if ref in cand or cand in ref:
        containment_bonus = 0.15

    score = max(
        token_score * 0.55 + bigram_score * 0.45,
        token_coverage * 0.55 + bigram_coverage * 0.45,
    )
    return min(1.0, round(score + containment_bonus, 4))


def _normalize_market_title(text: str) -> str:
    normalized = (text or "").lower()
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
