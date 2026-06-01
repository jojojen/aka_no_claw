"""SNS Monitor tools integration for OpenClaw."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from assistant_runtime import AssistantSettings, build_ssl_context

logger = logging.getLogger(__name__)


def bootstrap_sns_db(settings: AssistantSettings):
    """Create and bootstrap the SNS database."""
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(settings.sns_db_path)
    db.bootstrap()
    logger.info("SNS database initialized path=%s", settings.sns_db_path)
    return db


def _build_sns_buzz_fn(settings: AssistantSettings, x_client, ssl_context=None):
    """Build the /snsbuzz callback: search X by keyword, summarize via LLM, return Telegram-ready text."""
    from sns_monitor.digest import summarize_topic_sync, format_buzz_reply

    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = (settings.openclaw_local_text_model or "").split(",")[0].strip()
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)

    if backend != "ollama" or not endpoint or not model:
        logger.warning("SNS buzz: LLM not configured (backend=%s endpoint=%s model=%s)",
                       backend, endpoint, model)
        return None

    def buzz(query: str) -> str:
        result = summarize_topic_sync(
            query,
            x_client=x_client,
            llm_endpoint=endpoint,
            llm_model=model,
            llm_timeout=timeout,
            ssl_context=ssl_context if endpoint.startswith("https://") else None,
        )
        if result is None:
            return f"找不到關於「{query}」的推文。"
        return format_buzz_reply(result)

    return buzz


def _build_classifier_deps(settings: AssistantSettings, ssl_context=None):
    """Build the dependencies SnsMonitor needs to run the two-opportunity
    classifier: alias source, knowledge retriever, llm_fn, entity researcher.

    Returns a dict whose keys map 1:1 to ensure_monitor's classifier kwargs.
    If the local LLM isn't configured (no ollama backend), returns an empty
    dict so the monitor falls back to the legacy notify path."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = (settings.openclaw_local_text_model or "").split(",")[0].strip()
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)

    if not settings.sns_classifier_enabled:
        logger.info("SNS classifier: disabled via OPENCLAW_SNS_CLASSIFIER_ENABLED")
        return {}
    if backend != "ollama" or not endpoint or not model:
        logger.warning(
            "SNS classifier: local LLM not configured "
            "(backend=%s endpoint=%s model=%s) — falling back to legacy notify",
            backend, endpoint, model,
        )
        return {}

    from openclaw_adapter.knowledge_db import KnowledgeDatabase, format_knowledge_block
    from openclaw_adapter.entity_researcher import EntityResearcher
    from openclaw_adapter.opportunity_agent import _call_ollama_json

    knowledge_db = KnowledgeDatabase(settings.knowledge_db_path)
    logger.info("SNS classifier: knowledge DB ready path=%s", settings.knowledge_db_path)

    llm_ssl = ssl_context if endpoint.startswith("https://") else None

    def llm_fn(prompt: str) -> str:
        return _call_ollama_json(
            endpoint=endpoint, model=model, prompt=prompt,
            timeout_seconds=timeout, ssl_context=llm_ssl,
        )

    researcher = EntityResearcher(
        knowledge_db=knowledge_db,
        endpoint=endpoint,
        model=model,
        timeout_seconds=timeout,
        ssl_context=llm_ssl,
    )
    researcher.start()
    logger.info("SNS classifier: EntityResearcher started")

    from .knowledge_prewarmer import KnowledgePrewarmer
    prewarmer = KnowledgePrewarmer(
        research_fn=researcher.request,
        monitor_db_path=settings.monitor_db_path,
    )
    prewarmer.start()

    def knowledge_retriever(canonicals: tuple[str, ...]) -> str:
        entries = []
        unknown: list[str] = []
        for canonical in canonicals:
            entry = knowledge_db.get_entry(canonical)
            if entry is None:
                unknown.append(canonical)
                continue
            entries.append(entry)
            try:
                knowledge_db.mark_referenced(canonical)
            except Exception:
                logger.exception("knowledge_retriever: mark_referenced failed for %s", canonical)
        return format_knowledge_block(entries, unknown_entities=tuple(unknown))

    def knowledge_appender(payload: dict) -> None:
        """Sink silenced-signal observations into the lobster knowledge base.

        Payload contract (kept stable so sns_monitor_bot stays import-free):
          {"entity": str, "observed_at": str (ISO8601),
           "rationale": str, "suggested_action": str,
           "tweet_url": str, "deadline": str | None}
        """
        try:
            knowledge_db.append_observation(
                entity_alias_or_canonical=payload["entity"],
                observed_at=payload["observed_at"],
                rationale=payload.get("rationale", ""),
                suggested_action=payload.get("suggested_action", ""),
                tweet_url=payload.get("tweet_url", ""),
                deadline=payload.get("deadline"),
            )
        except Exception:
            logger.exception("knowledge_appender failed payload=%s", payload)

    return {
        "classifier_llm_fn": llm_fn,
        "entity_extraction_llm_fn": llm_fn,
        "alias_source": knowledge_db,
        "knowledge_retriever": knowledge_retriever,
        "knowledge_appender": knowledge_appender,
        "entity_research_fn": researcher.request,
        "monitor_db_path": settings.monitor_db_path,
        "opportunity_db_path": settings.opportunity_db_path,
        "min_score_to_push": settings.sns_classifier_min_score,
    }


def _start_sns_monitor(
    *,
    settings: AssistantSettings,
    token: str,
    ssl_context=None,
):
    """Start the SNS monitor daemon thread. Returns (sns_db, buzz_fn)."""
    try:
        from sns_monitor.x_client_web import XClientWeb as XClient
        from sns_monitor.monitor import ensure_monitor
        from sns_monitor.telegram import TelegramClient as SnsTelegramClient
    except ImportError as exc:
        logger.error("SNS monitor: failed to import required modules: %s", exc)
        return None, None

    logger.info("SNS monitor: timeline via Nitter RSS, /snsbuzz search via Reddit JSON API")

    try:
        from sns_monitor.reddit_buzz import RedditBuzzClient
        from sns_monitor.sources import RedditSource, XSource

        db = bootstrap_sns_db(settings)
        reddit_client = RedditBuzzClient()
        x_client = XClient(buzz_search_backend=reddit_client)

        sources = {
            "x": XSource(x_client),
            "reddit": RedditSource(client=reddit_client),
        }

        def notify_fn(
            chat_id: str,
            text: str,
            reply_markup: dict[str, object] | None = None,
        ) -> None:
            """Notify via Telegram. ``reply_markup`` is optional — the SNS
            monitor passes a 👍/👎/💰 inline keyboard for per-tweet posts."""
            try:
                client = SnsTelegramClient(token, ssl_context=ssl_context)
                client.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup,
                )
            except Exception as e:
                logger.exception("SNS notification failed chat_id=%s: %s", chat_id, e)

        classifier_kwargs = _build_classifier_deps(settings, ssl_context=ssl_context)
        monitor, started = ensure_monitor(
            db_path=settings.sns_db_path,
            x_client=x_client,
            sources=sources,
            notify_fn=notify_fn,
            interval_seconds=60,
            **classifier_kwargs,
        )
        logger.info("SNS monitor started=%s running=%s sources=%s classifier=%s",
                    started, monitor.is_running(), sorted(sources.keys()),
                    "on" if classifier_kwargs else "off")
        if started:
            print("[sns-monitor] ✅ SNS monitor started (interval=60s, sources=x+reddit)")
            print("[sns-monitor] 📱 Monitoring X timelines + Reddit subreddits per watch rules")
            if classifier_kwargs:
                print("[sns-monitor] 🧠 Two-opportunity classifier enabled "
                      f"(min_score={settings.sns_classifier_min_score})")

        buzz_fn = _build_sns_buzz_fn(settings, x_client, ssl_context=ssl_context)
        if buzz_fn is not None:
            print("[sns-monitor] ✨ /snsbuzz enabled (Reddit + LLM)")
        return db, buzz_fn
    except Exception as exc:
        logger.exception("SNS monitor startup failed: %s", exc)
        print("[sns-monitor] ❌ Failed to start SNS monitor")
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# CLI Tool Handlers for toolset.py
# ─────────────────────────────────────────────────────────────────────────────


def _configure_sns_add_account_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("screen_name", help="X account screen name (without @)")
    parser.add_argument("--label", default="", help="Optional label for this watch rule")
    parser.add_argument("--chat-id", required=True, help="Telegram chat ID to notify")
    parser.add_argument("--interval", type=int, default=15, help="Check interval in minutes (default 15)")
    parser.add_argument(
        "--keywords",
        "--filters",
        nargs="*",
        default=None,
        help='Only notify account tweets containing any keyword. Accepts: buy sell, buy,sell, or \'["buy","sell"]\'.',
    )
    parser.add_argument("--db", default=settings.sns_db_path, help=f"Database path (default {settings.sns_db_path})")


def _handle_sns_add_account(args: argparse.Namespace, settings: AssistantSettings) -> int:
    """Add an X account to the watch list."""
    from sns_monitor.filters import normalize_keyword_filters
    from sns_monitor.storage import SnsDatabase
    from sns_monitor.models import AccountWatch

    try:
        db = SnsDatabase(args.db)
        db.bootstrap()

        screen_name = args.screen_name.lstrip("@")
        include_keywords = normalize_keyword_filters(args.keywords)
        rule_id = SnsDatabase._watch_rule_id("account", screen_name)
        existing_rule = db.get_watch_rule(rule_id)
        rule = AccountWatch(
            rule_id=rule_id,
            screen_name=screen_name,
            user_id=getattr(existing_rule, "user_id", None),
            label=args.label or getattr(existing_rule, "label", None) or f"@{screen_name}",
            include_keywords=include_keywords,
            enabled=True,
            schedule_minutes=args.interval,
            chat_id=args.chat_id,
            last_checked_at=getattr(existing_rule, "last_checked_at", None),
        )
        db.save_watch_rule(rule)
        suffix = f" filters={list(include_keywords)}" if include_keywords else ""
        print(f"✓ Added X account @{screen_name}{suffix} (id={rule_id[:8]}...)")
        return 0
    except Exception as exc:
        logger.error("Failed to add account: %s", exc)
        print(f"✗ Error: {exc}")
        return 1


def _configure_sns_add_keyword_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("query", help="Search query (keyword or phrase)")
    parser.add_argument("--label", default="", help="Optional label for this watch rule")
    parser.add_argument("--chat-id", required=True, help="Telegram chat ID to notify")
    parser.add_argument("--interval", type=int, default=30, help="Check interval in minutes (default 30)")
    parser.add_argument("--db", default=settings.sns_db_path, help=f"Database path (default {settings.sns_db_path})")


def _handle_sns_add_keyword(args: argparse.Namespace, settings: AssistantSettings) -> int:
    """Add a keyword search to the watch list."""
    from sns_monitor.storage import SnsDatabase
    from sns_monitor.models import KeywordWatch

    try:
        db = SnsDatabase(args.db)
        db.bootstrap()

        rule_id = SnsDatabase._watch_rule_id("keyword", args.query)
        rule = KeywordWatch(
            rule_id=rule_id,
            query=args.query,
            label=args.label or f'"{args.query}"',
            enabled=True,
            schedule_minutes=args.interval,
            chat_id=args.chat_id,
            last_checked_at=None,
        )
        db.save_watch_rule(rule)
        print(f"✓ Added keyword watch: {args.query} (id={rule_id[:8]}...)")
        return 0
    except Exception as exc:
        logger.error("Failed to add keyword: %s", exc)
        print(f"✗ Error: {exc}")
        return 1


def _configure_sns_add_trend_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument(
        "category",
        choices=["trending", "for-you", "news", "sports", "entertainment"],
        help="Trend category to monitor",
    )
    parser.add_argument("--label", default="", help="Optional label for this watch rule")
    parser.add_argument("--chat-id", required=True, help="Telegram chat ID to notify")
    parser.add_argument("--interval", type=int, default=60, help="Check interval in minutes (default 60)")
    parser.add_argument("--db", default=settings.sns_db_path, help=f"Database path (default {settings.sns_db_path})")


def _handle_sns_add_trend(args: argparse.Namespace, settings: AssistantSettings) -> int:
    """Add a trend category to the watch list."""
    from sns_monitor.storage import SnsDatabase
    from sns_monitor.models import TrendWatch

    try:
        db = SnsDatabase(args.db)
        db.bootstrap()

        rule_id = SnsDatabase._watch_rule_id("trend", args.category)
        rule = TrendWatch(
            rule_id=rule_id,
            category=args.category,
            label=args.label or f"Trend: {args.category}",
            enabled=True,
            schedule_minutes=args.interval,
            chat_id=args.chat_id,
            last_checked_at=None,
        )
        db.save_watch_rule(rule)
        print(f"✓ Added trend watch: {args.category} (id={rule_id[:8]}...)")
        return 0
    except Exception as exc:
        logger.error("Failed to add trend: %s", exc)
        print(f"✗ Error: {exc}")
        return 1


def _configure_sns_list_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("--kind", choices=["account", "keyword", "trend"], help="Filter by watch type")
    parser.add_argument("--db", default=settings.sns_db_path, help=f"Database path (default {settings.sns_db_path})")


def _handle_sns_list(args: argparse.Namespace, settings: AssistantSettings) -> int:
    """List all SNS watch rules."""
    from sns_monitor.storage import SnsDatabase

    try:
        db = SnsDatabase(args.db)
        db.bootstrap()

        rules = db.list_watch_rules(kind=args.kind)
        if not rules:
            print("No watch rules found.")
            return 0

        print(f"SNS Watch Rules ({len(rules)} total):")
        print()
        for rule in rules:
            status = "✓ ENABLED" if rule.enabled else "✗ DISABLED"
            last_checked = rule.last_checked_at.isoformat() if rule.last_checked_at else "Never"

            # Format based on rule type
            if rule.__class__.__name__ == "AccountWatch":
                filters = f" filters={', '.join(rule.include_keywords)}" if rule.include_keywords else ""
                info = f"@{rule.screen_name}{filters}"
            elif rule.__class__.__name__ == "KeywordWatch":
                info = f'Keyword: "{rule.query}"'
            elif rule.__class__.__name__ == "TrendWatch":
                info = f"Trend: {rule.category}"
            else:
                info = "Unknown type"

            print(f"  {status} | {info} | {rule.label}")
            print(f"         ID: {rule.rule_id}")
            print(f"         Interval: {rule.schedule_minutes} min | Chat: {rule.chat_id}")
            print(f"         Last checked: {last_checked}")
            print()

        return 0
    except Exception as exc:
        logger.error("Failed to list rules: %s", exc)
        print(f"✗ Error: {exc}")
        return 1


def _configure_sns_toggle_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("rule_id", help="Rule ID to toggle")
    parser.add_argument("--enabled", action="store_true", help="Enable the rule")
    parser.add_argument("--disabled", action="store_true", help="Disable the rule")
    parser.add_argument("--db", default=settings.sns_db_path, help=f"Database path (default {settings.sns_db_path})")


def _handle_sns_toggle(args: argparse.Namespace, settings: AssistantSettings) -> int:
    """Toggle a watch rule enabled/disabled."""
    from sns_monitor.storage import SnsDatabase

    if not args.enabled and not args.disabled:
        print("✗ Must specify --enabled or --disabled")
        return 1

    try:
        db = SnsDatabase(args.db)
        db.bootstrap()

        enabled = args.enabled
        db.toggle_watch_rule(args.rule_id, enabled=enabled)

        status = "ENABLED" if enabled else "DISABLED"
        print(f"✓ Rule {args.rule_id[:8]}... {status}")
        return 0
    except Exception as exc:
        logger.error("Failed to toggle rule: %s", exc)
        print(f"✗ Error: {exc}")
        return 1


def _configure_sns_delete_parser(parser: argparse.ArgumentParser, settings: AssistantSettings) -> None:
    parser.add_argument("rule_id", help="Rule ID to delete")
    parser.add_argument("--db", default=settings.sns_db_path, help=f"Database path (default {settings.sns_db_path})")


def _handle_sns_delete(args: argparse.Namespace, settings: AssistantSettings) -> int:
    """Delete a watch rule."""
    from sns_monitor.storage import SnsDatabase

    try:
        db = SnsDatabase(args.db)
        db.bootstrap()

        found = db.delete_watch_rule(args.rule_id)
        if found:
            print(f"✓ Deleted rule {args.rule_id[:8]}...")
            return 0
        else:
            print(f"✗ Rule {args.rule_id} not found")
            return 1
    except Exception as exc:
        logger.error("Failed to delete rule: %s", exc)
        print(f"✗ Error: {exc}")
        return 1
