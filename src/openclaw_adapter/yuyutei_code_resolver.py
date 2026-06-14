"""Resolve a free-text item name to a Yuyu亭 (yuyu-tei.jp) game code.

A yuyutei reference band (買取/販売) can only be fetched for a single game
*code* (poc/ygo/ws/ua/op). A bare card name like 「大好きを前に 桐谷遥 SSP」 (a
プロセカ Weiß Schwarz card → ``ws``) carries no game keyword, so string rules
alone resolve it to nothing. Per the open-world rule (no hardcoded keyword
tables for recognition) the franchise→code decision is made by an LLM, grounded
by web search when the model is unsure, and the result is cached in the
knowledge DB so the same item never pays a second search.

Resolution order (cheap → expensive, each step short-circuits):
  1. Knowledge-DB cache  — zero network.
  2. Local LLM gate/classify — one local Ollama call (no ban risk). Resolves
     directly when the model knows the franchise; otherwise asks to "search".
  3. /search grounding   — one web search (the rate-limited resource), only when
     step 2 returns "search". The snippets are LLM-grounded into a code.
The resolved code (or a "no TCG game" negative) is written back to the KB, so a
manually-run /research never re-searches the same item.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from typing import Callable, Sequence

from .knowledge_db import KnowledgeDatabase

logger = logging.getLogger(__name__)

# yuyutei sell-path game codes. Closed protocol set (a fixed enum), NOT an
# open-world keyword table — the *mapping* from item name to one of these is the
# LLM's job; this tuple only states which codes the band fetcher accepts.
_KNOWN_CODES: frozenset[str] = frozenset({"poc", "ygo", "ws", "ua", "op"})
_NEGATIVE = "none"  # cached marker for "not a yuyutei TCG game"

_CACHE_MARKER_RE = re.compile(r"yuyutei_code=([a-z]+)")

# Each Ollama call returns JSON; signature matches opportunity_agent._call_ollama_json.
JsonCallFn = Callable[..., str]
SearchFn = Callable[[str, int], Sequence[object]]  # (query, limit) -> WebSearchResult-like

_CODE_LEGEND = (
    "poc=ポケモンカードゲーム / ygo=遊戯王 / "
    "ws=ヴァイスシュヴァルツ（プロセカ・ホロライブ等のアニメ/ゲーム版権タイトルを含む） / "
    "ua=UNION ARENA / op=ワンピースカードゲーム"
)


def _extract_code(raw: str) -> str | None:
    """Pull the verdict out of an LLM JSON reply. Returns a lowercased token
    (a game code or ``"search"``), ``_NEGATIVE`` when the model gave a valid
    "not a TCG game" answer (``{"code": null}`` or bare ``null``), or ``None``
    when the reply couldn't be parsed at all (transient/garbage — must NOT be
    cached as a negative)."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Tolerate a bare token / fenced JSON.
        match = re.search(r'"code"\s*:\s*"([a-z]+)"', raw or "", re.IGNORECASE)
        if match:
            return match.group(1).strip().lower()
        return None
    if payload is None:
        return _NEGATIVE
    if not isinstance(payload, dict):
        return None
    value = payload.get("code")
    if value is None:
        return _NEGATIVE
    return str(value).strip().lower()


class YuyuteiGameCodeResolver:
    def __init__(
        self,
        *,
        knowledge_db_path: str,
        json_call_fn: JsonCallFn,
        endpoint: str,
        model: str,
        timeout_seconds: int,
        ssl_context: ssl.SSLContext | None = None,
        search_fn: SearchFn | None = None,
        max_search_results: int = 5,
    ) -> None:
        self._db_path = knowledge_db_path
        self._json_call_fn = json_call_fn
        self._endpoint = endpoint
        self._model = model
        self._timeout_seconds = max(1, timeout_seconds)
        self._ssl_context = ssl_context
        self._search_fn = search_fn
        self._max_search_results = max(1, min(8, max_search_results))

    # ── public API ───────────────────────────────────────────────────────────

    def resolve(self, query: str) -> str | None:
        cleaned = " ".join((query or "").split()).strip()
        if not cleaned:
            return None

        cached = self._lookup_cache(cleaned)
        if cached is not None:
            code = None if cached == _NEGATIVE else cached
            logger.info("Yuyutei code cache hit query=%s code=%s", cleaned, code)
            return code

        verdict = self._classify_direct(cleaned)
        if verdict in _KNOWN_CODES:
            self._store_cache(cleaned, verdict, urls=())
            return verdict
        if verdict == "search":
            code = self._classify_via_search(cleaned)
            self._store_cache(cleaned, code or _NEGATIVE, urls=())
            return code
        if verdict == _NEGATIVE:
            self._store_cache(cleaned, _NEGATIVE, urls=())
            return None
        # verdict is None → LLM unavailable / unparsable. Do NOT poison the cache
        # with a negative on a transient failure; just skip this round.
        return None

    # ── steps ────────────────────────────────────────────────────────────────

    def _lookup_cache(self, query: str) -> str | None:
        """Return the cached code (a game code or ``_NEGATIVE``) or ``None`` for
        a miss."""
        try:
            entry = KnowledgeDatabase(self._db_path).get_entry(query)
        except Exception:
            logger.exception("Yuyutei code cache lookup failed query=%s", query)
            return None
        if entry is None:
            return None
        match = _CACHE_MARKER_RE.search(entry.summary or "")
        if not match:
            return None
        token = match.group(1)
        if token in _KNOWN_CODES or token == _NEGATIVE:
            return token
        return None

    def _store_cache(self, query: str, code: str, *, urls: tuple[str, ...]) -> None:
        summary = f"yuyutei_code={code}. 商品「{query}」の遊々亭ゲームコード判定。"
        try:
            KnowledgeDatabase(self._db_path).upsert_entry(
                entity_canonical=query,
                entity_type="tcg",
                summary=summary,
                source_urls=urls,
                confidence=0.6,
                origin="research_command",
                aliases=(query,),
            )
        except Exception:
            logger.exception("Yuyutei code cache store failed query=%s code=%s", query, code)

    def _classify_direct(self, query: str) -> str | None:
        prompt = (
            "あなたは日本のトレーディングカード判定器です。\n"
            "次の商品名が『1枚のトレカ』で、かつ下記いずれかのゲームに属するならそのコードを返す。\n"
            f"{_CODE_LEGEND}\n"
            "判定規則:\n"
            "- ゲームが確実に分かる → そのコード(poc/ygo/ws/ua/op)\n"
            "- トレカだがどのゲームか自信がない → \"search\"\n"
            "- トレカではない(フィギュア/グッズ/他カテゴリ) → null\n"
            f"商品名: {query}\n"
            'JSONのみで答える: {"code": "<poc|ygo|ws|ua|op|search>" または null}'
        )
        return self._call(prompt)

    def _classify_via_search(self, query: str) -> str | None:
        if self._search_fn is None:
            return None
        try:
            results = self._search_fn(query, self._max_search_results)
        except Exception:
            logger.exception("Yuyutei code resolver: /search failed query=%s", query)
            return None
        lines: list[str] = []
        for item in results or ():
            title = getattr(item, "title", "") or ""
            snippet = getattr(item, "snippet", "") or ""
            text = f"- {title} | {snippet}".strip()
            if text != "-":
                lines.append(text)
        if not lines:
            return None
        prompt = (
            f"次の検索結果から、商品「{query}」が属するトレカゲームのコードを1つ選ぶ。\n"
            f"{_CODE_LEGEND}\n"
            "判定できなければ null。\n"
            "検索結果:\n" + "\n".join(lines[: self._max_search_results]) + "\n"
            'JSONのみで答える: {"code": "<poc|ygo|ws|ua|op>" または null}'
        )
        code = self._call(prompt)
        return code if code in _KNOWN_CODES else None

    def _call(self, prompt: str) -> str | None:
        try:
            raw = self._json_call_fn(
                endpoint=self._endpoint,
                model=self._model,
                prompt=prompt,
                timeout_seconds=self._timeout_seconds,
                ssl_context=self._ssl_context,
            )
        except Exception:
            logger.exception("Yuyutei code resolver: Ollama call failed")
            return None
        return _extract_code(raw)
