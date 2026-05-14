from __future__ import annotations

import json
import logging
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from assistant_runtime import AssistantSettings, build_ssl_context, get_settings
from market_monitor.mercari_search import search_mercari
from price_monitor_bot.bot import TelegramBotClient
from price_monitor_bot.commands import lookup_card
from tcg_tracker.catalog import normalize_game_key, supported_game_hint

from .opportunity_models import (
    ListingOffer,
    OpportunityCandidate,
    OpportunityRecommendation,
    PriceCheck,
    ReputationCheck,
    build_candidate_id,
    build_listing_key,
)
from .opportunity_pipeline import OpportunityPipeline, OpportunityPipelineStats
from .opportunity_scoring import OpportunityThresholds, reputation_passes
from .opportunity_store import OpportunityStore

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
                rows = connection.execute(
                    """
                    SELECT t.tweet_id, t.author_handle, t.text, t.created_at, r.label AS rule_label
                    FROM seen_tweets t
                    LEFT JOIN watch_rules r ON r.rule_id = t.rule_id
                    WHERE t.first_seen_at >= ? OR t.created_at >= ?
                    ORDER BY t.first_seen_at DESC
                    LIMIT ?
                    """,
                    (cutoff, cutoff, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("Opportunity SNS read failed path=%s error=%s", self._db_path, exc)
            return []
        return [
            SnsPost(
                tweet_id=str(row["tweet_id"]),
                author_handle=str(row["author_handle"]),
                text=str(row["text"]),
                created_at=str(row["created_at"]),
                rule_label=str(row["rule_label"] or ""),
            )
            for row in rows
        ]


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
    ) -> None:
        self._pipeline = pipeline
        self._interval_seconds = interval_seconds

    def run_once(self) -> OpportunityPipelineStats:
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
    pipeline = OpportunityPipeline(
        store=store,
        candidate_provider=SnsLlmCandidateProvider(
            db_path=settings.sns_db_path,
            endpoint=settings.openclaw_local_text_endpoint,
            model=(settings.openclaw_local_text_model or "").split(",")[0].strip(),
            timeout_seconds=settings.opportunity_llm_timeout_seconds,
            lookback_hours=settings.opportunity_sns_lookback_hours,
            ssl_context=ssl_context if settings.openclaw_local_text_endpoint.startswith("https://") else None,
        ),
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
    return OpportunityAgent(pipeline=pipeline, interval_seconds=settings.opportunity_interval_seconds)


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
    ]
    return "\n".join(lines)


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
            lines.append(
                f"{index}. [{row['game']}] {row['title']} | heat={float(row['heat_score']):.0f} | checked={checked}"
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


def _build_sns_candidate_prompt(posts: Sequence[SnsPost], *, limit: int) -> str:
    lines = [
        "你是 OpenClaw 的商品機會偵測器。請從 SNS 貼文中找出有交易潛力的 TCG/收藏卡商品。",
        f"只接受 {supported_game_hint()}。忽略不明確、不是商品、或沒有買賣價值的話題。",
        "忽略明顯不在支援範圍的系列，例如デュエルマスターズ、ONE PIECE CARD GAME、Dragon Ball。",
        "title 必須是真正能在二級市場交易/搜尋的商品名，不要包含情報詞、活動詞或來源詞。",
        "如果貼文是「商品名 + 抽選情報/予約情報/発売情報」，title 只保留商品名。",
        "如果貼文是「セット名収録 卡名」，title 優先輸出卡名；除非整個セット本身才是商品。",
        f"最多輸出 {limit} 個候選。",
        "",
        "請嚴格輸出 JSON，不要 markdown：",
        '{"candidates":[{"game":"pokemon|ws|yugioh|union_arena","title":"商品名","search_query":"Mercari 搜尋關鍵字","heat_score":0-100,"reason":"一句話原因","source_tweet_ids":["..."]}]}',
        "",
        "正確例子：",
        "- アビスアイ 抽選情報 -> title=アビスアイ, search_query=アビスアイ",
        "- アビスアイ収録 ホエルオーex -> title=ホエルオーex, search_query=ホエルオーex",
        "- カスミの元気 Mercari -> title=カスミの元気, search_query=カスミの元気",
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
        heat_score = _clamp_float(item.get("heat_score"), minimum=0.0, maximum=100.0, default=0.0)
        source_ids = [
            str(source_id)
            for source_id in item.get("source_tweet_ids", [])
            if str(source_id) in known_tweet_ids
        ] if isinstance(item.get("source_tweet_ids"), list) else []
        candidates.append(
            OpportunityCandidate(
                candidate_id=build_candidate_id(game=game, title=title, search_query=search_query),
                game=game,
                title=title,
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


def run_opportunity_agent(*, settings: AssistantSettings | None = None, once: bool = False) -> OpportunityPipelineStats | None:
    agent = build_opportunity_agent(settings)
    if once:
        return agent.run_once()
    agent.run_forever()
    return None
