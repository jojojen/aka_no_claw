"""Standalone entry point for local.openclaw.price_monitor launchd service.

Runs marketplace watch_monitor + card image crawler background threads and
processes the watch_inbox write queue.

This process is the sole writer for data/monitor.sqlite3.
Telegram process reads monitor.sqlite3 read-only; write requests (watch add/
delete/update) arrive via data/watch_inbox.sqlite3.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_INBOX_POLL_INTERVAL = 3  # seconds


def _apply_watch_inbox_request(watch_db, req: dict) -> None:
    from market_monitor.storage import MarketplaceWatch, build_marketplace_watch_id

    action = req["action"]
    payload = req["payload"]

    if action == "add_watch":
        watch_id = payload.get("watch_id") or build_marketplace_watch_id(
            chat_id=payload.get("chat_id", ""),
            query=payload.get("query", ""),
        )
        markets = tuple(payload.get("markets") or [])
        market_options = payload.get("market_options") or {}
        watch = MarketplaceWatch(
            watch_id=watch_id,
            query=payload["query"],
            price_threshold_jpy=int(payload["price_threshold_jpy"]),
            markets=markets,
            enabled=bool(payload.get("enabled", True)),
            chat_id=payload.get("chat_id", ""),
            last_checked_at=None,
            created_at="",
            updated_at="",
            market_options=market_options,
        )
        watch_db.add_marketplace_watch(watch)
        logger.info("watch_inbox: add_watch watch_id=%s query=%s", watch_id, payload.get("query"))

    elif action == "delete_watch":
        watch_id = payload.get("watch_id", "")
        deleted = watch_db.delete_marketplace_watch(watch_id)
        logger.info("watch_inbox: delete_watch watch_id=%s deleted=%s", watch_id, deleted)

    elif action == "update_watch":
        watch_id = payload.get("watch_id", "")
        fields = {k: v for k, v in payload.items() if k != "watch_id"}
        if "markets" in fields:
            fields["markets"] = tuple(fields["markets"])
        if fields:
            watch_db.update_marketplace_watch(watch_id, **fields)
            logger.info("watch_inbox: update_watch watch_id=%s fields=%s", watch_id, list(fields))

    else:
        raise ValueError(f"Unknown watch_inbox action: {action!r}")


def _run_watch_inbox_poller(watch_db, watch_inbox, stop_event: threading.Event) -> None:
    logger.info("watch_inbox poller started path=%s", watch_inbox.path)
    while not stop_event.is_set():
        try:
            for req in watch_inbox.pop_pending(limit=20):
                try:
                    _apply_watch_inbox_request(watch_db, req)
                    watch_inbox.mark_done(req["id"])
                except Exception as exc:
                    logger.exception(
                        "watch_inbox apply failed req_id=%s action=%s", req["id"], req.get("action")
                    )
                    watch_inbox.mark_error(req["id"], str(exc))
        except Exception:
            logger.exception("watch_inbox poller: unexpected error in poll loop")
        stop_event.wait(timeout=_INBOX_POLL_INTERVAL)


def run_price_monitor_service(settings) -> int:
    """Start the price monitor service. Blocks until SIGTERM / KeyboardInterrupt."""
    from market_monitor.storage import MonitorDatabase
    from .watch_inbox import WatchInbox
    from .telegram_bot import _start_watch_monitor, _start_card_image_crawler, require_telegram_token

    watch_db = MonitorDatabase(settings.monitor_db_path)
    watch_db.bootstrap()

    watch_inbox = WatchInbox(settings.watch_inbox_db_path)
    watch_inbox.bootstrap()

    token = require_telegram_token(settings)

    _start_watch_monitor(settings=settings, watch_db=watch_db, token=token)
    _start_card_image_crawler(watch_db)

    stop_event = threading.Event()
    inbox_thread = threading.Thread(
        target=_run_watch_inbox_poller,
        args=(watch_db, watch_inbox, stop_event),
        name="watch-inbox-poller",
        daemon=True,
    )
    inbox_thread.start()

    print(
        "[price-monitor-service] ✅ Price monitor service ready "
        "(watch_monitor + card_image_crawler + watch_inbox poller running)"
    )
    logger.info("price_monitor_service: blocking until signal")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("price_monitor_service: KeyboardInterrupt — shutting down")
        stop_event.set()

    return 0
