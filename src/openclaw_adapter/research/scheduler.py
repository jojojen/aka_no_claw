"""Explicit `/research` stage scheduling and lifecycle control (R3.8)."""

from __future__ import annotations

import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from typing import Callable

from .models import ResearchBudget, ResearchJobContext, build_budgeted_search_fn

logger = logging.getLogger(__name__)

def run(
    service: object,
    raw_input: str,
    chat_id: str,
    *,
    marketplace_total_budget_seconds: float,
    marketplace_heartbeat_interval_seconds: float,
    build_report: Callable[[ResearchJobContext], object],
) -> str:
    chat_key = str(chat_id)
    if not raw_input or not raw_input.strip():
        return "用法：/research <Mercari 商品網址或商品名稱>"
    if not service._try_acquire_chat(chat_key):
        return "同一個聊天室目前已有 /research 在執行中，請等上一個研究完成。"

    notifier = service._notifier_factory(chat_key)
    cancel_check = (
        service._cancel_probe_factory(chat_key)
        if service._cancel_probe_factory is not None else None
    )
    budget = ResearchBudget(max_searches=service._max_searches)
    budgeted_search_fn = build_budgeted_search_fn(
        service._search_fn, budget, cancel_check=cancel_check
    )
    ctx = ResearchJobContext(
        raw_input=raw_input,
        chat_id=chat_key,
        notifier=notifier,
        budget=budget,
        search_fn=budgeted_search_fn,
        heartbeat_interval_seconds=service._heartbeat_interval_seconds,
        cancel_check=cancel_check,
    )
    try:
        notifier.send("⏳ /research 已開始，先抓商品頁與市場資料…")
        stage_by_no = {
            stage_no: (label, runner)
            for (stage_no, label), runner in zip(
                service._STAGES, service._stage_runners, strict=True
            )
        }

        def _run_tracked(stage_no: int) -> str:
            ctx.check_cancelled()
            label, runner = stage_by_no[stage_no]
            ctx.current_stage = stage_no
            ctx.current_label = label
            ctx.stage_started_monotonic = time.monotonic()
            ctx.last_heartbeat_monotonic = 0.0
            note = runner(ctx)
            milestone = service._MILESTONE_STAGES.get(stage_no)
            if milestone:
                notifier.send(f"✅ {milestone}：{note}")
            return note

        # Stages 0-2 are a true chain: parse → fetch item → entity profile,
        # and stages 3/4/5/7 all read their outputs. Run them in order first.
        for stage_no in (0, 1, 2):
            _run_tracked(stage_no)

        # Stages 3 (商品狀況), 4 (增值潛力), 5 (合理市價) and 7 (賣家風險) are mutually
        # independent — each reads only stage 0-2 outputs and writes disjoint
        # ctx fields. Run them concurrently so a cloud-offloaded appreciation
        # enricher overlaps the local price gate instead of queueing behind it.
        notifier.send("🔍 [3-7/7] 市場搜尋中…（可能需要 1-3 分鐘）")
        marketplace_start = time.monotonic()

        # Background heartbeat: stage threads can block on I/O for 90s+ each
        # so we can't rely on in-stage heartbeat() calls.  A daemon thread
        # fires progress notes independently of the scrapers.
        _heartbeat_stop = threading.Event()

        def _marketplace_heartbeat() -> None:
            _heartbeat_stop.wait(timeout=marketplace_heartbeat_interval_seconds)
            while not _heartbeat_stop.is_set():
                elapsed = time.monotonic() - marketplace_start
                notifier.send(
                    f"⏳ 市場搜尋仍在執行（已過 {elapsed:.0f}s）；請稍候…"
                )
                _heartbeat_stop.wait(timeout=marketplace_heartbeat_interval_seconds)

        threading.Thread(
            target=_marketplace_heartbeat, daemon=True, name="research-hb"
        ).start()

        parallel_notes: dict[int, str] = {}
        try:
            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="research-stage"
            ) as pool:

                def _run_parallel_stage(stage_no: int) -> str:
                    # Checked here (thread start) and again inside the stage
                    # via heartbeat()/budgeted search, so a cancel that lands
                    # mid-marketplace-search stops queued AND running stages.
                    ctx.check_cancelled()
                    return stage_by_no[stage_no][1](ctx)

                futures = {
                    pool.submit(_run_parallel_stage, stage_no): stage_no
                    for stage_no in (3, 4, 5, 7)
                }
                try:
                    for future in as_completed(
                        futures, timeout=marketplace_total_budget_seconds
                    ):
                        parallel_notes[futures[future]] = future.result()
                except FuturesTimeoutError:
                    ctx.marketplace_timed_out = True
                    elapsed = time.monotonic() - marketplace_start
                    logger.warning(
                        "research marketplace budget exhausted elapsed=%.1fs budget=%.1fs",
                        elapsed,
                        marketplace_total_budget_seconds,
                    )
                    notifier.send(
                        "⚠️ 市場搜尋逾時，已用目前取得的資料回答；"
                        "價格／成交資料可能不完整。"
                    )
        finally:
            _heartbeat_stop.set()

        elapsed_marketplace = time.monotonic() - marketplace_start
        logger.info(
            "research marketplace stages elapsed=%.1fs timed_out=%s",
            elapsed_marketplace,
            ctx.marketplace_timed_out,
        )
        milestone = service._MILESTONE_STAGES.get(5)
        if milestone and not ctx.marketplace_timed_out:
            notifier.send(f"✅ {milestone}：{parallel_notes.get(5, '')}")

        # Stage 6 (流動性) consumes stage 5's price evidence, so it must run
        # after the parallel batch completes.
        _run_tracked(6)

        report = build_report(ctx)
        return service._final_formatter(report)
    finally:
        service._release_chat(chat_key)
