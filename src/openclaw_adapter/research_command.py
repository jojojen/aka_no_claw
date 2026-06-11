from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag

from .knowledge_db import KnowledgeDatabase

logger = logging.getLogger(__name__)

SearchFn = Callable[[str, int], tuple[object, ...]]
FetchHtmlFn = Callable[[str], str]
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
        return None
    row = header.find_parent("div", class_=lambda classes: classes and "merDisplayRow" in classes)
    if not isinstance(row, Tag):
        return None
    body = row.find("div", class_=lambda classes: classes and any("body__" in cls for cls in classes))
    if not isinstance(body, Tag):
        return None
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
    return text or None


def _extract_seller_url(soup: BeautifulSoup, *, base_url: str) -> str | None:
    link = soup.select_one('a[data-location="item_details:seller_info"]')
    if not isinstance(link, Tag):
        return None
    href = str(link.get("href") or "").strip()
    if not href:
        return None
    return urljoin(base_url, href)


def _extract_seller_id(seller_url: str | None) -> str | None:
    if not seller_url:
        return None
    match = _MERCARI_PROFILE_PATH_RE.match(urlsplit(seller_url).path or "")
    return match.group(1) if match else None


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
    ) -> None:
        self._notifier_factory = notifier_factory or (lambda chat_id: _NullResearchNotifier())
        self._search_fn = search_fn or _NOOP_SEARCH_FN
        self._max_searches = max_searches
        self._item_fetcher = item_fetcher or MercariItemAdapter()
        self._knowledge_db_path = knowledge_db_path
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
        db.upsert_entry(
            entity_canonical=item.title,
            entity_type="product",
            summary=" ".join(summary_parts),
            source_urls=(item.item_url, *item.image_urls[:2]),
            confidence=min(0.85, item.source_confidence),
            origin="research_command",
            aliases=(),
        )

    def _stage_appreciation_placeholder(self, ctx: ResearchJobContext) -> str:
        warning = "增值潛力分析仍待接 IP 熱度、作者背景與來源證據。"
        result = ResearchSectionResult(
            section_name="增值潛力分析",
            status="unavailable",
            confidence=0.0,
            sample_count=0,
            evidence_count=0,
            summary="M2 尚未接入增值潛力分析。",
            warnings=(warning,),
        )
        ctx.add_section_result(result)
        return result.summary

    def _stage_price_placeholder(self, ctx: ResearchJobContext) -> str:
        if ctx.item_data is not None and ctx.item_data.listed_price_jpy is not None:
            summary = f"已拿到賣家開價 ¥{ctx.item_data.listed_price_jpy:,}，但 sold/active 比價樣本尚未接入。"
        else:
            summary = "合理市價分析仍待接 sold/active 比價資料。"
        result = ResearchSectionResult(
            section_name="合理市價分析",
            status="partial" if ctx.item_data is not None else "unavailable",
            confidence=0.2 if ctx.item_data is not None else 0.0,
            sample_count=1 if ctx.item_data is not None else 0,
            evidence_count=1 if ctx.item_data is not None else 0,
            summary=summary,
            evidence_urls=(ctx.item_data.item_url,) if ctx.item_data is not None else (),
            warnings=("M2 只保留開價，市場比價 evidence 還沒接上。",),
        )
        ctx.add_section_result(result)
        return summary

    def _stage_liquidity_placeholder(self, ctx: ResearchJobContext) -> str:
        summary = "流動性分析仍待接 LIQUIDITY_METHODOLOGY 所需資料。"
        result = ResearchSectionResult(
            section_name="流動性分析",
            status="unavailable",
            confidence=0.0,
            sample_count=0,
            evidence_count=0,
            summary=summary,
            warnings=(summary,),
        )
        ctx.add_section_result(result)
        return summary

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
        if ctx.item_data is not None and ctx.item_data.seller_id is not None:
            summary = f"已抓到賣家 ID {ctx.item_data.seller_id}，但 reputation snapshot 尚未串接。"
        else:
            summary = "賣家風險分析仍待接 reputation snapshot 服務。"
        result = ResearchSectionResult(
            section_name="賣家風險分析",
            status="partial" if ctx.item_data is not None else "unavailable",
            confidence=0.2 if ctx.item_data and ctx.item_data.seller_id else 0.0,
            sample_count=1 if ctx.item_data and ctx.item_data.seller_id else 0,
            evidence_count=1 if ctx.item_data and ctx.item_data.seller_url else 0,
            summary=summary,
            evidence_urls=(ctx.item_data.seller_url,) if ctx.item_data and ctx.item_data.seller_url else (),
            warnings=("M2 只抓到賣家識別資訊，尚未建立評價快照。",),
        )
        ctx.add_section_result(result)
        return summary

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
) -> Callable[[str, str], str]:
    service = ResearchCommandService(
        notifier_factory=notifier_factory,
        search_fn=search_fn,
        stage_runners=stage_runners,
        max_searches=max_searches,
        item_fetcher=item_fetcher,
        knowledge_db_path=knowledge_db_path,
    )
    return service.run
