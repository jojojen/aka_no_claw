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

import json
import logging
import re
import ssl
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence

from .knowledge_db import (
    ENTITY_TYPES,
    KnowledgeDatabase,
    _normalize_canonical,
)
from .web_search import WebSearchResult, search_duckduckgo

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
    ) -> None:
        self._db = knowledge_db
        self._endpoint = endpoint
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context
        self._max_search_results = max(1, min(8, max_search_results))
        self._search_fn = search_fn or (
            lambda query, limit: search_duckduckgo(
                query, max_results=limit, ssl_context=ssl_context,
            )
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
            self._queue.append(canonical)
        logger.info("EntityResearcher: enqueued %s (queue_size=%d)", canonical, len(self._queue))
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
                    logger.info("EntityResearcher: insufficient data for %s — skipping", canonical)
            except Exception:
                logger.exception("EntityResearcher: research failed for %s", canonical)

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
