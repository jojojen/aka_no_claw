from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

SearchFn = Callable[[str, int], tuple[object, ...]]
ResearchStageRunner = Callable[["ResearchJobContext"], str]

_MERCARI_ITEM_PATH_RE = re.compile(r"^/item/(m\d+)/?$", re.IGNORECASE)
_MERCARI_HOSTS = frozenset({"jp.mercari.com", "www.mercari.com", "mercari.com"})


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


@dataclass(slots=True)
class ResearchJobContext:
    raw_input: str
    chat_id: str
    notifier: ResearchNotifier
    budget: ResearchBudget
    search_fn: SearchFn
    target: ResearchTarget | None = None
    warnings: list[str] = field(default_factory=list)
    current_stage: int = 0
    current_label: str = ""

    def heartbeat(self, note: str = "仍在處理…") -> None:
        self.notifier.send(f"⏳ [{self.current_stage}/6] {self.current_label}：{note}")


class _NullResearchNotifier:
    def send(self, text: str) -> None:
        return None


_NOOP_SEARCH_FN: SearchFn = lambda query, limit: ()


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
    ) -> None:
        self._notifier_factory = notifier_factory or (lambda chat_id: _NullResearchNotifier())
        self._search_fn = search_fn or _NOOP_SEARCH_FN
        self._max_searches = max_searches
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
            self._stage_item_fetch_placeholder,
            self._stage_entity_placeholder,
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

    def _stage_item_fetch_placeholder(self, ctx: ResearchJobContext) -> str:
        ctx.warnings.append("商品資料抓取尚未接上 Mercari item adapter。")
        if ctx.target and ctx.target.mode == "mercari_url":
            return "M1 骨架：已保留商品抓取接點，待串 Mercari adapter"
        return "名稱模式暫不抓單一商品頁"

    def _stage_entity_placeholder(self, ctx: ResearchJobContext) -> str:
        ctx.warnings.append("實體辨識與 knowledge DB 寫回仍待 M2。")
        return "M1 骨架：已保留實體辨識與知識庫接點"

    def _stage_appreciation_placeholder(self, ctx: ResearchJobContext) -> str:
        ctx.warnings.append("增值潛力分析仍待接 IP 熱度、作者背景與來源證據。")
        return "M1 骨架：已保留增值潛力分析階段"

    def _stage_price_placeholder(self, ctx: ResearchJobContext) -> str:
        ctx.warnings.append("合理市價分析仍待接 sold/active 比價資料。")
        return "M1 骨架：已保留合理市價分析階段"

    def _stage_liquidity_placeholder(self, ctx: ResearchJobContext) -> str:
        ctx.warnings.append("流動性分析仍待接 LIQUIDITY_METHODOLOGY 所需資料。")
        return "M1 骨架：已保留流動性分析階段"

    def _stage_seller_placeholder(self, ctx: ResearchJobContext) -> str:
        if ctx.target and ctx.target.mode != "mercari_url":
            return "名稱模式首版不做賣家風險"
        ctx.warnings.append("賣家風險分析仍待接 reputation snapshot 服務。")
        return "M1 骨架：已保留賣家風險分析階段"

    def _format_final_report(self, ctx: ResearchJobContext) -> str:
        assert ctx.target is not None
        mode_label = "Mercari 商品網址" if ctx.target.mode == "mercari_url" else "商品名稱"
        lines = [
            "龍蝦 /research 已完成 M1 骨架流程。",
            f"研究模式：{mode_label}",
            f"研究目標：{ctx.target.display_text}",
            f"搜尋預算：{ctx.budget.searches_used}/{ctx.budget.max_searches}（本階段尚未動用 Yahoo 搜尋）",
            "目前已完成：輸入解析、Mercari URL 正規化、進度通知、單 chat 防重入、共享搜尋預算骨架。",
            "目前尚未完成：商品抓取、實體辨識、增值潛力、市價、流動性、賣家風險的實資料分析。",
        ]
        if ctx.warnings:
            lines.append("待接資料源：")
            lines.extend(f"- {warning}" for warning in ctx.warnings)
        return "\n".join(lines)


def build_research_handler(
    *,
    notifier_factory: Callable[[str], ResearchNotifier] | None = None,
    search_fn: SearchFn | None = None,
    stage_runners: Sequence[ResearchStageRunner] | None = None,
    max_searches: int = 5,
) -> Callable[[str, str], str]:
    service = ResearchCommandService(
        notifier_factory=notifier_factory,
        search_fn=search_fn,
        stage_runners=stage_runners,
        max_searches=max_searches,
    )
    return service.run
