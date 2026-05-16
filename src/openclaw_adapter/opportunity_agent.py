from __future__ import annotations

import json
import logging
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from assistant_runtime import AssistantSettings, build_ssl_context, get_settings
from market_monitor.mercari_search import search_mercari
from price_monitor_bot.bot import TelegramBotClient
from price_monitor_bot.commands import lookup_card
from tcg_tracker.catalog import normalize_game_key, supported_game_hint
from tcg_tracker.hot_cards import TcgHotCardService

from .opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    build_candidate_id,
    build_listing_key,
    normalize_product_type,
)
from .opportunity_pipeline import CandidateProvider, OpportunityPipeline, OpportunityPipelineStats
from .opportunity_scoring import OpportunityThresholds, reputation_passes
from .opportunity_store import OpportunityStore
from .web_search import WebSearchResult, search_duckduckgo

logger = logging.getLogger(__name__)

_PRODUCT_TITLE_NOISE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"\bMercari\b", ""),
    (r"\bmercari\b", ""),
    (r"メルカリ", ""),
    (r"\s*(?:抽選|予約|発売|再販|入荷|販売|応募|キャンペーン)\s*(?:情報|開始|受付|告知|予告|ニュース)?\s*$", ""),
    (r"\s*(?:情報|ニュース|まとめ)\s*$", ""),
)
_SEARCH_QUERY_NOISE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"\bMercari\b", ""),
    (r"\bmercari\b", ""),
    (r"メルカリ", ""),
    (r"\s*(?:抽選|予約|発売|再販|入荷|販売|応募|キャンペーン)\s*(?:情報|開始|受付|告知|予告|ニュース)?\s*", " "),
    (r"\s*(?:情報|ニュース|まとめ)\s*", " "),
)
_UNSUPPORTED_FRANCHISE_MARKERS: tuple[str, ...] = (
    "デュエルマスターズ",
    "デュエマ",
    "one piece card game",
    "ワンピースカード",
    "ドラゴンボール",
    "magic: the gathering",
)


@dataclass(frozen=True, slots=True)
class SnsPost:
    tweet_id: str
    author_handle: str
    text: str
    created_at: str
    rule_label: str


class SnsLlmCandidateProvider:
    def __init__(
        self,
        *,
        db_path: str | Path,
        endpoint: str,
        model: str,
        timeout_seconds: int,
        lookback_hours: int,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._endpoint = endpoint
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._lookback_hours = lookback_hours
        self._ssl_context = ssl_context

    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        posts = self._read_recent_posts(limit=min(max(limit * 2, 8), 16))
        if not posts:
            logger.info("Opportunity SNS discovery found no recent posts path=%s", self._db_path)
            return ()
        if not self._endpoint or not self._model:
            logger.warning("Opportunity SNS discovery skipped LLM extraction because endpoint/model is empty.")
            return ()

        prompt = _build_sns_candidate_prompt(posts, limit=limit)
        logger.info(
            "Opportunity SNS discovery extracting candidates posts=%d model=%s timeout_seconds=%d",
            len(posts),
            self._model,
            self._timeout_seconds,
        )
        try:
            raw = _call_ollama_json(
                endpoint=self._endpoint,
                model=self._model,
                prompt=prompt,
                timeout_seconds=self._timeout_seconds,
                ssl_context=self._ssl_context,
            )
        except Exception as exc:
            logger.exception("Opportunity SNS LLM extraction failed: %s", exc)
            return ()
        return tuple(_parse_candidate_response(raw, posts=posts, limit=limit))

    def _read_recent_posts(self, *, limit: int) -> list[SnsPost]:
        if not self._db_path.exists():
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)).isoformat()
        try:
            with sqlite3.connect(self._db_path) as connection:
                connection.row_factory = sqlite3.Row
                # Over-fetch by a factor so the Python-side domain filter
                # still leaves us with `limit` matched rows even when the
                # SNS DB is dominated by non-TCG-tagged accounts.
                rows = connection.execute(
                    """
                    SELECT t.tweet_id, t.author_handle, t.text, t.created_at,
                           r.label AS rule_label, r.query_json AS rule_query_json
                    FROM seen_tweets t
                    LEFT JOIN watch_rules r ON r.rule_id = t.rule_id
                    WHERE t.first_seen_at >= ? OR t.created_at >= ?
                    ORDER BY t.first_seen_at DESC
                    LIMIT ?
                    """,
                    (cutoff, cutoff, limit * 8),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("Opportunity SNS read failed path=%s error=%s", self._db_path, exc)
            return []
        from sns_monitor.models import TCG_DOMAINS, normalize_domains

        posts: list[SnsPost] = []
        for row in rows:
            raw_json = row["rule_query_json"]
            domains: tuple[str, ...] = ()
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    domains = normalize_domains(parsed.get("domains"))
            if not domains or not (set(domains) & TCG_DOMAINS):
                # Rule is tagged with no domains, or with non-TCG domains.
                # Skip — this is what stops @realDonaldTrump-style tweets
                # from polluting the TCG opportunity LLM.
                continue
            posts.append(
                SnsPost(
                    tweet_id=str(row["tweet_id"]),
                    author_handle=str(row["author_handle"]),
                    text=str(row["text"]),
                    created_at=str(row["created_at"]),
                    rule_label=str(row["rule_label"] or ""),
                )
            )
            if len(posts) >= limit:
                break
        return posts


class WebResearchCandidateProvider:
    def __init__(
        self,
        *,
        base_provider,
        researcher: "WebOpportunityResearcher",
    ) -> None:
        self._base_provider = base_provider
        self._researcher = researcher

    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        candidates = self._base_provider.discover(limit=limit)
        enriched: list[OpportunityCandidate] = []
        for candidate in candidates:
            try:
                enriched.append(self._researcher.enrich(candidate))
            except Exception:
                logger.exception("Opportunity web research enrichment failed candidate_id=%s", candidate.candidate_id)
                enriched.append(candidate)
        return tuple(enriched)


# ─── Provider A: hot-card-board → single_card candidates ─────────────────────

class HotCardBoardCandidateProvider:
    """Convert items in the existing `/trend` hot-card boards into TCG
    opportunity candidates. Zero external dependencies — the hot-card service
    is already wired for the `/trend` command, so this just plugs the same
    data into the candidate pipeline.
    """

    def __init__(
        self,
        *,
        hot_card_service: TcgHotCardService,
        per_game_limit: int = 3,
        min_hot_score: float = 60.0,
    ) -> None:
        self._hot_card_service = hot_card_service
        self._per_game_limit = max(1, per_game_limit)
        self._min_hot_score = min_hot_score

    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        try:
            boards = self._hot_card_service.load_boards(limit=self._per_game_limit)
        except Exception:
            logger.exception("HotCardBoardCandidateProvider failed to load boards")
            return ()
        candidates: list[OpportunityCandidate] = []
        for board in boards:
            for entry in board.items[: self._per_game_limit]:
                if entry.hot_score is None or entry.hot_score < self._min_hot_score:
                    continue
                search_parts = [
                    part for part in (entry.title, entry.card_number, entry.rarity) if part
                ]
                search_query = " ".join(search_parts) or entry.title
                candidate = OpportunityCandidate(
                    candidate_id=build_candidate_id(
                        game=board.game,
                        product_type="single_card",
                        title=entry.title,
                        search_query=search_query,
                        product_identifier=entry.card_number,
                    ),
                    game=board.game,
                    product_type="single_card",
                    title=entry.title,
                    product_identifier=entry.card_number,
                    search_query=search_query,
                    heat_score=float(entry.hot_score),
                    reason=f"熱門卡排行 #{entry.rank} ({board.label})",
                    source_kind="hot_card_board",
                    metadata={"board_game": board.game, "rank": entry.rank},
                )
                candidates.append(candidate)
                if len(candidates) >= limit:
                    return tuple(candidates)
        return tuple(candidates)


# ─── Provider B: periodic web-trend search → LLM-extracted candidates ─────────

DEFAULT_WEB_TREND_QUERIES: tuple[str, ...] = (
    "ポケモンカード 再販 抽選情報 2026",
    "遊戯王 QCCP Quarter Century 新弾",
    "Weiss Schwarz 新ブースター 2026",
    "Union Arena 新ブースター 2026",
    "Pokemon Start Deck 100 新一波 抽選",
)


class ScheduledWebSearchCandidateProvider:
    """Run a small batch of TCG-trend queries via DuckDuckGo, then feed the
    snippets to the existing SNS-extraction LLM prompt (lightly adapted) to
    pull out structured candidates. Surfaces sealed_box / starter_deck /
    booster_pack / promo signals that don't show up on the hot-card board.
    """

    def __init__(
        self,
        *,
        search_fn,
        llm_fn,
        queries: Sequence[str] = DEFAULT_WEB_TREND_QUERIES,
        results_per_query: int = 5,
    ) -> None:
        self._search_fn = search_fn
        self._llm_fn = llm_fn
        self._queries = tuple(queries)
        self._results_per_query = max(1, results_per_query)

    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        if not self._queries:
            return ()
        snippets: list[WebSearchResult] = []
        for query in self._queries:
            try:
                results = self._search_fn(query, max_results=self._results_per_query)
            except Exception:
                logger.exception("ScheduledWebSearchCandidateProvider search failed query=%s", query)
                continue
            if results:
                snippets.extend(results)
        if not snippets:
            return ()
        pseudo_posts = _snippets_as_pseudo_posts(snippets)
        prompt = _build_web_trend_candidate_prompt(snippets, limit=limit)
        try:
            raw = self._llm_fn(prompt)
        except Exception:
            logger.exception("ScheduledWebSearchCandidateProvider LLM extraction failed")
            return ()
        candidates = list(_parse_candidate_response(raw, posts=pseudo_posts, limit=limit))
        retagged: list[OpportunityCandidate] = []
        for candidate in candidates:
            metadata = dict(candidate.metadata)
            metadata.setdefault("source_urls", [r.url for r in snippets[:5]])
            retagged.append(
                OpportunityCandidate(
                    candidate_id=candidate.candidate_id,
                    game=candidate.game,
                    product_type=candidate.product_type,
                    title=candidate.title,
                    product_identifier=candidate.product_identifier,
                    search_query=candidate.search_query,
                    heat_score=candidate.heat_score,
                    reason=candidate.reason,
                    source_kind="web_trend_search",
                    source_url=candidate.source_url,
                    metadata=metadata,
                    created_at=candidate.created_at,
                )
            )
        return tuple(retagged)


def _snippets_as_pseudo_posts(snippets: Sequence[WebSearchResult]) -> tuple[SnsPost, ...]:
    """Wrap web search results as SnsPost shapes so we can reuse
    `_parse_candidate_response` (which expects a `posts` collection to
    validate `source_tweet_ids` against). The URL becomes the tweet_id.
    """
    posts: list[SnsPost] = []
    for snippet in snippets:
        posts.append(
            SnsPost(
                tweet_id=snippet.url,
                author_handle=snippet.url,
                text=f"{snippet.title} — {snippet.snippet}",
                created_at="",
                rule_label="web_trend",
            )
        )
    return tuple(posts)


def _build_web_trend_candidate_prompt(snippets: Sequence[WebSearchResult], *, limit: int) -> str:
    """Same overall instructions as `_build_sns_candidate_prompt` but the
    inputs are search-engine snippets instead of tweets. We reuse the same
    parser (`_parse_candidate_response`) so the prompt's output schema must
    match exactly.
    """
    lines = [
        "你是 OpenClaw 的商品機會偵測器。以下是搜尋引擎回的 title + snippet，請從中找出真正能在二級市場交易的 TCG 商品。",
        f"只接受 {supported_game_hint()}。忽略不明確、不是商品、或沒有買賣價值的話題。",
        "忽略明顯不在支援範圍的系列，例如デュエルマスターズ、ONE PIECE CARD GAME、Dragon Ball。",
        "",
        "每個候選必須描述「同一個」具體商品，並且帶上三層結構：",
        "- game (IP)：pokemon / ws / yugioh / union_arena",
        "- product_type：single_card / booster_pack / sealed_box / starter_deck / promo / other",
        "- title：可在二級市場搜尋到的具體商品名（不要包含「抽選情報」「予約情報」等情報詞）",
        "- product_identifier：單張卡填卡號、整盒填 set code、其他可為 null",
        "- search_query：Mercari 搜尋用的關鍵字",
        "",
        "如果一則 snippet 同時提到多個不同 product_type 的商品，要拆成多個 candidate；商品名本身內含的「・」要保留。",
        f"最多輸出 {limit} 個候選。",
        "",
        "請嚴格輸出 JSON：",
        '{"candidates":[{"game":"...","product_type":"...","title":"...","product_identifier":"...|null","search_query":"...","heat_score":0-100,"reason":"...","source_tweet_ids":["<URL>"]}]}',
        "",
        "搜尋結果：",
    ]
    for index, snippet in enumerate(snippets, 1):
        text = " ".join(f"{snippet.title} — {snippet.snippet}".split())
        if len(text) > 260:
            text = text[:260] + "..."
        lines.append(f"[{index}] url={snippet.url}: {text}")
    return "\n".join(lines)


# ─── Chained candidate provider (compose multiple providers) ─────────────────

class ChainedCandidateProvider:
    """Run a list of providers in order, merge their outputs, dedupe by
    candidate_id, sort by heat_score descending, and truncate to `limit`.
    """

    def __init__(self, providers: Sequence["CandidateProvider"]) -> None:
        self._providers = tuple(providers)

    def discover(self, *, limit: int) -> Sequence[OpportunityCandidate]:
        seen: dict[str, OpportunityCandidate] = {}
        for provider in self._providers:
            try:
                discovered = provider.discover(limit=limit)
            except Exception:
                logger.exception(
                    "ChainedCandidateProvider sub-provider failed type=%s",
                    type(provider).__name__,
                )
                continue
            for candidate in discovered:
                if candidate.candidate_id in seen:
                    # Same ID — keep whichever has the higher heat_score.
                    if candidate.heat_score > seen[candidate.candidate_id].heat_score:
                        seen[candidate.candidate_id] = candidate
                    continue
                seen[candidate.candidate_id] = candidate
        ranked = sorted(seen.values(), key=lambda c: c.heat_score, reverse=True)
        return tuple(ranked[:limit])


class WebOpportunityResearcher:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: int,
        max_results: int = 3,
        ssl_context: ssl.SSLContext | None = None,
        search_fn=None,
        json_call_fn=None,
    ) -> None:
        self._endpoint = endpoint
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_results = max(1, min(5, max_results))
        self._ssl_context = ssl_context
        self._search_fn = search_fn or (
            lambda query, limit: search_duckduckgo(
                query,
                max_results=limit,
                ssl_context=ssl_context,
            )
        )
        self._json_call_fn = json_call_fn or _call_ollama_json

    def enrich(self, candidate: OpportunityCandidate) -> OpportunityCandidate:
        query = _build_opportunity_research_query(candidate)
        sources = tuple(self._search_fn(query, self._max_results))
        if not sources:
            return candidate

        assessment = _default_web_assessment(candidate, query=query, sources=sources)
        if self._endpoint and self._model:
            prompt = _build_opportunity_web_assessment_prompt(candidate, query=query, sources=sources)
            try:
                raw = self._json_call_fn(
                    endpoint=self._endpoint,
                    model=self._model,
                    prompt=prompt,
                    timeout_seconds=self._timeout_seconds,
                    ssl_context=self._ssl_context,
                )
                assessment = _parse_web_assessment(raw, fallback=assessment)
            except Exception:
                logger.exception("Opportunity web research LLM assessment failed candidate_id=%s", candidate.candidate_id)

        heat_score = _apply_web_assessment_to_heat(candidate.heat_score, assessment)
        metadata = dict(candidate.metadata)
        metadata["web_research"] = {
            "query": query,
            "assessment": {
                "is_relevant": assessment.is_relevant,
                "demand_score": assessment.demand_score,
                "reason": assessment.reason,
            },
            "sources": [_source_to_metadata(source) for source in sources],
        }

        reason = candidate.reason
        if assessment.reason:
            reason = f"{reason} 網路佐證：{assessment.reason}"

        return replace(
            candidate,
            heat_score=heat_score,
            reason=reason,
            source_kind=_append_source_kind(candidate.source_kind, "web"),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class WebOpportunityAssessment:
    is_relevant: bool
    demand_score: float
    reason: str


class TcgFairValueChecker:
    def __init__(self, *, db_path: str | Path) -> None:
        self._db_path = db_path

    def check(self, candidate: OpportunityCandidate) -> PriceCheck | None:
        game = normalize_game_key(candidate.game)
        if game is None:
            logger.info("Opportunity price skipped unsupported game=%s title=%s", candidate.game, candidate.title)
            return None
        result = lookup_card(
            db_path=self._db_path,
            game=game,
            name=candidate.title,
            persist=True,
        )
        if result.fair_value is None:
            logger.info("Opportunity price skipped no fair value title=%s offers=%d", candidate.title, len(result.offers))
            return None
        return PriceCheck(
            candidate_id=candidate.candidate_id,
            fair_value_jpy=result.fair_value.amount_jpy,
            confidence=result.fair_value.confidence,
            sample_count=result.fair_value.sample_count,
            notes=tuple(result.notes),
        )


class MercariOpportunityListingFinder:
    def find(self, candidate: OpportunityCandidate, *, price_max_jpy: int, limit: int) -> Sequence[ListingOffer]:
        results = search_mercari(candidate.search_query or candidate.title, price_max=price_max_jpy, max_results=limit)
        offers: list[ListingOffer] = []
        for raw in results:
            url = str(raw.get("url") or "")
            if not url:
                continue
            listing_id = str(raw.get("item_id") or "") or build_listing_key(url)
            try:
                price_jpy = int(raw.get("price_jpy") or 0)
            except (TypeError, ValueError):
                continue
            if price_jpy <= 0:
                continue
            offers.append(
                ListingOffer(
                    listing_id=listing_id,
                    title=str(raw.get("title") or ""),
                    price_jpy=price_jpy,
                    url=url,
                    thumbnail_url=str(raw.get("thumbnail_url") or "") or None,
                )
            )
        return tuple(offers)


class ReputationSnapshotOpportunityChecker:
    def __init__(
        self,
        *,
        server_url: str,
        thresholds: OpportunityThresholds,
        timeout_seconds: int = 240,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._thresholds = thresholds
        self._timeout_seconds = timeout_seconds

    def check(self, listing: ListingOffer) -> ReputationCheck:
        try:
            proof_url = self._request_snapshot(listing.url)
            proof = self._fetch_proof(proof_url)
        except Exception as exc:
            logger.exception("Opportunity reputation snapshot failed listing=%s: %s", listing.url, exc)
            return ReputationCheck(
                listing_url=listing.url,
                trusted=False,
                status="failed",
                reason=f"Snapshot failed: {exc}",
            )

        total_reviews = _as_int_or_none((proof.get("metrics") or {}).get("total_reviews"))
        quality = proof.get("quality") or {}
        overall = quality.get("overall") or {}
        positive_rate = _as_float_or_none(overall.get("rate"))
        passed, reason = reputation_passes(
            ReputationCheck(
                listing_url=listing.url,
                trusted=False,
                proof_url=_absolute_url(self._server_url, proof_url),
                total_reviews=total_reviews,
                positive_rate=positive_rate,
                status=str(proof.get("status") or "unknown"),
            ),
            self._thresholds,
        )
        return ReputationCheck(
            listing_url=listing.url,
            trusted=passed,
            proof_url=_absolute_url(self._server_url, proof_url),
            total_reviews=total_reviews,
            positive_rate=positive_rate,
            grade=None,
            status=str(proof.get("status") or "unknown"),
            reason=reason,
        )

    def _request_snapshot(self, listing_url: str) -> str:
        payload = json.dumps({"query_url": listing_url}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._server_url}/api/captures",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if "proof_url" in data:
            return str(data["proof_url"])
        job_id = str(data.get("job_id") or "")
        if not job_id:
            raise RuntimeError(f"Snapshot request returned no job id: {data}")

        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"{self._server_url}/api/jobs/{job_id}", timeout=15) as response:
                status_payload = json.loads(response.read().decode("utf-8"))
            status = str(status_payload.get("status") or "")
            if status == "done":
                proof_url = str(status_payload.get("proof_url") or "")
                if not proof_url:
                    raise RuntimeError(f"Snapshot job finished without proof_url: {status_payload}")
                return proof_url
            if status == "failed":
                raise RuntimeError(str(status_payload.get("error") or "Snapshot job failed."))
            time.sleep(3)
        raise TimeoutError(f"Snapshot job {job_id} did not finish within {self._timeout_seconds}s.")

    def _fetch_proof(self, proof_url: str) -> dict[str, Any]:
        proof_id = proof_url.rstrip("/").split("/")[-1]
        if not proof_id:
            raise RuntimeError(f"Invalid proof URL: {proof_url}")
        with urllib.request.urlopen(f"{self._server_url}/api/proofs/{proof_id}", timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Proof response was not a JSON object.")
        return payload


class TelegramOpportunityNotifier:
    def __init__(
        self,
        *,
        token: str | None,
        chat_ids: Sequence[str],
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._token = token
        self._chat_ids = tuple(chat_id for chat_id in chat_ids if chat_id)
        self._ssl_context = ssl_context

    def notify(self, recommendation: OpportunityRecommendation) -> None:
        text = format_opportunity_recommendation(recommendation)
        if not self._token or not self._chat_ids:
            logger.warning("Opportunity recommendation ready but Telegram token/chat ids are not configured:\n%s", text)
            return
        client = TelegramBotClient(self._token, ssl_context=self._ssl_context)
        for chat_id in self._chat_ids:
            client.send_message(chat_id=chat_id, text=text)


class OpportunityAgent:
    def __init__(
        self,
        *,
        pipeline: OpportunityPipeline,
        interval_seconds: int,
        preflight_fn=None,
    ) -> None:
        self._pipeline = pipeline
        self._interval_seconds = interval_seconds
        self._preflight_fn = preflight_fn

    def run_once(self) -> OpportunityPipelineStats:
        if self._preflight_fn is not None:
            try:
                self._preflight_fn()
            except Exception:
                logger.exception("Opportunity agent preflight failed")
        return self._pipeline.run_once()

    def run_forever(self) -> None:
        logger.info("Opportunity agent started interval_seconds=%d", self._interval_seconds)
        while True:
            try:
                stats = self.run_once()
                logger.info("Opportunity agent tick completed stats=%s", stats)
            except Exception:
                logger.exception("Opportunity agent tick failed")
            time.sleep(max(10, self._interval_seconds))


def build_opportunity_agent(settings: AssistantSettings | None = None) -> OpportunityAgent:
    settings = settings or get_settings()
    thresholds = OpportunityThresholds(
        min_heat_score=settings.opportunity_min_heat_score,
        max_price_ratio=settings.opportunity_max_price_ratio,
        min_price_confidence=settings.opportunity_min_price_confidence,
        min_total_reviews=settings.opportunity_min_total_reviews,
        min_positive_rate=settings.opportunity_min_positive_rate,
    )
    ssl_context = build_ssl_context(settings)
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    text_model = (settings.openclaw_local_text_model or "").split(",")[0].strip()
    sns_provider = SnsLlmCandidateProvider(
        db_path=settings.sns_db_path,
        endpoint=settings.openclaw_local_text_endpoint,
        model=text_model,
        timeout_seconds=settings.opportunity_llm_timeout_seconds,
        lookback_hours=settings.opportunity_sns_lookback_hours,
        ssl_context=ssl_context if settings.openclaw_local_text_endpoint.startswith("https://") else None,
    )

    sub_providers: list[CandidateProvider] = [sns_provider]

    if settings.opportunity_hot_card_provider_enabled:
        try:
            hot_card_service = TcgHotCardService()
            sub_providers.append(
                HotCardBoardCandidateProvider(
                    hot_card_service=hot_card_service,
                    per_game_limit=settings.opportunity_hot_card_per_game_limit,
                    min_hot_score=settings.opportunity_hot_card_min_score,
                )
            )
            logger.info("Opportunity agent: HotCardBoardCandidateProvider enabled")
        except Exception:
            logger.exception("Failed to wire HotCardBoardCandidateProvider; skipping")

    if settings.opportunity_web_trend_provider_enabled and text_model:
        text_endpoint = settings.openclaw_local_text_endpoint
        timeout_seconds = settings.opportunity_llm_timeout_seconds
        text_ssl = ssl_context if text_endpoint.startswith("https://") else None

        def _llm_fn(prompt: str, *, endpoint=text_endpoint, model=text_model,
                    timeout=timeout_seconds, ssl=text_ssl) -> str:
            return _call_ollama_json(
                endpoint=endpoint, model=model, prompt=prompt,
                timeout_seconds=timeout, ssl_context=ssl,
            )

        queries = settings.opportunity_web_trend_queries or DEFAULT_WEB_TREND_QUERIES
        sub_providers.append(
            ScheduledWebSearchCandidateProvider(
                search_fn=search_duckduckgo,
                llm_fn=_llm_fn,
                queries=queries,
                results_per_query=settings.opportunity_web_trend_results_per_query,
            )
        )
        logger.info(
            "Opportunity agent: ScheduledWebSearchCandidateProvider enabled queries=%d",
            len(queries),
        )

    candidate_provider: CandidateProvider = (
        sub_providers[0] if len(sub_providers) == 1 else ChainedCandidateProvider(sub_providers)
    )

    if (settings.openclaw_local_text_backend or "").strip().lower() == "ollama" and text_model:
        candidate_provider = WebResearchCandidateProvider(
            base_provider=candidate_provider,
            researcher=WebOpportunityResearcher(
                endpoint=settings.openclaw_local_text_endpoint,
                model=text_model,
                timeout_seconds=settings.opportunity_llm_timeout_seconds,
                ssl_context=ssl_context,
            ),
        )
    pipeline = OpportunityPipeline(
        store=store,
        candidate_provider=candidate_provider,
        price_checker=TcgFairValueChecker(db_path=settings.monitor_db_path),
        listing_finder=MercariOpportunityListingFinder(),
        reputation_checker=ReputationSnapshotOpportunityChecker(
            server_url=settings.reputation_agent_server_url,
            thresholds=thresholds,
        ),
        notifier=TelegramOpportunityNotifier(
            token=settings.openclaw_telegram_bot_token,
            chat_ids=settings.openclaw_telegram_chat_ids,
            ssl_context=ssl_context,
        ),
        thresholds=thresholds,
        candidate_limit=settings.opportunity_candidate_limit,
        listing_limit=settings.opportunity_listing_limit,
        candidate_check_interval_seconds=settings.opportunity_candidate_check_interval_seconds,
    )

    preflight_fn = _build_preflight_callable(settings=settings, ssl_context=ssl_context)
    return OpportunityAgent(
        pipeline=pipeline,
        interval_seconds=settings.opportunity_interval_seconds,
        preflight_fn=preflight_fn,
    )


def _build_preflight_callable(*, settings: AssistantSettings, ssl_context):
    """Return a callable that the opportunity-agent invokes before every tick:

    - Backfill domains for one untagged SNS rule (LLM-driven).
    - Once every N hours, run SNS account auto-discovery to add new TCG
      handles via search engine + LLM.

    Returns None when both features are disabled, so the agent skips the
    preflight step entirely.
    """
    backfill_enabled = settings.opportunity_sns_domain_backfill_enabled
    discovery_enabled = settings.opportunity_sns_auto_discovery_enabled
    if not (backfill_enabled or discovery_enabled):
        return None
    text_model = (settings.openclaw_local_text_model or "").split(",")[0].strip()
    if not text_model:
        # No local text model available → can't drive backfill / discovery.
        return None
    text_endpoint = settings.openclaw_local_text_endpoint
    timeout_seconds = settings.opportunity_llm_timeout_seconds
    text_ssl = ssl_context if text_endpoint.startswith("https://") else None

    def _llm_fn(prompt: str) -> str:
        return _call_ollama_json(
            endpoint=text_endpoint,
            model=text_model,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            ssl_context=text_ssl,
        )

    notifier_state = {"client": None}

    def _telegram_notify(text: str) -> None:
        chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
        token = settings.openclaw_telegram_bot_token
        if not token or not chat_ids:
            logger.warning(
                "Opportunity preflight notify suppressed (no telegram token / chat ids):\n%s",
                text,
            )
            return
        if notifier_state["client"] is None:
            notifier_state["client"] = TelegramBotClient(token, ssl_context=ssl_context)
        for chat_id in chat_ids:
            notifier_state["client"].send_message(chat_id=chat_id, text=text)

    last_discovery_at = {"value": 0.0}
    discovery_interval_seconds = max(60, settings.opportunity_sns_auto_discovery_interval_hours * 3600)
    primary_chat_id = settings.openclaw_telegram_chat_id or ""

    def preflight() -> None:
        from sns_monitor.storage import SnsDatabase
        from .opportunity_sns_discovery import discover_tcg_sns_accounts
        from .opportunity_sns_domain_backfill import backfill_missing_domains

        sns_db = SnsDatabase(settings.sns_db_path)
        if backfill_enabled:
            try:
                backfill_missing_domains(
                    sns_db=sns_db,
                    sns_db_path=settings.sns_db_path,
                    llm_fn=_llm_fn,
                    telegram_notify_fn=_telegram_notify,
                    limit=1,
                )
            except Exception:
                logger.exception("Opportunity preflight: domain backfill failed")
        if discovery_enabled:
            now = time.monotonic()
            if now - last_discovery_at["value"] >= discovery_interval_seconds:
                last_discovery_at["value"] = now
                try:
                    discover_tcg_sns_accounts(
                        sns_db=sns_db,
                        search_fn=search_duckduckgo,
                        llm_fn=_llm_fn,
                        telegram_notify_fn=_telegram_notify,
                        chat_id=primary_chat_id,
                        max_new_per_run=settings.opportunity_sns_auto_discovery_max_new_per_run,
                        min_confidence=settings.opportunity_sns_auto_discovery_min_confidence,
                    )
                except Exception:
                    logger.exception("Opportunity preflight: account auto-discovery failed")

    return preflight


def format_opportunity_recommendation(recommendation: OpportunityRecommendation) -> str:
    c = recommendation.candidate
    p = recommendation.price
    listing = recommendation.listing
    rep = recommendation.reputation
    lines = [
        "發現可能值得看的商品",
        "",
        f"商品：{c.title}",
        f"熱度：{c.heat_score:.0f}/100",
        f"理由：{c.reason}",
    ]
    web_sources = _web_research_sources_from_metadata(c.metadata)
    if web_sources:
        lines.extend(["", "市場佐證："])
        for index, source in enumerate(web_sources[:3], 1):
            title = source.get("title") or "source"
            url = source.get("url") or ""
            lines.append(f"[{index}] {title}")
            if url:
                lines.append(url)
    lines.extend([
        "",
        f"合理價：約 ¥{p.fair_value_jpy:,}",
        f"目前售價：¥{listing.price_jpy:,}",
        f"折扣：約 {recommendation.discount_pct:.1f}%",
        f"機會分數：{recommendation.score:.1f}/100",
        "",
        "賣家信譽：",
        f"評價率：{_format_optional_pct(rep.positive_rate)}",
        f"總評價：{_format_optional_int(rep.total_reviews)}",
        f"Snapshot：{rep.proof_url or 'n/a'}",
        "",
        "商品連結：",
        listing.url,
        "",
        "判斷：價格低於目標價，賣家信譽通過，值得人工確認。",
    ])
    return "\n".join(lines)


def _format_title_with_identifier(*, title: str, product_type: str, identifier: str | None) -> str:
    if not identifier:
        return title
    if product_type == "single_card":
        return f"{title} ({identifier})"
    if product_type in {"sealed_box", "booster_pack"}:
        return f"{title} [{identifier}]"
    return title


def format_opportunity_status(settings: AssistantSettings, *, limit: int = 10) -> str:
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    candidates = store.list_recent_candidates(limit=limit)
    recommendations = store.list_recent_recommendations(limit=min(limit, 5))

    lines = [
        "OpenClaw Opportunity Agent",
        f"status: {'enabled' if settings.opportunity_agent_enabled else 'disabled'}",
        f"db: {settings.opportunity_db_path}",
        f"interval: {settings.opportunity_interval_seconds}s",
        f"thresholds: heat>={settings.opportunity_min_heat_score:.0f}, price<={settings.opportunity_max_price_ratio:.0%}, seller>={settings.opportunity_min_positive_rate:.1f}%/{settings.opportunity_min_total_reviews} reviews",
        "",
        f"候選目標（最近 {len(candidates)} 筆）",
    ]
    if not candidates:
        lines.append("目前沒有候選目標。等 SNS monitor 收到貼文後，下一輪會開始萃取。")
    else:
        for index, row in enumerate(candidates, 1):
            checked = row["last_checked_at"] or "尚未檢查"
            product_type = (
                row["product_type"] if "product_type" in row.keys() and row["product_type"] else "other"
            )
            identifier = row["product_identifier"] if "product_identifier" in row.keys() else None
            title_with_id = _format_title_with_identifier(
                title=row["title"],
                product_type=product_type,
                identifier=identifier,
            )
            lines.append(
                f"{index}. [{row['game']} / {product_type}] {title_with_id} | heat={float(row['heat_score']):.0f} | checked={checked}"
            )
            lines.append(f"   search: {row['search_query']}")
            if row["reason"]:
                lines.append(f"   reason: {row['reason']}")

    lines.append("")
    lines.append(f"最近推薦紀錄（最近 {len(recommendations)} 筆）")
    if not recommendations:
        lines.append("目前還沒有推薦紀錄。")
    else:
        for row in recommendations:
            status = "sent" if row["notified_at"] else ("accepted" if row["accepted"] else "rejected")
            lines.append(
                f"- {status} | {row['listing_title']} | ¥{int(row['listing_price_jpy']):,} | score={float(row['opportunity_score']):.1f}"
            )
            lines.append(f"  {row['listing_url']}")
    return "\n".join(lines)


def dismiss_opportunity_target(settings: AssistantSettings, target: str, *, limit: int = 30) -> str:
    selector = " ".join(target.split()).strip()
    if not selector:
        return "請提供要移除的機會目標，例如：/hunt remove 2 或 /hunt remove Umbreon ex SAR"

    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    candidates = store.list_recent_candidates(limit=max(1, limit))
    if not candidates:
        return "目前沒有可移除的機會目標。"

    resolved = _resolve_candidate_selector(candidates, selector)
    if isinstance(resolved, str):
        return resolved

    removed = store.dismiss_candidate(str(resolved["candidate_id"]))
    if not removed:
        return f"找不到可移除的 active 目標：{selector}"

    return (
        "已從機會清單移除\n"
        f"目標：[{resolved['game']}] {resolved['title']}\n"
        "之後相同 candidate_id 再出現時會保持隱藏。"
    )


def _resolve_candidate_selector(candidates: Sequence[Any], selector: str) -> Any | str:
    lowered = selector.lower()
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        return f"找不到第 {index} 個目標。請先用 /hunt status 看目前清單。"

    id_matches = [
        row for row in candidates
        if str(row["candidate_id"]).lower().startswith(lowered)
    ]
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        return _format_ambiguous_candidate_matches(id_matches)

    exact_matches = [
        row for row in candidates
        if lowered in {str(row["title"]).lower(), str(row["search_query"]).lower()}
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return _format_ambiguous_candidate_matches(exact_matches)

    partial_matches = [
        row for row in candidates
        if lowered in str(row["title"]).lower() or lowered in str(row["search_query"]).lower()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]
    if len(partial_matches) > 1:
        return _format_ambiguous_candidate_matches(partial_matches)

    return f"找不到符合「{selector}」的 active 目標。請先用 /hunt status 確認名稱或編號。"


def _format_ambiguous_candidate_matches(matches: Sequence[Any]) -> str:
    lines = ["找到多個可能目標，請用更完整名稱或 candidate_id 前綴指定："]
    for row in matches[:8]:
        lines.append(
            f"- {str(row['candidate_id'])[:12]} | [{row['game']}] {row['title']} | search: {row['search_query']}"
        )
    return "\n".join(lines)


def _build_sns_candidate_prompt(posts: Sequence[SnsPost], *, limit: int) -> str:
    lines = [
        "你是 OpenClaw 的商品機會偵測器。請從 SNS 貼文中找出有交易潛力的 TCG/收藏卡商品。",
        f"只接受 {supported_game_hint()}。忽略不明確、不是商品、或沒有買賣價值的話題。",
        "忽略明顯不在支援範圍的系列，例如デュエルマスターズ、ONE PIECE CARD GAME、Dragon Ball。",
        "",
        "每個候選必須描述「同一個」具體商品，並且帶上三層結構：",
        "- game (Layer 1, IP)：pokemon / ws / yugioh / union_arena",
        "- product_type (Layer 2, 商品類型)：必須是下列其中之一：",
        "    single_card  - 單張卡片（例：ピカチュウex SAR、青眼の白龍 QCCP-JP001）",
        "    booster_pack - 拆售或補充包（例：強化拡張パック 単品）",
        "    sealed_box   - 整盒、整箱、display（例：強化拡張パック ボックス、ハイクラスパック ボックス）",
        "    starter_deck - 預組產品（例：スタートデッキ100、Structure Deck、Trial Deck）",
        "    promo        - 抽選 / promo 卡（例：プロモパック）",
        "    other        - 不屬上面五類",
        "- title (Layer 3)：可在二級市場搜尋到的具體商品名（不要包含「抽選情報」「予約情報」等情報詞）",
        "- product_identifier (Layer 3 細項)：單張卡填卡號（例 201/165、QCCP-JP001），整盒填 set code（例 sv-p），其他可為 null",
        "- search_query：Mercari 搜尋用的關鍵字",
        "",
        "拆分規則（重要）：",
        "- 如果同一則貼文同時提到多個不同 product_type 的商品（例如「擴充包系列」+「Start Deck 產品」），必須拆成多個 candidate，每個 candidate 只描述一個具體商品。",
        "- 即使兩個商品都是同一個 IP、即使在同一個句子裡用「・」「／」「、」「&」「+」分隔，只要是兩個獨立商品線（不同 product_type，或同 type 但不同商品），就要拆開成多個 candidate。",
        "- 但是商品名本身內含的「・」（例：同一張卡上的多個寶可夢名）要保留不拆。",
        "",
        "Title 規則：",
        "- 如果貼文是「商品名 + 抽選情報/予約情報/発売情報」，title 只保留商品名。",
        "- 如果貼文是「セット名収録 卡名」，title 優先輸出卡名（product_type=single_card）；除非整套 set 本身才是商品（product_type=sealed_box 或 booster_pack）。",
        f"最多輸出 {limit} 個候選。",
        "",
        "請嚴格輸出 JSON，不要 markdown：",
        '{"candidates":[{"game":"pokemon|ws|yugioh|union_arena","product_type":"single_card|booster_pack|sealed_box|starter_deck|promo|other","title":"商品名","product_identifier":"卡號或set code或null","search_query":"Mercari 關鍵字","heat_score":0-100,"reason":"一句話原因","source_tweet_ids":["..."]}]}',
        "",
        "正確例子：",
        "- 貼文「インフェルノX・スタートデッキ100 抽選情報」",
        "  -> 兩個 candidate：",
        '     {"game":"pokemon","product_type":"sealed_box","title":"インフェルノX","product_identifier":null,...}',
        '     {"game":"pokemon","product_type":"starter_deck","title":"スタートデッキ100","product_identifier":null,...}',
        "- 貼文「アビスアイ収録 ホエルオーex 201/165 SAR」",
        "  -> 一個 candidate：",
        '     {"game":"pokemon","product_type":"single_card","title":"ホエルオーex","product_identifier":"201/165",...}',
        "- 貼文「ピカチュウ・カビゴンex（同一張卡）」",
        "  -> 一個 candidate（保留商品名內固有的「・」）：",
        '     {"game":"pokemon","product_type":"single_card","title":"ピカチュウ・カビゴンex","product_identifier":null,...}',
        "- 貼文「カスミの元気 Mercari」",
        '  -> {"game":"pokemon","product_type":"single_card","title":"カスミの元気","search_query":"カスミの元気",...}',
        "",
        "SNS 貼文：",
    ]
    for index, post in enumerate(posts, 1):
        text = " ".join(post.text.split())
        if len(text) > 260:
            text = text[:260] + "..."
        lines.append(
            f"[{index}] id={post.tweet_id} rule={post.rule_label} author={post.author_handle} date={post.created_at}: {text}"
        )
    return "\n".join(lines)


def _build_opportunity_research_query(candidate: OpportunityCandidate) -> str:
    game_label = {
        "pokemon": "Pokemon card",
        "ws": "Weiss Schwarz card",
        "yugioh": "Yu-Gi-Oh card",
        "union_arena": "Union Arena card",
    }.get(candidate.game, f"{candidate.game} card")
    topic = candidate.search_query or candidate.title
    return f"{topic} {game_label} demand popularity price trend resale"


def _build_opportunity_web_assessment_prompt(
    candidate: OpportunityCandidate,
    *,
    query: str,
    sources: Sequence[WebSearchResult],
) -> str:
    lines = [
        "You are evaluating whether a TCG/collectible-card opportunity has real outside-market support.",
        "CRITICAL LANGUAGE RULE: The JSON reason value must be Traditional Chinese as used in Taiwan (zh-TW).",
        "Do not write the reason in English, Japanese, Simplified Chinese, or Mainland Chinese phrasing.",
        "Use only the provided web search results. Do not invent facts.",
        "Return strict JSON only with this shape:",
        '{"is_relevant":true,"demand_score":0-100,"reason":"一句繁體中文（台灣）佐證原因"}',
        "",
        f"Candidate game: {candidate.game}",
        f"Candidate title: {candidate.title}",
        f"Candidate Mercari search query: {candidate.search_query}",
        f"SNS heat score: {candidate.heat_score:.0f}/100",
        f"SNS reason: {candidate.reason}",
        f"Web query: {query}",
        "",
        "Search results:",
    ]
    for index, source in enumerate(sources, 1):
        lines.append(f"[{index}] {source.title}")
        lines.append(f"URL: {source.url}")
        lines.append(f"Snippet: {source.snippet or '(no snippet)'}")
    lines.extend(
        [
            "",
            "Scoring guidance:",
            "- 80-100: strong demand signal, sellouts, price movement, releases, or collector attention.",
            "- 55-79: plausible interest but limited evidence.",
            "- 0-54: weak, unrelated, stale, or source evidence does not support the candidate.",
            "- is_relevant=false if results are mostly about the wrong franchise, wrong product, or generic unrelated content.",
        ]
    )
    return "\n".join(lines)


def _parse_web_assessment(raw: str, *, fallback: WebOpportunityAssessment) -> WebOpportunityAssessment:
    try:
        payload = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        logger.warning("Opportunity web assessment response was not JSON: %s", raw[:500])
        return fallback
    if not isinstance(payload, dict):
        return fallback
    return WebOpportunityAssessment(
        is_relevant=_as_bool(payload.get("is_relevant"), default=fallback.is_relevant),
        demand_score=_clamp_float(
            payload.get("demand_score"),
            minimum=0.0,
            maximum=100.0,
            default=fallback.demand_score,
        ),
        reason=str(payload.get("reason") or fallback.reason).strip(),
    )


def _default_web_assessment(
    candidate: OpportunityCandidate,
    *,
    query: str,
    sources: Sequence[WebSearchResult],
) -> WebOpportunityAssessment:
    signal_hits = 0
    signal_terms = (
        "popular",
        "popularity",
        "demand",
        "trend",
        "price",
        "sold out",
        "resale",
        "collector",
        "高騰",
        "人気",
        "注目",
        "予約",
        "抽選",
        "再販",
    )
    haystack = " ".join(f"{source.title} {source.snippet}" for source in sources).lower()
    for term in signal_terms:
        if term.lower() in haystack:
            signal_hits += 1
    demand_score = min(100.0, max(candidate.heat_score, 50.0 + len(sources) * 4.0 + signal_hits * 4.0))
    first_title = sources[0].title if sources else query
    return WebOpportunityAssessment(
        is_relevant=True,
        demand_score=demand_score,
        reason=f"找到 {len(sources)} 個網路來源；第一筆結果是「{first_title}」。",
    )


def _apply_web_assessment_to_heat(current_heat: float, assessment: WebOpportunityAssessment) -> float:
    if not assessment.is_relevant:
        return round(max(0.0, current_heat - 15.0), 1)
    blended = current_heat * 0.70 + assessment.demand_score * 0.30
    return round(max(current_heat, min(100.0, blended)), 1)


def _source_to_metadata(source: WebSearchResult) -> dict[str, str]:
    return {
        "title": source.title,
        "url": source.url,
        "snippet": source.snippet,
    }


def _append_source_kind(source_kind: str, suffix: str) -> str:
    parts = [part for part in source_kind.split("+") if part]
    if suffix not in parts:
        parts.append(suffix)
    return "+".join(parts) or suffix


def _call_ollama_json(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
    ssl_context: ssl.SSLContext | None,
) -> str:
    url = endpoint.rstrip("/")
    if not url.endswith("/api/generate"):
        url = f"{url}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 700},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("response") or "").strip()


def _parse_candidate_response(raw: str, *, posts: Sequence[SnsPost], limit: int) -> list[OpportunityCandidate]:
    try:
        payload = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        logger.warning("Opportunity candidate response was not JSON: %s", raw[:500])
        return []
    raw_candidates = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(raw_candidates, list):
        return []

    known_tweet_ids = {post.tweet_id for post in posts}
    candidates: list[OpportunityCandidate] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        game = normalize_game_key(str(item.get("game") or "").strip())
        raw_title = str(item.get("title") or "").strip()
        raw_search_query = str(item.get("search_query") or raw_title).strip()
        title = _normalize_product_title(raw_title)
        search_query = _normalize_search_query(raw_search_query, fallback=title)
        if game is None or not title or not search_query or _looks_like_unsupported_franchise(title):
            continue
        product_type = normalize_product_type(item.get("product_type"))
        raw_identifier = item.get("product_identifier")
        if isinstance(raw_identifier, str):
            cleaned_identifier = raw_identifier.strip()
            product_identifier: str | None = cleaned_identifier or None
        else:
            product_identifier = None
        # Telemetry: surface candidates whose title still contains a typical
        # multi-product separator. If the LLM had correctly split, neither half
        # of the source would have these. Frequent hits in prod = signal to
        # tighten the prompt or add a second-pass split verifier.
        if any(sep in title for sep in ("・", "／", "、", "&")):
            logger.info(
                "Opportunity candidate title still contains a multi-product separator after LLM extraction title=%r game=%s product_type=%s",
                title,
                game,
                product_type,
            )
        heat_score = _clamp_float(item.get("heat_score"), minimum=0.0, maximum=100.0, default=0.0)
        source_ids = [
            str(source_id)
            for source_id in item.get("source_tweet_ids", [])
            if str(source_id) in known_tweet_ids
        ] if isinstance(item.get("source_tweet_ids"), list) else []
        candidates.append(
            OpportunityCandidate(
                candidate_id=build_candidate_id(
                    game=game,
                    product_type=product_type,
                    title=title,
                    search_query=search_query,
                    product_identifier=product_identifier,
                ),
                game=game,
                product_type=product_type,
                title=title,
                product_identifier=product_identifier,
                search_query=search_query,
                heat_score=heat_score,
                reason=str(item.get("reason") or "SNS discussion signal").strip(),
                source_kind="sns_llm",
                metadata={"source_tweet_ids": source_ids},
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def _normalize_product_title(title: str) -> str:
    cleaned = _normalize_candidate_spacing(title)
    cleaned = _strip_collectible_noise(cleaned, replacements=_PRODUCT_TITLE_NOISE_REPLACEMENTS)

    # Pattern: "set-name収録 card-name" or "set-name 収録 card-name".
    # For individual-card hunting, the card name is the tradable target.
    collected_match = re.search(r"^.+?収録\s*(?P<card>.+)$", cleaned)
    if collected_match:
        candidate = _normalize_candidate_spacing(collected_match.group("card"))
        if _looks_like_specific_product_name(candidate):
            cleaned = candidate

    return _normalize_candidate_spacing(cleaned)


def _normalize_search_query(search_query: str, *, fallback: str) -> str:
    cleaned = _normalize_candidate_spacing(search_query)
    cleaned = _strip_collectible_noise(cleaned, replacements=_SEARCH_QUERY_NOISE_REPLACEMENTS)
    cleaned = _normalize_product_title(cleaned)
    return cleaned or fallback


def _strip_collectible_noise(value: str, *, replacements: tuple[tuple[str, str], ...]) -> str:
    cleaned = value
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned)
    return _normalize_candidate_spacing(cleaned)


def _normalize_candidate_spacing(value: str) -> str:
    return " ".join(value.replace("　", " ").strip(" |/-_　").split())


def _looks_like_specific_product_name(value: str) -> bool:
    candidate = value.strip()
    if len(candidate) < 2:
        return False
    if any(token in candidate for token in ("情報", "ニュース", "まとめ", "抽選", "予約", "発売")):
        return False
    return True


def _looks_like_unsupported_franchise(value: str) -> bool:
    candidate = value.strip().lower()
    return any(marker in candidate for marker in _UNSUPPORTED_FRANCHISE_MARKERS)


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _clamp_float(value: object, *, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _as_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _as_int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _absolute_url(base: str, maybe_relative: str) -> str:
    if maybe_relative.startswith("http://") or maybe_relative.startswith("https://"):
        return maybe_relative
    return f"{base.rstrip('/')}/{maybe_relative.lstrip('/')}"


def _format_optional_int(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def _format_optional_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _web_research_sources_from_metadata(metadata: object) -> list[dict[str, str]]:
    if not isinstance(metadata, MappingABC):
        return []
    web_research = metadata.get("web_research")
    if not isinstance(web_research, MappingABC):
        return []
    sources = web_research.get("sources")
    if not isinstance(sources, list):
        return []
    normalized: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, MappingABC):
            continue
        url = str(source.get("url") or "").strip()
        title = str(source.get("title") or "").strip()
        if not url:
            continue
        normalized.append({"title": title or url, "url": url})
    return normalized


def run_opportunity_agent(*, settings: AssistantSettings | None = None, once: bool = False) -> OpportunityPipelineStats | None:
    agent = build_opportunity_agent(settings)
    if once:
        return agent.run_once()
    agent.run_forever()
    return None
