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

    def _store_cache(
        self,
        query: str,
        code: str,
        *,
        urls: tuple[str, ...],
        matched_title: str | None = None,
        kind: str | None = None,
    ) -> None:
        # The yuyutei_code= marker MUST stay at the head: the resolver's own cache
        # lookup (_CACHE_MARKER_RE) and the digest's operational-cache filter both
        # key on it. Any enriched identity is appended after, never before.
        summary = f"yuyutei_code={code}."
        if matched_title:
            kind_label = {"single": "単カード", "box": "BOX/カートン"}.get(kind or "")
            tail = f"（{kind_label}）" if kind_label else ""
            summary += f" 遊々亭一致商品「{matched_title}」{tail}."
        summary += f" 検索語「{query}」の遊々亭ゲームコード判定。"
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

    def normalize_raw_card_query(self, query: str) -> str | None:
        """Derive 遊々亭's raw-card search term from a noisy /research price query.

        遊々亭 stocks raw (ungraded) singles, so a query that carries grading +
        platform + condition noise (e.g.「風に舞う花びらの中で 初音ミク ssp PSA10
        プロセカ」) makes the search_word miss — HTTP 200 with zero parsed hits
        (#41). The clean raw-card identity (character / card title / rarity) is an
        open-world judgement, so the local LLM derives it (Rule G — no keyword
        list); grading tokens are expected to be stripped by the caller first.
        Returns the normalized term, or ``None`` when the model is unsure or
        unavailable so the caller falls back to its own query. Never raises."""
        try:
            cleaned = " ".join((query or "").split()).strip()
            if not cleaned:
                return None
            prompt = (
                "あなたはトレカ検索語の正規化器です。\n"
                "遊々亭(yuyu-tei.jp)は『生(未鑑定)カード』を在庫します。次の検索語から、"
                "遊々亭で1枚のカードがヒットするような最小の検索語を作る。\n"
                "規則:\n"
                "- キャラ名・カード名・レアリティ(SSP/SR等)は残す。\n"
                "- 鑑定/状態の語(PSA10・BGS・美品・新品等)とプラットフォーム名"
                "(プロセカ・ホロライブ等)は除く(版権名は検索を狭めるため)。\n"
                "- 変更不要ならそのまま返す。判断できなければ null。\n"
                f"検索語: {cleaned}\n"
                'JSONのみで答える: {"query": "<正規化した検索語>" または null}'
            )
            raw = self._call_raw(prompt)
            if not raw:
                return None
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                match = re.search(r'"query"\s*:\s*"([^"]+)"', raw)
                payload = {"query": match.group(1)} if match else None
            if not isinstance(payload, dict):
                return None
            value = payload.get("query")
            if not isinstance(value, str):
                return None
            normalized = " ".join(value.split()).strip()
            return normalized or None
        except Exception:
            logger.exception("Yuyutei raw-card query normalize failed query=%s", query)
            return None

    def enrich_cache(self, query: str, titles: Sequence[str]) -> None:
        """Best-effort: from yuyutei listing titles already fetched for *query*
        (which carry the real card number / rarity / set / box name), have the
        local LLM pick the listing that IS this product and record its verbatim
        title + kind onto the cached entry. Grounded selection only — the title is
        stored verbatim (no field hallucination), and an unsure model picks nothing
        rather than guessing. Zero extra network (titles already fetched). Never
        raises: a failed enrichment must not break the price path."""
        try:
            cleaned = " ".join((query or "").split()).strip()
            if not cleaned:
                return
            candidates = [t.strip() for t in (titles or ()) if t and t.strip()]
            if not candidates:
                return
            cached = self._lookup_cache(cleaned)
            if cached is None or cached == _NEGATIVE:
                # Only enrich a positive game-code entry the resolver already cached.
                return
            picked = self._pick_matching_title(cleaned, candidates)
            if picked is None:
                return
            title, kind = picked
            self._store_cache(cleaned, cached, urls=(), matched_title=title, kind=kind)
            logger.info("Yuyutei cache enriched query=%s title=%s kind=%s", cleaned, title, kind)
        except Exception:
            logger.exception("Yuyutei cache enrichment failed query=%s", query)

    def _pick_matching_title(
        self, query: str, candidates: Sequence[str]
    ) -> tuple[str, str | None] | None:
        """Ask the LLM which listing title is the SAME product as *query*. Returns
        ``(verbatim_title, kind)`` or ``None`` when no candidate clearly matches.
        ``kind`` is one of ``"single" | "box" | None``."""
        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(candidates, 1))
        prompt = (
            "あなたはトレカ商品の同定器です。\n"
            f"検索語: {query}\n"
            "次の遊々亭の在庫商品名から、検索語とまったく同じ商品を1つだけ選ぶ。\n"
            "確実に一致するものが無ければ index は null（推測で選ばない）。\n"
            "kind は単カードなら \"single\"、BOX/カートン等の未開封なら \"box\"、不明なら null。\n"
            f"候補:\n{numbered}\n"
            'JSONのみで答える: {"index": <1〜' + str(len(candidates)) + ' または null>, '
            '"kind": "<single|box>" または null}'
        )
        raw = self._call_raw(prompt)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            match = re.search(r'"index"\s*:\s*(\d+)', raw)
            if not match:
                return None
            payload = {"index": int(match.group(1))}
        if not isinstance(payload, dict):
            return None
        index = payload.get("index")
        if not isinstance(index, int) or not (1 <= index <= len(candidates)):
            return None
        kind = payload.get("kind")
        kind = kind if kind in ("single", "box") else None
        return candidates[index - 1], kind

    def _call_raw(self, prompt: str) -> str | None:
        try:
            return self._json_call_fn(
                endpoint=self._endpoint,
                model=self._model,
                prompt=prompt,
                timeout_seconds=self._timeout_seconds,
                ssl_context=self._ssl_context,
            )
        except Exception:
            logger.exception("Yuyutei cache enrichment: Ollama call failed")
            return None

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
