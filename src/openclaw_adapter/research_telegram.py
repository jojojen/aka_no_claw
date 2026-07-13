"""Telegram glue for /research: reply cache/views, notifier, snapshot lookups.

Moved out of telegram_bot.py in R2.2 (#75). telegram_bot re-imports these
names, so legacy ``openclaw_adapter.telegram_bot`` import paths keep working.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context
from price_monitor_bot.bot import ResearchRenderer, TelegramResearchQuery
from telegram_core.transport import TelegramBotClient

from .dynamic_tools import CloudBackendUnavailable, build_research_cloud_text_client
from .llm_pool_settings import _LLM_NOT_CONFIGURED_MESSAGE, _select_text_generation_model
from .reputation_snapshot import (
    ReputationSnapshotResult,
    SnapshotStillPending,
    fetch_reputation_proof_document,
    request_reputation_snapshot,
)
from .research_command import (
    ResearchNotifier,
    ResearchReport,
    SellerReputationSnapshot,
    build_appreciation_enricher,
    format_research_compact_report,
    format_research_detail_report,
    _build_seller_snapshot_section_result,
)
from .telegram_env import require_telegram_token
from .web_search import (
    DEFAULT_WEB_SEARCH_LIMIT,
    _build_summary_prompt,
    build_web_research_answer,
    fetch_page_text,
    filter_relevant_sources_with_ollama,
    format_web_research_answer,
    reformulate_queries_with_ollama,
    summarize_web_sources_with_ollama,
    web_search,
)

logger = logging.getLogger(__name__)


def _run_research_worker_call(func: Callable[[], object]) -> object:
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


class _ResearchReplyCache:
    def __init__(self, *, max_entries: int = 128, ttl_seconds: int = 3600) -> None:
        self._max_entries = max(8, max_entries)
        self._ttl_seconds = max(60, ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float, ResearchReport]] = {}

    def put(self, report: ResearchReport) -> str:
        token = uuid.uuid4().hex[:8]
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._entries[token] = (report.chat_id, now, report)
            while len(self._entries) > self._max_entries:
                oldest_token = next(iter(self._entries))
                self._entries.pop(oldest_token, None)
        return token

    def get(self, *, token: str, chat_id: str) -> ResearchReport | None:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(token)
            if entry is None:
                return None
            stored_chat_id, _created_at, report = entry
            if stored_chat_id != chat_id:
                return None
            return report

    def _prune_locked(self, now: float) -> None:
        expired = [
            token
            for token, (_chat_id, created_at, _report) in self._entries.items()
            if now - created_at > self._ttl_seconds
        ]
        for token in expired:
            self._entries.pop(token, None)


def _build_research_reply_markup(token: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "摘要", "callback_data": f"rs:{token}:summary"},
                {"text": "看市價", "callback_data": f"rs:{token}:price"},
            ],
            [
                {"text": "看賣家", "callback_data": f"rs:{token}:seller"},
                {"text": "看來源", "callback_data": f"rs:{token}:sources"},
            ],
            [
                {"text": "看警告", "callback_data": f"rs:{token}:warnings"},
            ],
        ]
    }


def _build_research_reply_formatter(
    cache: _ResearchReplyCache,
) -> "Callable[[ResearchReport], tuple[str, dict[str, object]]]":
    def render(report: ResearchReport) -> tuple[str, dict[str, object]]:
        token = cache.put(report)
        return format_research_compact_report(report), _build_research_reply_markup(token)

    return render


def _build_research_callback_handler(
    cache: _ResearchReplyCache,
) -> "Callable[[str, str, str], tuple[object, str | None, object]]":
    def handler(payload: str, original_text: str, chat_id: str) -> tuple[object, str | None, object]:
        token, _, view = (payload or "").partition(":")
        report = cache.get(token=token, chat_id=str(chat_id))
        if report is None:
            return "研究結果已過期，請重新執行 /research。", None, None
        detail_text = format_research_detail_report(report, view=view or "summary")
        return "已切換研究視圖", detail_text, _build_research_reply_markup(token)

    return handler


def default_web_research_renderer(settings: AssistantSettings) -> ResearchRenderer:
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    # The grounded summarise step reads several fetched pages, so it is the
    # slowest LLM call in the pipeline. Give it extra headroom to survive
    # Ollama queue contention from background workers (entity researcher etc.).
    summarize_timeout = max(timeout, 120)
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None

    def render(query: TelegramResearchQuery) -> str:
        if backend != "ollama" or not endpoint or not model:
            return _LLM_NOT_CONFIGURED_MESSAGE
        answer = build_web_research_answer(
            query.query,
            max_results=DEFAULT_WEB_SEARCH_LIMIT,
            search_fn=lambda q, limit: web_search(q, max_results=limit),
            # Item 4: turn the question into a few focused search queries first.
            reformulate_fn=lambda q: reformulate_queries_with_ollama(
                q,
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout,
                ssl_context=ssl_ctx,
            ),
            # Drop off-topic SEO hits before they reach the summary / source list.
            relevance_fn=lambda q, sources: filter_relevant_sources_with_ollama(
                q,
                sources,
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout,
                ssl_context=ssl_ctx,
            ),
            # Item 1: download the top results so the summary reads article text.
            fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
            summarize_fn=lambda q, sources: summarize_web_sources_with_ollama(
                q,
                sources,
                endpoint=endpoint,
                model=model,
                timeout_seconds=summarize_timeout,
                ssl_context=ssl_ctx,
            ),
        )
        return format_web_research_answer(answer)

    return render


def _build_research_notifier_factory(settings: AssistantSettings) -> "Callable[[str], ResearchNotifier]":
    token = require_telegram_token(settings)
    client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))

    class _TelegramResearchNotifier:
        def __init__(self, chat_id: str) -> None:
            self._chat_id = chat_id

        def send(self, text: str) -> None:
            client.send_message(chat_id=self._chat_id, text=text)

    return lambda chat_id: _TelegramResearchNotifier(str(chat_id))


def _build_research_seller_snapshot_lookup(
    settings: AssistantSettings,
) -> "Callable[[str], SellerReputationSnapshot]":
    def lookup(seller_url: str) -> SellerReputationSnapshot:
        result = request_reputation_snapshot(settings=settings, query_url=seller_url)
        try:
            proof_document = (
                fetch_reputation_proof_document(settings=settings, proof_id=result.proof_id)
                if result.proof_id
                else {}
            )
        except Exception as exc:
            # Job completed but the proof document fetch failed transiently (e.g.
            # socket timeout). Do not report a permanent failure: convert to a
            # pending state so /research schedules the background follow-up, which
            # re-fetches the proof once it is reachable.
            if not result.proof_id:
                raise
            logger.warning(
                "seller snapshot proof fetch failed after job done proof_id=%s, "
                "scheduling follow-up: %s",
                result.proof_id,
                exc,
            )

            def _poll_proof() -> "ReputationSnapshotResult | None":
                deadline = time.monotonic() + 900.0
                while time.monotonic() < deadline:
                    try:
                        fetch_reputation_proof_document(
                            settings=settings, proof_id=result.proof_id
                        )
                        return result
                    except Exception:
                        time.sleep(2.0)
                return None

            raise SnapshotStillPending(result.job_id or result.proof_id, poll_fn=_poll_proof)
        subject = proof_document.get("subject", {}) if isinstance(proof_document, dict) else {}
        metrics = proof_document.get("metrics", {}) if isinstance(proof_document, dict) else {}
        quality = proof_document.get("quality", {}) if isinstance(proof_document, dict) else {}
        review_entries = proof_document.get("review_entries", ()) if isinstance(proof_document, dict) else ()
        as_seller = quality.get("as_seller") if isinstance(quality, dict) else None
        as_buyer = quality.get("as_buyer") if isinstance(quality, dict) else None
        overall = quality.get("overall") if isinstance(quality, dict) else None
        return SellerReputationSnapshot(
            seller_url=seller_url,
            proof_url=result.proof_url,
            proof_id=result.proof_id,
            reused=result.reused,
            display_name=subject.get("display_name") if isinstance(subject, dict) else None,
            captured_at=proof_document.get("captured_at") if isinstance(proof_document, dict) else None,
            total_reviews=metrics.get("total_reviews") if isinstance(metrics, dict) else None,
            listing_count=metrics.get("listing_count") if isinstance(metrics, dict) else None,
            followers_count=metrics.get("followers_count") if isinstance(metrics, dict) else None,
            following_count=metrics.get("following_count") if isinstance(metrics, dict) else None,
            seller_positive=as_seller.get("positive") if isinstance(as_seller, dict) else None,
            seller_negative=as_seller.get("negative") if isinstance(as_seller, dict) else None,
            seller_rate=as_seller.get("rate") if isinstance(as_seller, dict) else None,
            buyer_positive=as_buyer.get("positive") if isinstance(as_buyer, dict) else None,
            buyer_negative=as_buyer.get("negative") if isinstance(as_buyer, dict) else None,
            buyer_rate=as_buyer.get("rate") if isinstance(as_buyer, dict) else None,
            overall_rate=overall.get("rate") if isinstance(overall, dict) else None,
            seller_negative_excerpts=_extract_negative_seller_review_excerpts(review_entries),
        )

    return lookup


def _build_research_seller_snapshot_followup(
    settings: "AssistantSettings",
) -> "Callable[[str, Callable, ResearchNotifier], None]":
    """Return a followup fn that background-polls a pending snapshot and pushes the result."""
    import threading as _threading

    def _proof_doc_to_seller_snapshot(
        seller_url: str,
        result: ReputationSnapshotResult,
        proof_doc: dict,
    ) -> SellerReputationSnapshot:
        subject = proof_doc.get("subject", {}) if isinstance(proof_doc, dict) else {}
        metrics = proof_doc.get("metrics", {}) if isinstance(proof_doc, dict) else {}
        quality = proof_doc.get("quality", {}) if isinstance(proof_doc, dict) else {}
        review_entries = proof_doc.get("review_entries", ()) if isinstance(proof_doc, dict) else ()
        as_seller = quality.get("as_seller") if isinstance(quality, dict) else None
        as_buyer = quality.get("as_buyer") if isinstance(quality, dict) else None
        overall = quality.get("overall") if isinstance(quality, dict) else None
        return SellerReputationSnapshot(
            seller_url=seller_url,
            proof_url=result.proof_url,
            proof_id=result.proof_id,
            reused=result.reused,
            display_name=subject.get("display_name") if isinstance(subject, dict) else None,
            captured_at=proof_doc.get("captured_at") if isinstance(proof_doc, dict) else None,
            total_reviews=metrics.get("total_reviews") if isinstance(metrics, dict) else None,
            listing_count=metrics.get("listing_count") if isinstance(metrics, dict) else None,
            followers_count=metrics.get("followers_count") if isinstance(metrics, dict) else None,
            following_count=metrics.get("following_count") if isinstance(metrics, dict) else None,
            seller_positive=as_seller.get("positive") if isinstance(as_seller, dict) else None,
            seller_negative=as_seller.get("negative") if isinstance(as_seller, dict) else None,
            seller_rate=as_seller.get("rate") if isinstance(as_seller, dict) else None,
            buyer_positive=as_buyer.get("positive") if isinstance(as_buyer, dict) else None,
            buyer_negative=as_buyer.get("negative") if isinstance(as_buyer, dict) else None,
            buyer_rate=as_buyer.get("rate") if isinstance(as_buyer, dict) else None,
            overall_rate=overall.get("rate") if isinstance(overall, dict) else None,
            seller_negative_excerpts=_extract_negative_seller_review_excerpts(review_entries),
        )

    def followup(seller_url: str, poll_fn: "Callable", notifier: ResearchNotifier) -> None:
        def _bg() -> None:
            try:
                result = poll_fn()
                if result is None:
                    notifier.send(
                        f"⏰ 賣家快照逾時或失敗，請手動查詢：/snapshot {seller_url}"
                    )
                    return
                proof_doc = (
                    fetch_reputation_proof_document(settings=settings, proof_id=result.proof_id)
                    if result.proof_id
                    else {}
                )
                snapshot = _proof_doc_to_seller_snapshot(seller_url, result, proof_doc)
                section = _build_seller_snapshot_section_result(snapshot)
                notifier.send(f"📋 賣家風險分析（補送）\n{section.summary}")
            except Exception as exc:
                logger.error("seller snapshot followup failed seller_url=%s: %s", seller_url, exc)
                notifier.send(
                    f"⚠️ 賣家快照補送失敗：{exc}\n請手動查詢：/snapshot {seller_url}"
                )

        _threading.Thread(target=_bg, daemon=True, name="reputation-followup").start()

    return followup


def _extract_negative_seller_review_excerpts(review_entries: object) -> tuple[str, ...]:
    if not isinstance(review_entries, list):
        return ()
    excerpts: list[str] = []
    seen: set[str] = set()
    for entry in review_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "seller" or entry.get("rating") != "negative":
            continue
        text = " ".join(str(entry.get("body_excerpt") or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        excerpts.append(text)
        if len(excerpts) >= 3:
            break
    return tuple(excerpts)


def _build_research_ip_heat_lookup(
    settings: AssistantSettings,
) -> "Callable[[tuple[str, ...]], dict[str, tuple[object, ...]]]":
    from pathlib import Path as _Path

    from .ip_heat_store import IpHeatStore

    store = IpHeatStore(_Path(settings.knowledge_db_path).with_name("ip_heat.sqlite3"))

    def lookup(canonicals: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for canonical in canonicals:
            signals = tuple(store.latest_for_ip(canonical))
            if signals:
                result[canonical] = signals
        return result

    return lookup


def _build_research_appreciation_enricher(settings: AssistantSettings):
    """A4: build the appreciation web-enricher reusing the same page fetch + LLM
    summariser the /research web-research renderer uses. Returns None when neither
    the local text LLM nor the cloud enricher is configured (falls back to
    snippet-only evidence).

    Phase-1 cloud offload: when ``OPENCLAW_RESEARCH_CLOUD_ENRICHER=opencode`` and
    the OpenCode CLI probes ok, the summariser (the open-ended, abstract step)
    runs on cloud big-pickle while the price gate stays local. A cloud outage
    (``CloudBackendUnavailable``) or empty reply degrades to a single in-process
    local summarise — it must NOT trigger /new's bot-restart failover. The local
    relevance gate is skipped in cloud mode so stage 3 doesn't queue a second
    call on the same local Ollama as the price gate."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    local_ready = backend == "ollama" and bool(endpoint) and bool(model)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    summarize_timeout = max(timeout, 120)
    ssl_ctx = build_ssl_context(settings) if (endpoint or "").startswith("https://") else None

    def _local_summarize(q, sources):
        return summarize_web_sources_with_ollama(
            q,
            sources,
            endpoint=endpoint,
            model=model,
            timeout_seconds=summarize_timeout,
            ssl_context=ssl_ctx,
        )

    cloud_client = build_research_cloud_text_client(settings)
    if cloud_client is not None:
        def _cloud_summarize(q, sources):
            if not sources:
                return f"我找不到足夠有用的網路來源來回答：{q}"
            prompt = _build_summary_prompt(q, sources)
            try:
                text = (cloud_client.generate(prompt, temperature=0.2) or "").strip()
            except CloudBackendUnavailable:
                logger.warning(
                    "research appreciation cloud enricher unavailable; "
                    "falling back to local for this request",
                    exc_info=True,
                )
                return _local_summarize(q, sources) if local_ready else ""
            if text:
                return text
            return _local_summarize(q, sources) if local_ready else ""

        logger.info("research appreciation enricher: cloud big-pickle (local fallback)")
        return build_appreciation_enricher(
            fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
            summarize_fn=_cloud_summarize,
            relevance_fn=None,
        )

    if not local_ready:
        return None
    return build_appreciation_enricher(
        fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
        summarize_fn=_local_summarize,
        relevance_fn=lambda q, sources: filter_relevant_sources_with_ollama(
            q,
            sources,
            endpoint=endpoint,
            model=model,
            timeout_seconds=timeout,
            ssl_context=ssl_ctx,
        ),
    )


def _build_yuyutei_code_resolver(
    settings: AssistantSettings, search_fn: "Callable[[str, int], object]"
) -> "object | None":
    """Build the LLM/RAG resolver that maps a bare card name → yuyutei game code
    so the 遊々亭 買取/販売 band can appear for queries with no game keyword.
    Returns the resolver (exposing ``.resolve`` and ``.enrich_cache``) or ``None``
    (band falls back to keyword-only routing) when the local text LLM isn't
    configured."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    if backend != "ollama" or not endpoint or not model:
        return None

    from .opportunity_agent import _call_ollama_json
    from .yuyutei_code_resolver import YuyuteiGameCodeResolver

    return YuyuteiGameCodeResolver(
        knowledge_db_path=settings.knowledge_db_path,
        json_call_fn=_call_ollama_json,
        endpoint=endpoint,
        model=model,
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        ssl_context=build_ssl_context(settings) if endpoint.startswith("https://") else None,
        search_fn=search_fn,
    )
