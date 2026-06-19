"""Standalone entry point for local.openclaw.sns_monitor launchd service.

Starts the SNS background monitor threads (RSS polling, keyword scheduler,
push notifications) and processes the sns_inbox + knowledge_inbox write queues.

This process is the sole writer for:
  - data/sns.sqlite3        (SNS watch rules, tweet cache, feedback)
  - data/knowledge.sqlite3  (RAG knowledge entries)

Telegram process is read-only on both files; write requests arrive via
data/sns_inbox.sqlite3 and data/knowledge_inbox.sqlite3.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_INBOX_POLL_INTERVAL = 3  # seconds


# ─── SNS inbox processor ──────────────────────────────────────────────────────

def _apply_sns_inbox_request(sns_db, req: dict) -> None:
    action = req["action"]
    payload = req["payload"]

    if action == "save_rule":
        from sns_monitor.models import AccountWatch, KeywordWatch, TrendWatch
        from datetime import datetime

        kind = payload.get("kind", "")
        last_checked_at = None
        raw_lc = payload.get("last_checked_at")
        if raw_lc:
            try:
                last_checked_at = datetime.fromisoformat(raw_lc)
            except Exception:
                pass

        if kind == "account":
            rule = AccountWatch(
                rule_id=payload["rule_id"],
                screen_name=payload["screen_name"],
                user_id=payload.get("user_id"),
                label=payload.get("label", ""),
                include_keywords=tuple(payload.get("include_keywords") or []),
                domains=tuple(payload.get("domains") or []),
                enabled=bool(payload.get("enabled", True)),
                schedule_minutes=int(payload.get("schedule_minutes", 15)),
                chat_id=payload.get("chat_id", ""),
                last_checked_at=last_checked_at,
                source=payload.get("source", "x"),
                is_auto_discovered=bool(payload.get("is_auto_discovered", False)),
            )
        elif kind == "keyword":
            rule = KeywordWatch(
                rule_id=payload["rule_id"],
                query=payload["query"],
                label=payload.get("label", ""),
                domains=tuple(payload.get("domains") or []),
                enabled=bool(payload.get("enabled", True)),
                schedule_minutes=int(payload.get("schedule_minutes", 30)),
                chat_id=payload.get("chat_id", ""),
                source=payload.get("source", "x"),
            )
        elif kind == "trend":
            rule = TrendWatch(
                rule_id=payload["rule_id"],
                category=payload["category"],
                label=payload.get("label", ""),
                domains=tuple(payload.get("domains") or []),
                enabled=bool(payload.get("enabled", True)),
                schedule_minutes=int(payload.get("schedule_minutes", 60)),
                chat_id=payload.get("chat_id", ""),
                source=payload.get("source", "x"),
            )
        else:
            raise ValueError(f"Unknown rule kind: {kind!r}")
        sns_db.save_watch_rule(rule)
        logger.info("sns_inbox: save_rule kind=%s rule_id=%s", kind, payload.get("rule_id"))

    elif action == "delete_rule":
        rule_id = payload.get("rule_id", "")
        if rule_id:
            deleted = sns_db.delete_watch_rule(rule_id)
            logger.info("sns_inbox: delete_rule rule_id=%s deleted=%s", rule_id, deleted)

    elif action == "feedback":
        from sns_monitor.feedback import record_sns_feedback
        record_sns_feedback(
            db=sns_db,
            tweet_id=payload.get("tweet_id", ""),
            rule_id=payload.get("rule_id", ""),
            chat_id=payload.get("chat_id", ""),
            kind=payload.get("kind", ""),
        )
        logger.info("sns_inbox: feedback kind=%s rule_id=%s", payload.get("kind"), payload.get("rule_id"))

    elif action == "auto_discovery_feedback":
        sns_db.record_auto_discovery_feedback(
            screen_name=payload.get("screen_name", ""),
            polarity=payload.get("polarity", "negative"),
            domains=tuple(payload.get("domains") or []),
            chat_id=payload.get("chat_id", ""),
        )
        logger.info("sns_inbox: auto_discovery_feedback polarity=%s", payload.get("polarity"))

    else:
        raise ValueError(f"Unknown sns_inbox action: {action!r}")


# ─── Knowledge inbox processor ────────────────────────────────────────────────

def _apply_knowledge_inbox_request(knowledge_db, req: dict) -> None:
    action = req["action"]
    payload = req["payload"]

    if action == "save_entry":
        knowledge_db.upsert_entry(
            entity_canonical=payload["entity_canonical"],
            entity_type=payload.get("entity_type", "other"),
            summary=payload.get("summary", ""),
            source_urls=tuple(payload.get("source_urls") or []),
            confidence=float(payload.get("confidence", 0.5)),
            origin=payload.get("origin", "manual"),
            aliases=tuple(payload.get("aliases") or []),
        )
        logger.info("knowledge_inbox: save_entry canonical=%s", payload.get("entity_canonical"))

    elif action == "alias_entry":
        knowledge_db.add_alias(payload["alias"], payload["canonical"])
        logger.info("knowledge_inbox: alias_entry %s → %s",
                    payload.get("alias"), payload.get("canonical"))

    elif action == "delete_entry":
        entry_id = payload.get("entry_id")
        if entry_id:
            knowledge_db.delete_entry(entry_id)
            logger.info("knowledge_inbox: delete_entry id=%s", entry_id)
        else:
            canonical = (payload.get("entity_canonical") or "").strip().lower()
            if canonical:
                with knowledge_db.connect() as conn:
                    conn.execute(
                        "DELETE FROM knowledge_entries WHERE entity_canonical = ?",
                        (canonical,),
                    )
                    conn.commit()
                logger.info("knowledge_inbox: delete_entry by canonical=%s", canonical)

    elif action == "mark_codegen_applied":
        ids = payload.get("ids") or []
        if ids:
            knowledge_db.mark_codegen_applied(tuple(ids))
            logger.info("knowledge_inbox: mark_codegen_applied count=%d", len(ids))

    elif action == "codegen_upsert":
        knowledge_db.upsert_codegen_knowledge(
            category=payload.get("category", "other"),
            title=payload.get("title", ""),
            technique=payload.get("technique", ""),
            keywords=tuple(payload.get("keywords") or []),
            origin=payload.get("origin", "seed"),
            confidence=float(payload.get("confidence", 0.5)),
        )
        logger.info("knowledge_inbox: codegen_upsert title=%s", payload.get("title"))

    else:
        raise ValueError(f"Unknown knowledge_inbox action: {action!r}")


# ─── Poll threads ─────────────────────────────────────────────────────────────

def _run_sns_inbox_poller(sns_db, sns_inbox_path, stop_event: threading.Event) -> None:
    from sns_monitor.inbox import SnsInbox
    inbox = SnsInbox(sns_inbox_path)
    inbox.bootstrap()
    logger.info("sns_inbox poller started path=%s", sns_inbox_path)
    while not stop_event.is_set():
        try:
            for req in inbox.pop_pending(limit=20):
                try:
                    _apply_sns_inbox_request(sns_db, req)
                    inbox.mark_done(req["id"])
                except Exception as exc:
                    logger.exception("sns_inbox apply failed req_id=%s action=%s",
                                     req["id"], req.get("action"))
                    inbox.mark_error(req["id"], str(exc))
        except Exception:
            logger.exception("sns_inbox poller: unexpected error in poll loop")
        stop_event.wait(timeout=_INBOX_POLL_INTERVAL)


def _run_knowledge_inbox_poller(knowledge_db, knowledge_inbox_path, stop_event: threading.Event) -> None:
    from openclaw_adapter.knowledge_inbox import KnowledgeInbox
    inbox = KnowledgeInbox(knowledge_inbox_path)
    inbox.bootstrap()
    logger.info("knowledge_inbox poller started path=%s", knowledge_inbox_path)
    while not stop_event.is_set():
        try:
            for req in inbox.pop_pending(limit=20):
                try:
                    _apply_knowledge_inbox_request(knowledge_db, req)
                    inbox.mark_done(req["id"])
                except Exception as exc:
                    logger.exception("knowledge_inbox apply failed req_id=%s action=%s",
                                     req["id"], req.get("action"))
                    inbox.mark_error(req["id"], str(exc))
        except Exception:
            logger.exception("knowledge_inbox poller: unexpected error in poll loop")
        stop_event.wait(timeout=_INBOX_POLL_INTERVAL)


# ─── Service entry point ──────────────────────────────────────────────────────

def run_sns_monitor_service(settings) -> int:
    """Start the SNS monitor service. Blocks until SIGTERM / KeyboardInterrupt."""
    from assistant_runtime import build_ssl_context
    from .telegram_bot import require_telegram_token
    from .sns_tools import _start_sns_monitor
    from .knowledge_db import KnowledgeDatabase

    token = require_telegram_token(settings)
    ssl_ctx = build_ssl_context(settings)

    sns_db, _buzz_fn = _start_sns_monitor(
        settings=settings, token=token, ssl_context=ssl_ctx,
    )
    if sns_db is None:
        logger.error("sns_monitor_service: _start_sns_monitor failed — aborting")
        return 1

    knowledge_db = KnowledgeDatabase(settings.knowledge_db_path)
    knowledge_db.bootstrap()
    logger.info("sns_monitor_service: knowledge DB ready path=%s", settings.knowledge_db_path)

    stop_event = threading.Event()

    sns_inbox_thread = threading.Thread(
        target=_run_sns_inbox_poller,
        args=(sns_db, settings.sns_inbox_db_path, stop_event),
        name="sns-inbox-poller",
        daemon=True,
    )
    knowledge_inbox_thread = threading.Thread(
        target=_run_knowledge_inbox_poller,
        args=(knowledge_db, settings.knowledge_inbox_db_path, stop_event),
        name="knowledge-inbox-poller",
        daemon=True,
    )
    sns_inbox_thread.start()
    knowledge_inbox_thread.start()

    print("[sns-monitor-service] ✅ SNS monitor service ready "
          "(monitor + sns_inbox + knowledge_inbox pollers running)")
    logger.info("sns_monitor_service: blocking until signal")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("sns_monitor_service: KeyboardInterrupt — shutting down")
        stop_event.set()

    return 0
