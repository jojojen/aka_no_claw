"""Poller-process background jobs: schedulers, monitors, and inbox bootstrap.

Moved out of telegram_bot.py in R2.4 (#75). These are the aka-owned daemons
started from `run_telegram_polling` (RAG digest, home-schedule, backup, title
corpus, card-image crawler, Mercari watch monitor) plus the inbox/DB bootstrap
helpers that prepare their state. telegram_bot re-imports every name so
`run_telegram_polling` and price_monitor_service (which imports
`_start_watch_monitor` / `_start_card_image_crawler` from telegram_bot) are
unchanged.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from assistant_runtime import AssistantSettings, build_ssl_context
from market_monitor.storage import MonitorDatabase
from price_monitor_bot.watch_monitor import ensure_monitor as _ensure_watch_monitor
from telegram_core.transport import TelegramBotClient

from .backup_command import BackupScheduler
from .home_schedule import HomeScheduleScheduler, get_home_schedule_store, make_run_slash_command
from .rag_daily_digest import RagDailyDigestScheduler
from .reputation_render import (
    format_reputation_snapshot_delivery_text,
    render_reputation_snapshot_artifacts,
)
from .reputation_snapshot import fetch_reputation_proof_document, request_reputation_snapshot
from .telegram_env import require_telegram_token

logger = logging.getLogger(__name__)


def _start_rag_daily_digest(settings) -> RagDailyDigestScheduler | None:
    """Start the daily RAG digest daemon (fires at 22:00 local time)."""
    chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
    if not chat_ids:
        logger.warning("_start_rag_daily_digest: no chat_ids configured — skipping")
        return None
    try:
        token = require_telegram_token(settings)
        ssl_ctx = build_ssl_context(settings)
        client = TelegramBotClient(token, ssl_context=ssl_ctx)

        def _send(chat_id: str, text: str, reply_markup: dict | None) -> None:
            client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

        scheduler = RagDailyDigestScheduler(
            db_path=settings.knowledge_db_path,
            chat_ids=chat_ids,
            send_fn=_send,
            signal_db_path=(
                settings.collectible_signal_db_path
                if settings.collectible_signal_store_enabled
                else None
            ),
        )
        scheduler.start()
        return scheduler
    except Exception:
        logger.exception("_start_rag_daily_digest: failed to start")
        return None


def _start_home_schedule_scheduler(settings, command_handlers) -> HomeScheduleScheduler | None:
    """Start the /schedulehome daemon (issue #39): fires due home schedules at
    minute resolution, re-dispatching their stored slash commands through the
    same command registry the bot uses. Results are reported back to Telegram."""
    chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
    try:
        store = get_home_schedule_store(settings.openclaw_home_schedules_path)
        run_command = make_run_slash_command(command_handlers)
        notify = None
        if chat_ids:
            token = require_telegram_token(settings)
            client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))

            def notify(text: str) -> None:  # noqa: F811 - intentional conditional def
                for cid in chat_ids:
                    client.send_message(chat_id=cid, text=text, reply_markup=None)

        # Single-user/local: scheduled commands deliver to the first configured
        # chat (e.g. /generateaudio sends its generated audio file there).
        scheduler_chat_id = chat_ids[0] if chat_ids else ""
        scheduler = HomeScheduleScheduler(
            store=store,
            run_command=run_command,
            chat_id=scheduler_chat_id,
            notify=notify,
        )
        scheduler.start()
        return scheduler
    except Exception:
        logger.exception("_start_home_schedule_scheduler: failed to start")
        return None


def _bootstrap_inboxes(settings):
    """Create and bootstrap the sns_inbox and knowledge_inbox for the telegram process.

    Telegram is the *producer*; sns_monitor service is the consumer.
    Returns (SnsInbox, KnowledgeInbox).
    """
    from sns_monitor.inbox import SnsInbox
    from .knowledge_inbox import KnowledgeInbox
    sns_inbox = SnsInbox(settings.sns_inbox_db_path)
    sns_inbox.bootstrap()
    knowledge_inbox = KnowledgeInbox(settings.knowledge_inbox_db_path)
    knowledge_inbox.bootstrap()
    logger.info(
        "telegram: inboxes bootstrapped sns=%s knowledge=%s",
        settings.sns_inbox_db_path, settings.knowledge_inbox_db_path,
    )
    return sns_inbox, knowledge_inbox


def _bootstrap_watch_inbox(settings):
    """Create and bootstrap the watch_inbox for the telegram process.

    Telegram is the *producer*; price_monitor service is the consumer.
    Returns WatchInbox.
    """
    from .watch_inbox import WatchInbox
    inbox = WatchInbox(settings.watch_inbox_db_path)
    inbox.bootstrap()
    logger.info("telegram: watch inbox bootstrapped path=%s", settings.watch_inbox_db_path)
    return inbox


def _bootstrap_opportunity_inbox(settings):
    """Create and bootstrap the opportunity_inbox for the telegram process.

    Telegram is the *producer*; opportunity_agent service is the consumer.
    Returns OpportunityInbox.
    """
    from .opportunity_inbox import OpportunityInbox
    inbox = OpportunityInbox(settings.opportunity_inbox_db_path)
    inbox.bootstrap()
    logger.info("telegram: opportunity inbox bootstrapped path=%s", settings.opportunity_inbox_db_path)
    return inbox


def _start_backup_scheduler(settings) -> None:
    """Start the daily auto-backup daemon (fires at 23:00 local time)."""
    data_dir = Path(settings.monitor_db_path).resolve().parent
    project_root = data_dir.parent
    generated_tools_dir = project_root / "generated_tools"
    dest = Path(settings.openclaw_backup_dir)
    hour = getattr(settings, "openclaw_backup_hour", 23)
    scheduler = BackupScheduler(
        data_dir=data_dir,
        generated_tools_dir=generated_tools_dir if generated_tools_dir.is_dir() else None,
        dest=dest,
        hour=hour,
        notify=_build_backup_notify(settings),
    )
    scheduler.start()


def _build_backup_notify(settings):
    """Telegram send callback for scheduled-backup reports; None → log-only."""
    chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
    if not chat_ids:
        logger.warning("_build_backup_notify: no chat_ids configured — backup runs silent")
        return None
    try:
        token = require_telegram_token(settings)
        client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))
    except Exception:
        logger.exception("_build_backup_notify: notify client unavailable — backup runs silent")
        return None

    def _notify(text: str) -> None:
        for chat_id in chat_ids:
            client.send_message(chat_id=chat_id, text=text)

    return _notify


def _start_title_corpus_rebuilder(settings) -> None:
    """Weekly: rebuild the comp-filter IDF table from the passive title corpus
    without noisy Telegram notices from the always-on bot runtime. Reads only
    locally cached titles — zero new external queries (Rule C7)."""
    try:
        from .title_corpus_rebuilder import TitleCorpusRebuilder
    except Exception:
        logger.exception("_start_title_corpus_rebuilder: import failed — skipping")
        return
    TitleCorpusRebuilder(notify_fn=lambda _text: None, notify_enabled=False).start()


def _start_card_image_crawler(watch_db: MonitorDatabase):
    """Kick off the trend-driven perceptual-hash crawler in the background.
    Pulls Snkrdunk's hot products every 6 hours and pre-populates
    `card_image_fingerprints` so user photo uploads of popular boxes/cards
    can short-circuit the slow OCR + vision LLM pipeline.

    Best-effort: if the price_monitor_bot package isn't importable or any
    other init issue arises, the bot keeps running without proactive
    fingerprinting (the lookup-time persist path still learns over time)."""
    try:
        from tcg_tracker.image_crawler import CardImageCrawler, CardImageCrawlMonitor
    except Exception as exc:
        logger.warning("card image crawler unavailable: %s", exc)
        return None
    try:
        crawler = CardImageCrawler(
            database=watch_db,
            games=("pokemon", "ws", "union_arena"),
            per_game_limit=30,
        )
        monitor = CardImageCrawlMonitor(
            crawler=crawler,
            interval_seconds=6 * 3600,   # every 6 hours
            initial_delay_seconds=120,    # let the rest of the bot finish booting
        )
        monitor.start()
        return monitor
    except Exception as exc:
        logger.warning("card image crawler failed to start: %s", exc)
        return None


def _bootstrap_watch_db(settings: AssistantSettings) -> MonitorDatabase:
    db = MonitorDatabase(settings.monitor_db_path)
    db.bootstrap()
    return db


def _start_watch_monitor(
    *,
    settings: AssistantSettings,
    watch_db: MonitorDatabase,
    token: str,
) -> None:
    chat_id = settings.openclaw_telegram_chat_id
    if not chat_id:
        logger.warning("Mercari watch monitor: no OPENCLAW_TELEGRAM_CHAT_ID set, notifications will be skipped")

    ssl_ctx = build_ssl_context(settings)

    def notify(notification_chat_id: str, text: str) -> None:
        resolved_chat = notification_chat_id if notification_chat_id and notification_chat_id != "dashboard" else chat_id
        if not resolved_chat:
            logger.warning("Mercari watch notify: no chat_id, dropping message")
            return
        if not text or not text.strip():
            return
        from .outbound_guards import guard_outbound
        reason = guard_outbound(text, proactive=True)
        if reason:
            logger.warning("outbound guard blocked push: %s", reason)
            return
        client = TelegramBotClient(token, ssl_context=ssl_ctx)
        client.send_message(chat_id=resolved_chat, text=text)

    def do_snapshot(notification_chat_id: str, urls: list[str]) -> None:
        resolved_chat = notification_chat_id if notification_chat_id and notification_chat_id != "dashboard" else chat_id
        if not resolved_chat:
            logger.warning("Auto-snapshot: no chat_id, skipping")
            return
        bot_client = TelegramBotClient(token, ssl_context=ssl_ctx)
        try:
            bot_client.send_message(
                chat_id=resolved_chat,
                text=f"正在為 {len(urls)} 筆新商品建立賣家信譽快照，請稍候…",
            )
        except Exception:
            logger.warning("Auto-snapshot: failed to send ack message")

        def _run() -> None:
            for url in urls:
                try:
                    result = request_reputation_snapshot(settings=settings, query_url=url)
                    proof_document = None
                    if result.proof_id:
                        try:
                            proof_document = fetch_reputation_proof_document(
                                settings=settings, proof_id=result.proof_id
                            )
                        except Exception:
                            logger.exception("Auto-snapshot: proof fetch failed proof_id=%s", result.proof_id)
                    pdf_path, preview_path = render_reputation_snapshot_artifacts(
                        settings=settings, result=result
                    )
                    summary = format_reputation_snapshot_delivery_text(result, proof_document)
                    from .outbound_guards import guard_outbound
                    _snap_reason = guard_outbound(summary, proactive=True)
                    if _snap_reason:
                        logger.warning("outbound guard blocked push: %s", _snap_reason)
                    else:
                        c = TelegramBotClient(token, ssl_context=ssl_ctx)
                        c.send_message(chat_id=resolved_chat, text=summary)
                        c.send_document(
                            chat_id=resolved_chat,
                            document_path=pdf_path,
                            caption="信譽快照 PDF",
                        )
                        c.send_photo(
                            chat_id=resolved_chat,
                            photo_path=preview_path,
                            caption="信譽快照預覽",
                        )
                    for p in (pdf_path, preview_path):
                        try:
                            p.unlink()
                        except Exception:
                            pass
                    logger.info("Auto-snapshot: completed url=%s proof_id=%s", url, result.proof_id)
                except Exception:
                    logger.exception("Auto-snapshot: failed url=%s", url)

        threading.Thread(target=_run, name="auto-snapshot", daemon=True).start()

    monitor, started = _ensure_watch_monitor(
        db_path=watch_db.path,
        notify_fn=notify,
        snapshot_fn=do_snapshot,
        interval_seconds=60,
    )
    logger.info("Mercari watch monitor started=%s running=%s", started, monitor.is_running())
    if started:
        print("[watch-monitor] Mercari watch monitor started (interval=60s)")
