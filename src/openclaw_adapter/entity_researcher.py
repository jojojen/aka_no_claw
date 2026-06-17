"""Backfill ``KnowledgeDatabase`` entries for unknown entities.

When the SNS signal classifier encounters an entity (IP / product / set /
event / creator / store) it has no grounded knowledge of, this researcher
runs a web search, asks an LLM to condense the top results into a 300-500
char summary tailored to TCG collector buy/sell judgment, and upserts the
result with ``origin='web_research'`` and ``confidence=0.5``.

Designed to run in the background (a daemon thread) so the classifier
isn't blocked by network / LLM latency — the current tweet uses whatever
knowledge is already available; the next tweet that mentions the same
entity gets the freshly-backfilled context.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import ssl
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence

from .knowledge_db import (
    COMMON_KNOWLEDGE_SUMMARY,
    ENTITY_TYPES,
    KnowledgeDatabase,
    NO_DATA_SUMMARY,
    _normalize_canonical,
)
from .web_search import WebSearchResult, web_search

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchResult:
    entity_canonical: str
    entity_type: str
    summary: str
    aliases: tuple[str, ...]
    source_urls: tuple[str, ...]


def _build_research_query(entity_name: str) -> str:
    """Bilingual JP/EN query optimised for TCG collector context."""
    return f'"{entity_name}" TCG カード IP 客群 市場'


def _build_common_knowledge_prompt(entity_name: str) -> str:
    """Ask the local model whether an entity is general public knowledge it already
    grounds on its own — in which case researching + storing it adds nothing to the
    TCG relevance classifier (which uses the same model) and only clutters the digest."""
    return (
        "你是 TCG / 收藏品知識庫的守門員。這個知識庫只該收錄『niche、需要特別查證才知道』"
        "的 TCG 相關 entity：特定卡包 / 系列、角色 / 創作者、店鋪、限時活動、冷門 IP。\n\n"
        "如果某個名稱是『廣為人知的大眾常識』——例如大型企業（Amazon / Sony）、國家地名、"
        "知名平台（YouTube / Twitter）、通用詞彙——那麼分類器本來就認得，存進知識庫毫無 grounding 價值。\n\n"
        f"名稱：{entity_name}\n\n"
        "你『本來就有把握、不需要查證』就知道這是什麼嗎？也就是它屬於大眾常識，而非需要查的 niche TCG entity？\n"
        "嚴格只回 JSON：\n"
        '{"common_knowledge": true/false, "reason": "一句話"}'
    )


def _build_condensation_prompt(
    entity_name: str, snippets: Sequence[WebSearchResult]
) -> str:
    snippet_lines = []
    for idx, snippet in enumerate(snippets[:5], 1):
        text = " ".join(f"{snippet.title} — {snippet.snippet}".split())
        if len(text) > 300:
            text = text[:300] + "…"
        snippet_lines.append(f"[{idx}] {text}\n   {snippet.url}")

    return (
        "你是 TCG / 收藏品市場分析助手。為下面的 entity 寫一段 300-500 字的繁體中文知識摘要，\n"
        "供 SNS 推文 relevance 分類器當作 grounded context 使用。\n"
        f"\nEntity 名稱：{entity_name}\n\n"
        "請判斷此 entity 屬於哪個類型（並用其中一個字串作為 entity_type）：\n"
        f"  {' / '.join(ENTITY_TYPES)}\n"
        "  - ip = 動漫/遊戲/VTuber 等 IP 本身（pokemon / pjsk / ホロライブ）\n"
        "  - product = 具體商品（一卡、一盒、一個週邊）\n"
        "  - set = 卡片擴充包 / 系列（アビスアイ / クリムゾンヘイズ）\n"
        "  - creator = 角色 / 創作者個人（特定 VTuber 名）\n"
        "  - event = 限時活動 / 抽選 / 展覽\n"
        "  - store = 通路 / 店鋪（Joshin / カードラッシュ）\n"
        "  - other = 都不像\n\n"
        "摘要必須包含（缺則寫「不明」）：\n"
        "  1. 是什麼（一句話）\n"
        "  2. 主要客群 / 收藏者特徵\n"
        "  3. 二手 / 卡片 / 週邊市場狀況（熱度、價格走勢、稀有度）\n"
        "  4. 對 TCG 投資判斷有用的關鍵事實（如發行週期、EOL 風險、抽選機制慣例）\n"
        "  5. 常見別名 / 簡稱 / 英日中對照\n\n"
        "搜尋片段：\n"
        + "\n".join(snippet_lines)
        + "\n\n請嚴格回 JSON：\n"
        '{"entity_type": "...", "summary": "300-500 字繁體中文摘要", '
        '"aliases": ["..."], "confident": true/false}\n'
        "若搜尋片段資訊不足判斷此 entity，confident=false 並寫一句話 summary 標註 ‘資料不足’。"
    )


def _parse_research_response(raw: str) -> dict | None:
    """Tolerant JSON parser — strips markdown fences and finds the JSON object."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


class EntityResearcher:
    """Background researcher that pulls entities off an internal queue,
    runs web search + LLM condensation, and upserts results into the
    KnowledgeDatabase.

    Use ``request(entity_name)`` to enqueue. Threadsafe; duplicates within a
    short window are dropped so two tweets mentioning the same entity don't
    trigger two researches."""

    def __init__(
        self,
        *,
        knowledge_db: KnowledgeDatabase,
        endpoint: str,
        model: str,
        timeout_seconds: int = 60,
        ssl_context: ssl.SSLContext | None = None,
        max_search_results: int = 5,
        search_fn: Callable | None = None,
        json_call_fn: Callable | None = None,
        recent_dedup_size: int = 200,
        max_per_day: int = 15,
    ) -> None:
        self._db = knowledge_db
        self._endpoint = endpoint
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context
        self._max_search_results = max(1, min(8, max_search_results))
        self._search_fn = search_fn or (
            lambda query, limit: web_search(query, max_results=limit)
        )
        # Lazy import to avoid circular dependency with opportunity_agent
        # (which already houses the canonical _call_ollama_json helper).
        if json_call_fn is not None:
            self._json_call_fn = json_call_fn
        else:
            from .opportunity_agent import _call_ollama_json
            self._json_call_fn = _call_ollama_json

        self._queue: deque[str] = deque()
        self._recent: deque[str] = deque(maxlen=recent_dedup_size)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._max_per_day = max(1, max_per_day)
        self._daily_count: int = 0
        self._daily_date: str = ""

    # ── Queue API ──────────────────────────────────────────────────────────

    def request(self, entity_name: str) -> bool:
        """Enqueue an entity for research. Returns True if accepted, False if
        skipped (already in DB, in queue, or recently researched)."""
        canonical = _normalize_canonical(entity_name)
        if not canonical:
            return False
        with self._lock:
            if canonical in self._recent:
                return False
            # Cheap pre-check: if already in DB, don't re-research.
            existing = self._db.get_entry(canonical)
            if existing is not None and existing.origin in ("web_research", "manual"):
                # Web-research won't override manual; skip both.
                self._recent.append(canonical)
                return False
            if canonical in self._queue:
                return False
            today = datetime.date.today().isoformat()
            if self._daily_date != today:
                self._daily_date = today
                self._daily_count = 0
            if self._daily_count >= self._max_per_day:
                logger.debug(
                    "EntityResearcher: daily budget %d reached — skipping %s",
                    self._max_per_day, canonical,
                )
                return False
            self._daily_count += 1
            self._queue.append(canonical)
        logger.info(
            "EntityResearcher: enqueued %s (queue_size=%d, today=%d/%d)",
            canonical, len(self._queue), self._daily_count, self._max_per_day,
        )
        return True

    # ── Worker lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="entity-researcher", daemon=True,
        )
        self._worker.start()
        logger.info("EntityResearcher: worker started")

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(2):
            with self._lock:
                if not self._queue:
                    continue
                canonical = self._queue.popleft()
                self._recent.append(canonical)
            try:
                self._handle(canonical)
            except Exception:
                logger.exception("EntityResearcher: research failed for %s", canonical)

    def _handle(self, canonical: str) -> None:
        """Process one queued entity: common-knowledge gate → research → cache.
        Extracted from the worker loop so it can be unit-tested synchronously."""
        if self._is_common_knowledge(canonical):
            # The local model already grounds this entity (general common
            # knowledge), so storing a research summary adds nothing to the
            # classifier. Cache a hidden stub (confidence 0 → never surfaced,
            # filtered by is_insufficient_entry) so request() short-circuits
            # future encounters and we skip the web search entirely.
            self._db.upsert_entry(
                entity_canonical=canonical,
                entity_type="other",
                summary=COMMON_KNOWLEDGE_SUMMARY,
                confidence=0.0,
                origin="web_research",
            )
            logger.info(
                "EntityResearcher: %s is common knowledge — skipped research, cached as hidden stub",
                canonical,
            )
            return
        result = self.research(canonical)
        if result is not None:
            self._db.upsert_entry(
                entity_canonical=result.entity_canonical,
                entity_type=result.entity_type,
                summary=result.summary,
                source_urls=result.source_urls,
                confidence=0.5,
                origin="web_research",
                aliases=result.aliases,
            )
            logger.info(
                "EntityResearcher: backfilled %s (type=%s, %d aliases, %d sources)",
                result.entity_canonical, result.entity_type,
                len(result.aliases), len(result.source_urls),
            )
        else:
            # Cache the negative result so request() short-circuits on
            # future encounters and never retriggers a search.  confidence=0
            # is intentionally below any real result (0.5+), so a later
            # successful research will overwrite this stub.
            self._db.upsert_entry(
                entity_canonical=canonical,
                entity_type="other",
                summary=NO_DATA_SUMMARY,
                confidence=0.0,
                origin="web_research",
            )
            logger.info("EntityResearcher: insufficient data for %s — cached as no-data stub", canonical)

    # ── Pre-store gate ──────────────────────────────────────────────────────

    def _is_common_knowledge(self, entity_name: str) -> bool:
        """Ask the local model whether *entity_name* is general public knowledge it
        already grounds. Fail-open: any error / non-JSON returns False so we fall
        through to normal research rather than silently dropping a real entity."""
        try:
            raw = self._json_call_fn(
                endpoint=self._endpoint, model=self._model,
                prompt=_build_common_knowledge_prompt(entity_name),
                timeout_seconds=self._timeout_seconds, ssl_context=self._ssl_context,
            )
        except Exception:
            logger.exception("EntityResearcher: common-knowledge check failed for %s", entity_name)
            return False
        parsed = _parse_research_response(raw)
        if not isinstance(parsed, dict):
            return False
        return bool(parsed.get("common_knowledge", False))

    # ── Synchronous research (called by worker, also unit-testable) ────────

    def research(self, entity_name: str) -> ResearchResult | None:
        query = _build_research_query(entity_name)
        try:
            snippets = list(self._search_fn(query, self._max_search_results))
        except Exception:
            logger.exception("EntityResearcher: web search failed for %s", entity_name)
            return None
        if not snippets:
            return None

        prompt = _build_condensation_prompt(entity_name, snippets)
        try:
            raw = self._json_call_fn(
                endpoint=self._endpoint, model=self._model, prompt=prompt,
                timeout_seconds=self._timeout_seconds, ssl_context=self._ssl_context,
            )
        except Exception:
            logger.exception("EntityResearcher: LLM call failed for %s", entity_name)
            return None

        parsed = _parse_research_response(raw)
        if not isinstance(parsed, dict):
            logger.warning("EntityResearcher: LLM returned non-JSON for %s: %s",
                           entity_name, (raw or "")[:200])
            return None
        if not parsed.get("confident", True):
            return None

        entity_type = str(parsed.get("entity_type", "")).strip().lower()
        if entity_type not in ENTITY_TYPES:
            entity_type = "other"
        summary = str(parsed.get("summary", "")).strip()
        if not summary:
            return None
        aliases_raw = parsed.get("aliases") or []
        aliases = tuple(
            str(a).strip() for a in aliases_raw if isinstance(a, str) and a.strip()
        )
        return ResearchResult(
            entity_canonical=_normalize_canonical(entity_name),
            entity_type=entity_type,
            summary=summary,
            aliases=aliases,
            source_urls=tuple(s.url for s in snippets if s.url),
        )
