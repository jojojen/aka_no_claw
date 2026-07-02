"""Telegram-side SNS command handlers.

Extracted from price_monitor_bot.bot so that the base dispatcher contains no
SNS domain logic.

All builders accept:
- ``sns_db``    — SnsDatabase (or None) opened read-only for list/resolve lookups.
- ``sns_inbox`` — SnsInbox (or None) for write operations.

When ``sns_inbox`` is provided, writes go through the inbox (single-writer-per-file
pattern: sns_monitor service is the sole writer to sns.sqlite3). When None, writes
fall back to direct ``sns_db`` access (used in tests / standalone mode).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from telegram_core.contracts import TEXT_CLARIFICATION_TTL_SECONDS, TelegramTextReplyPlan
from telegram_core.list_view import LIST_VIEW_MODE_READ, ListRow, build_list_view

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers (shared between command and callback builders)
# ---------------------------------------------------------------------------

def _resolve_sns_rule_id(sns_db, target: str) -> str | None:
    """Resolve a user-supplied target string to a full SNS rule_id, or None."""
    if sns_db is None:
        return None
    cleaned = target.strip()
    if not cleaned:
        return None
    rules = list(sns_db.list_watch_rules())

    for rule in rules:
        if rule.rule_id == cleaned or rule.rule_id.startswith(cleaned):
            return rule.rule_id

    source_filter = "x"
    had_source_prefix = False
    body = cleaned
    for src in ("x:",):
        if cleaned.lower().startswith(src):
            source_filter = src[:-1]
            had_source_prefix = True
            body = cleaned[len(src):].strip()
            break

    def _source_matches(rule) -> bool:
        return (not had_source_prefix) or getattr(rule, "source", "x") == source_filter

    if body.startswith("@"):
        handle = body.lstrip("@").lower()
        for rule in rules:
            if getattr(rule, "screen_name", "").lower() == handle and _source_matches(rule):
                return rule.rule_id
        return None

    if body.lower().startswith("keyword:"):
        query = body.split(":", 1)[1].strip().lower()
        for rule in rules:
            if getattr(rule, "query", "").lower() == query and _source_matches(rule):
                return rule.rule_id
        return None

    bare = body.lstrip("@").lower()
    for rule in rules:
        if getattr(rule, "screen_name", "").lower() == bare and _source_matches(rule):
            return rule.rule_id
    return None


def _describe_sns_rule(sns_db, rule_id: str) -> str:
    if sns_db is None:
        return rule_id[:8]
    for rule in sns_db.list_watch_rules():
        if rule.rule_id != rule_id:
            continue
        screen_name = getattr(rule, "screen_name", None)
        if screen_name:
            include_keywords = getattr(rule, "include_keywords", ())
            filters = f" [{', '.join(include_keywords)}]" if include_keywords else ""
            return f"@{screen_name}{filters}"
        query = getattr(rule, "query", None)
        if query:
            return f"關鍵字「{query}」"
        return rule.rule_id[:8]
    return rule_id[:8]


def _do_sns_delete(sns_db, raw: str, sns_inbox=None) -> str:
    if sns_db is None:
        return "SNS 監控尚未啟用（sns_db 未設定）。"
    try:
        target = raw.strip()
        if not target:
            return "請提供 @帳號 或規則 ID。例如：/snsdelete @elonmusk 或 /snsdelete abc12345"
        rule_id = _resolve_sns_rule_id(sns_db, target)
        if rule_id is None:
            return f"找不到對應的 SNS 規則：{target}"
        label = _describe_sns_rule(sns_db, rule_id)
        if sns_inbox is not None:
            sns_inbox.push("delete_rule", {"rule_id": rule_id})
            logger.info("SNS rule delete queued rule_id=%s target=%s", rule_id, target)
        else:
            found = sns_db.delete_watch_rule(rule_id)
            if not found:
                return f"找不到規則 {target}"
            logger.info("SNS rule deleted rule_id=%s target=%s", rule_id, target)
        return f"✓ 已刪除 SNS 監控：{label}"
    except Exception as exc:
        logger.exception("SNS delete failed raw=%s", raw)
        return f"刪除失敗：{exc}"


# ---------------------------------------------------------------------------
# Command handler builders
# ---------------------------------------------------------------------------

def build_sns_add_handler(sns_db, sns_inbox=None) -> Callable[[str, str], str]:
    """Build handler for /snsadd (and /sns_add). ``(remainder, chat_id) -> str``."""

    def handler(raw: str, chat_id: str) -> str:
        if sns_db is None and sns_inbox is None:
            return "SNS 監控尚未啟用（sns_db 未設定）。"
        from sns_monitor.filters import (
            extract_labeled_brackets,
            extract_schedule_minutes,
            parse_account_watch_text,
            rewrite_social_url,
            split_source_prefix,
        )
        from sns_monitor.models import AccountWatch, KeywordWatch, TrendWatch
        from sns_monitor.storage import SnsDatabase

        # Default scan cadence for a new /snsadd rule when the user gives no
        # explicit schedule:NN. 720m (12h) keeps polling gentle on each source
        # (avoids rate-limit/ban) while still surfacing new posts twice a day.
        _DEFAULT_SCHEDULE_MINUTES = 720
        default_schedules: dict[tuple[str, str], int] = {
            ("x", "account"): _DEFAULT_SCHEDULE_MINUTES,
            ("x", "keyword"): _DEFAULT_SCHEDULE_MINUTES,
            ("x", "trend"): _DEFAULT_SCHEDULE_MINUTES,
        }
        source_label = {"x": "X"}

        try:
            raw = raw.strip()
            if not raw:
                return (
                    '用法：/snsadd x:@username\n'
                    '     /snsadd x:@username filter[抽選] domain[pokemon] schedule:30\n'
                    '     /snsadd x:keyword:搜尋詞 domain[gundam]\n'
                    '     /snsadd x:trend:trending\n'
                    '（關鍵字風向請改用 /snsbuzz <關鍵字>，走 4chan）'
                )

            raw = rewrite_social_url(raw)
            schedule_override, raw = extract_schedule_minutes(raw)
            source, body = split_source_prefix(raw)

            account_target = parse_account_watch_text(body)
            if account_target is not None:
                screen_name, include_keywords, domains = account_target
                rule_id = SnsDatabase._watch_rule_id("account", screen_name, source)
                existing_rule = sns_db.get_watch_rule(rule_id)
                resolved_domains = (
                    domains if domains is not None else getattr(existing_rule, "domains", ())
                )
                schedule_minutes = (
                    schedule_override
                    if schedule_override is not None
                    else getattr(existing_rule, "schedule_minutes", None)
                    or default_schedules.get((source, "account"), _DEFAULT_SCHEDULE_MINUTES)
                )
                display = f"@{screen_name}"
                rule = AccountWatch(
                    rule_id=rule_id,
                    screen_name=screen_name,
                    user_id=getattr(existing_rule, "user_id", None),
                    label=getattr(existing_rule, "label", None) or display,
                    include_keywords=include_keywords,
                    domains=resolved_domains,
                    enabled=True,
                    schedule_minutes=schedule_minutes,
                    chat_id=chat_id,
                    last_checked_at=getattr(existing_rule, "last_checked_at", None),
                    source=source,
                )
                if sns_inbox is not None:
                    sns_inbox.push_rule(rule, chat_id=chat_id)
                elif sns_db is not None:
                    sns_db.save_watch_rule(rule)
                logger.info(
                    "SNS account watch queued/added source=%s target=%s chat_id=%s "
                    "include_keywords=%s domains=%s schedule=%dm",
                    source, screen_name, chat_id, include_keywords,
                    resolved_domains, schedule_minutes,
                )
                filter_line = f"\n篩選：{', '.join(include_keywords)}" if include_keywords else ""
                domain_line = f"\n領域：{', '.join(resolved_domains)}" if resolved_domains else ""
                kind_label = "帳號"
                return (
                    f"✓ 已新增 {source_label.get(source, source)} {kind_label}追蹤：{display}"
                    f"{filter_line}{domain_line}\n排程：每 {schedule_minutes} 分鐘\nID: {rule_id[:8]}…"
                )

            if body.lower().startswith("keyword:"):
                _, parsed_domains, body_clean = extract_labeled_brackets(body[len("keyword:"):])
                query = body_clean.strip()
                if not query:
                    return "請提供搜尋關鍵字。例如：/snsadd x:keyword:機動戰士 domain[gundam]"
                rule_id = SnsDatabase._watch_rule_id("keyword", query, source)
                existing_rule = sns_db.get_watch_rule(rule_id)
                resolved_domains = (
                    parsed_domains if parsed_domains is not None
                    else getattr(existing_rule, "domains", ())
                )
                schedule_minutes = (
                    schedule_override
                    if schedule_override is not None
                    else getattr(existing_rule, "schedule_minutes", None)
                    or default_schedules.get((source, "keyword"), _DEFAULT_SCHEDULE_MINUTES)
                )
                rule = KeywordWatch(
                    rule_id=rule_id,
                    query=query,
                    label=f'"{query}"',
                    domains=resolved_domains,
                    enabled=True,
                    schedule_minutes=schedule_minutes,
                    chat_id=chat_id,
                    last_checked_at=None,
                    source=source,
                )
                if sns_inbox is not None:
                    sns_inbox.push_rule(rule, chat_id=chat_id)
                elif sns_db is not None:
                    sns_db.save_watch_rule(rule)
                logger.info(
                    "SNS keyword watch queued/added source=%s query=%s chat_id=%s domains=%s schedule=%dm",
                    source, query, chat_id, resolved_domains, schedule_minutes,
                )
                domain_line = f"\n領域：{', '.join(resolved_domains)}" if resolved_domains else ""
                return (
                    f'✓ 已新增 {source_label.get(source, source)} 關鍵字追蹤："{query}"{domain_line}'
                    f"\n排程：每 {schedule_minutes} 分鐘\nID: {rule_id[:8]}…"
                )

            if body.lower().startswith("trend:"):
                _, parsed_domains, body_clean = extract_labeled_brackets(body[len("trend:"):])
                category = body_clean.strip()
                if category not in {"trending", "for-you", "news", "sports", "entertainment"}:
                    return "不支援的分類。請使用：trending, for-you, news, sports, 或 entertainment"
                rule_id = SnsDatabase._watch_rule_id("trend", category, source)
                existing_rule = sns_db.get_watch_rule(rule_id)
                resolved_domains = (
                    parsed_domains if parsed_domains is not None
                    else getattr(existing_rule, "domains", ())
                )
                schedule_minutes = (
                    schedule_override
                    if schedule_override is not None
                    else getattr(existing_rule, "schedule_minutes", None)
                    or default_schedules.get((source, "trend"), _DEFAULT_SCHEDULE_MINUTES)
                )
                rule = TrendWatch(
                    rule_id=rule_id,
                    category=category,
                    label=f"Trend: {category}",
                    domains=resolved_domains,
                    enabled=True,
                    schedule_minutes=schedule_minutes,
                    chat_id=chat_id,
                    last_checked_at=None,
                    source=source,
                )
                if sns_inbox is not None:
                    sns_inbox.push_rule(rule, chat_id=chat_id)
                elif sns_db is not None:
                    sns_db.save_watch_rule(rule)
                logger.info(
                    "SNS trend watch queued/added source=%s category=%s chat_id=%s schedule=%dm",
                    source, category, chat_id, schedule_minutes,
                )
                return (
                    f"✓ 已新增 {source_label.get(source, source)} 熱門話題追蹤：{category}"
                    f"\n排程：每 {schedule_minutes} 分鐘\nID: {rule_id[:8]}…"
                )

            return (
                '不認識的格式。用法：\n'
                '  /snsadd x:@username [filter[…] domain[…] schedule:NN]\n'
                '  /snsadd x:keyword:搜尋詞 / x:trend:trending\n'
                '（關鍵字風向請改用 /snsbuzz <關鍵字>，走 4chan）'
            )
        except Exception as exc:
            logger.exception("SNS add failed raw=%s chat_id=%s", raw, chat_id)
            return f"新增失敗：{exc}"

    return handler


def build_snslist_view_fn(sns_db) -> Callable[..., tuple[str, "dict | None", int]]:
    """Build paginated view function for list kind ``sl``."""

    def view_fn(*, page: int = 0, mode: str = LIST_VIEW_MODE_READ):
        if sns_db is None:
            return "SNS 監控尚未啟用（sns_db 未設定）。", None, 0
        try:
            rules = list(sns_db.list_watch_rules())
        except Exception as exc:
            logger.exception("SNS list failed")
            return f"列表失敗：{exc}", None, 0

        rules.sort(key=lambda r: (not r.enabled, r.rule_id))
        items: list[ListRow] = []
        for rule in rules:
            status = "✓" if rule.enabled else "✗"
            source = getattr(rule, "source", "x")
            source_tag = f"[{source}] "
            screen_name = getattr(rule, "screen_name", None)
            query_text = getattr(rule, "query", None)
            category = getattr(rule, "category", None)
            if screen_name:
                handle_display = f"@{screen_name}"
                include_kw = getattr(rule, "include_keywords", ()) or ()
                filters = f" filter[{', '.join(include_kw)}]" if include_kw else ""
                info = f"{handle_display}{filters}"
                short = handle_display
            elif query_text:
                info = f'"{query_text}"'
                short = f'"{query_text[:18]}"'
            elif category:
                info = f"Trend:{category}"
                short = f"Trend:{category}"
            else:
                info = "Unknown"
                short = rule.rule_id[:8]
            domains = getattr(rule, "domains", ())
            domain_segment = f" domain[{', '.join(domains)}]" if domains else " domain[?]"
            schedule_segment = (
                f" schedule:{rule.schedule_minutes}m"
                if getattr(rule, "schedule_minutes", None) else ""
            )
            text_block = (
                f"  {status} {source_tag}{info}{domain_segment}{schedule_segment}"
                f" ({rule.rule_id[:8]}…)"
            )
            items.append(ListRow(id=rule.rule_id, text=text_block, short_label=short))

        return build_list_view(
            list_kind="sl",
            items=items,
            page=page,
            mode=mode,
            list_title="📋 SNS 監控規則",
            empty_message="尚無 SNS 監控規則。\n用法：/snsadd @username",
        )

    return view_fn


def build_snslist_handler(sns_db) -> Callable[[str, str], tuple]:
    """Build handler for /snslist that returns ``(text, markup)``."""
    view_fn = build_snslist_view_fn(sns_db)

    def handler(remainder: str, chat_id: str):
        text, markup, _ = view_fn(page=0, mode=LIST_VIEW_MODE_READ)
        return text, markup

    return handler


def build_sns_rule_deleter(sns_db, sns_inbox=None) -> tuple[Callable[[str], bool], str]:
    """Return ``(deleter_fn, human_label)`` for list kind ``sl``."""

    def delete_fn(rule_id: str) -> bool:
        if sns_inbox is not None:
            try:
                sns_inbox.push("delete_rule", {"rule_id": rule_id})
                return True
            except Exception:
                logger.exception("SNS delete inbox push failed rule_id=%s", rule_id)
                return False
        if sns_db is None:
            return False
        try:
            return bool(sns_db.delete_watch_rule(rule_id))
        except Exception:
            logger.exception("SNS delete by id failed rule_id=%s", rule_id)
            return False

    return delete_fn, "SNS 規則"


def build_sns_delete_handler(sns_db, sns_inbox=None) -> Callable[[str, str], str]:
    """Build handler for /snsdelete. ``(remainder, chat_id) -> str``."""

    def handler(raw: str, chat_id: str) -> str:
        return _do_sns_delete(sns_db, raw, sns_inbox=sns_inbox)

    return handler


def build_sns_buzz_handler(buzz_fn) -> Callable[[str, str], str]:
    """Build handler for /snsbuzz. ``(remainder, chat_id) -> str``."""

    def handler(raw: str, chat_id: str) -> str:
        if buzz_fn is None:
            return "SNS Buzz 功能尚未啟用（需要 4chan client 與 LLM endpoint）。"
        query = raw.strip()
        if not query:
            return "請提供關鍵字。例如：/snsbuzz amd"
        try:
            return buzz_fn(query)
        except Exception as exc:
            logger.exception("SNS buzz failed query=%s", query)
            return f"熱門整理失敗：{exc}"

    return handler


def build_sns_clear_filter_handler(sns_db, sns_inbox=None) -> Callable[[str, str], str]:
    """Build handler for NL ``sns_clear_filter`` intent. ``(handle, chat_id) -> str``."""

    def handler(handle: str, chat_id: str) -> str:
        if sns_db is None:
            return "SNS 監控尚未啟用（sns_db 未設定）。"
        from dataclasses import replace
        from sns_monitor.models import AccountWatch
        from sns_monitor.storage import SnsDatabase

        try:
            screen_name = handle.lstrip("@").strip()
            if not screen_name:
                return "請提供 @ 帳號，例如：把 @elonmusk 的 filter 拿掉。"
            rule_id = SnsDatabase._watch_rule_id("account", screen_name)
            existing_rule = sns_db.get_watch_rule(rule_id)
            if not isinstance(existing_rule, AccountWatch):
                return f"找不到 @{screen_name} 的 X 帳號追蹤規則（請先用 /snsadd 新增）。"
            if not existing_rule.include_keywords:
                return f"✓ @{screen_name} 目前沒有 filter，無需清空。"
            previous = existing_rule.include_keywords
            cleared = replace(existing_rule, include_keywords=())
            if sns_inbox is not None:
                sns_inbox.push_rule(cleared, chat_id=chat_id)
            else:
                sns_db.save_watch_rule(cleared)
            logger.info("SNS filter cleared/queued screen_name=%s previous=%s", screen_name, previous)
            return f"✓ 已清空 @{screen_name} 的 filter（追蹤仍啟用，原本：{', '.join(previous)}）。"
        except Exception as exc:
            logger.exception("SNS clear filter failed handle=%s", handle)
            return f"清空 filter 失敗：{exc}"

    return handler


# ---------------------------------------------------------------------------
# Callback handler builders
# ---------------------------------------------------------------------------

def build_snsdel_callback_handler(sns_db, sns_inbox=None) -> Callable[[str, str, str], tuple]:
    """Build callback for ``snsdel:<handle>`` — notification one-tap delete."""

    def handler(payload: str, original_text: str, chat_id: str) -> tuple:
        handle = payload.lstrip("@")
        reply = _do_sns_delete(sns_db, f"@{handle}", sns_inbox=sns_inbox)
        if reply.startswith("✓"):
            return f"已刪除 @{handle}", f"{original_text}\n\n✓ 已刪除 @{handle}", None
        if reply.startswith("找不到"):
            return (
                f"已經不在追蹤 @{handle}",
                f"{original_text}\n\n✓ 已刪除 @{handle}（先前已移除）",
                None,
            )
        return reply[:200], None, None

    return handler


def build_snsaddok_callback_handler(sns_db, sns_inbox=None) -> Callable[[str, str, str], tuple]:
    """Build callback for ``snsaddok:<handle>`` — notification one-tap positive feedback."""

    def handler(payload: str, original_text: str, chat_id: str) -> tuple:
        handle = payload.lstrip("@")
        if sns_db is None and sns_inbox is None:
            return "SNS monitor 未啟用，無法寫入回饋", None, None
        try:
            domains: tuple = ()
            if sns_db is not None:
                from sns_monitor.models import AccountWatch as _AccountWatch
                rule = next(
                    (r for r in sns_db.list_watch_rules()
                     if isinstance(r, _AccountWatch)
                     and (r.screen_name or "").lower() == handle.lower()),
                    None,
                )
                domains = tuple(getattr(rule, "domains", ()) or ()) if rule else ()

            if sns_inbox is not None:
                sns_inbox.push("auto_discovery_feedback", {
                    "screen_name": handle,
                    "polarity": "positive",
                    "domains": list(domains),
                    "chat_id": str(chat_id),
                }, chat_id=str(chat_id))
            elif sns_db is not None:
                sns_db.record_auto_discovery_feedback(
                    screen_name=handle, polarity="positive",
                    domains=domains, chat_id=str(chat_id),
                )
            toast = f"👍 已記錄 @{handle}"
            new_text = f"{original_text}\n\n👍 已記錄為投資訊號帳號"
            return toast, new_text, None
        except Exception:
            logger.exception("snsaddok: positive feedback failed handle=@%s", handle)
            return "回饋寫入失敗", None, None

    return handler


def build_snsfb_callback_handler(sns_db, sns_inbox=None) -> Callable[[str, str, str], tuple]:
    """Build callback for ``snsfb:<kind>:<tweet_id>:<rule_id>`` — per-post feedback."""

    def handler(payload: str, original_text: str, chat_id: str) -> tuple:
        parts = payload.split(":", 2)
        if len(parts) != 3 or parts[0] not in {"up", "down", "bought"}:
            return "未知回饋", None, None
        kind, tweet_id, rule_id = parts

        if sns_inbox is not None:
            sns_inbox.push("feedback", {
                "tweet_id": tweet_id,
                "rule_id": rule_id,
                "kind": kind,
                "chat_id": str(chat_id),
            }, chat_id=str(chat_id))
            # Optimistic toast — service will apply and can push a Telegram notification
            if kind == "up":
                toast = "✓ 已記錄 👍（已提高同類推文推播機率）"
            elif kind == "bought":
                toast = "✓ 已記錄 💰（已提高同類推文推播機率）"
            else:
                toast = "✓ 已標記不感興趣（24h cooldown）"
            return toast, f"{original_text}\n\n{toast}", None

        sns_db_path = getattr(sns_db, "path", None) if sns_db is not None else None
        if sns_db_path is None:
            return "SNS monitor 未啟用，無法寫入回饋", None, None
        try:
            from sns_monitor.feedback import record_sns_feedback
            from sns_monitor.storage import SnsDatabase

            db = SnsDatabase(sns_db_path)
            result = record_sns_feedback(
                db=db, tweet_id=tweet_id, rule_id=rule_id,
                chat_id=str(chat_id), kind=kind,
            )
        except Exception:
            logger.exception(
                "snsfb feedback failed tweet_id=%s rule_id=%s kind=%s",
                tweet_id, rule_id, kind,
            )
            return "回饋寫入失敗，請看 log", None, None

        if result.get("status") != "ok":
            reason = result.get("reason", "unknown")
            return f"記錄失敗：{reason}", None, None

        side_effects = list(result.get("side_effects") or ())
        if kind == "up":
            toast = "✓ 已記錄 👍（已提高同類推文推播機率）"
        elif kind == "bought":
            toast = "✓ 已記錄 💰（已提高同類推文推播機率）"
        else:
            if "rule_disabled" in side_effects:
                toast = "✓ 已標記不感興趣（累計過閾值，rule 自動停用）"
            else:
                toast = "✓ 已標記不感興趣（24h cooldown）"
        new_text = f"{original_text}\n\n{toast}"
        return toast, new_text, None

    return handler


# ---------------------------------------------------------------------------
# SNS bulk update (preview → confirm/cancel) — moved from price_monitor_bot.bot
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PendingTelegramSnsBulkUpdate:
    """A SNS bulk-rule update that's been previewed to the user and is
    waiting on a confirm / cancel inline-button tap.

    The ``action`` field selects which apply helper runs on confirm:
    - ``"add"``           → apply_bulk_keyword_filter_add (uses keywords)
    - ``"remove"``        → apply_bulk_keyword_filter_remove (uses keywords)
    - ``"set_schedule"``  → apply_bulk_schedule_update (uses schedule_minutes)
    """
    chat_id: str
    bulk_target_domain: str
    keywords: tuple[str, ...]
    affected_rule_ids: tuple[str, ...]
    action: str = "add"
    schedule_minutes: int | None = None
    created_at: float = field(default_factory=time.monotonic)

    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > TEXT_CLARIFICATION_TTL_SECONDS


def build_sns_bulk_add_filter_plan(
    sns_db,
    *,
    chat_id: str | int,
    target_domain: str,
    keywords: tuple[str, ...],
    set_pending: Callable[[PendingTelegramSnsBulkUpdate], None],
) -> TelegramTextReplyPlan:
    """Build the preview message for ``sns_bulk_add_filter`` and stash a
    PendingTelegramSnsBulkUpdate so the confirm/cancel inline buttons
    know what to do."""
    if sns_db is None:
        return TelegramTextReplyPlan(ack=None, reply="SNS 監控尚未啟用（sns_db 未設定）。")
    if not target_domain:
        return TelegramTextReplyPlan(ack=None, reply="我看不太懂你要對哪類帳號加 filter，請重講一次。")
    if not keywords:
        return TelegramTextReplyPlan(ack=None, reply="請告訴我要加哪個 filter 關鍵字。")

    try:
        from sns_monitor.bulk_filter import (
            find_accounts_matching_domain,
            resolve_target_domain_set,
        )
    except ImportError:
        logger.exception("sns_monitor.bulk_filter import failed")
        return TelegramTextReplyPlan(ack=None, reply="SNS bulk-filter 模組載入失敗。")

    target_set = resolve_target_domain_set(target_domain)
    accounts = find_accounts_matching_domain(sns_db, target_set)
    if not accounts:
        return TelegramTextReplyPlan(
            ack=None,
            reply=f"找不到任何 domain 跟 {target_domain} 有交集的 SNS 帳號。",
        )

    kw_display = "、".join(keywords)
    lines = [
        f"🎯 找到 {len(accounts)} 個 {target_domain} 相關帳號，要把 filter 加上：{kw_display}",
        "",
    ]
    for rule in accounts[:10]:
        existing = (
            f" 現有 filter[{', '.join(rule.include_keywords)}]"
            if rule.include_keywords else " 現有 filter[]"
        )
        domain_label = f" domain[{', '.join(rule.domains)}]" if rule.domains else ""
        lines.append(f"- @{rule.screen_name}{domain_label}{existing}")
    if len(accounts) > 10:
        lines.append(f"  …以及另外 {len(accounts) - 10} 筆")
    lines.append("")
    lines.append(f"確認要把「{kw_display}」加進這 {len(accounts)} 個帳號的 filter 嗎？")

    pending = PendingTelegramSnsBulkUpdate(
        chat_id=str(chat_id),
        bulk_target_domain=target_domain,
        keywords=keywords,
        affected_rule_ids=tuple(r.rule_id for r in accounts),
        action="add",
    )
    set_pending(pending)

    reply_markup = {
        "inline_keyboard": [
            [{"text": f"✓ 全部修改 ({len(accounts)})", "callback_data": "bulk:c"}],
            [{"text": "✖️ 取消", "callback_data": "bulk:x"}],
        ]
    }
    return TelegramTextReplyPlan(
        ack=None,
        reply="\n".join(lines),
        reply_markup=reply_markup,
    )


def build_sns_bulk_remove_filter_plan(
    sns_db,
    *,
    chat_id: str | int,
    target_domain: str,
    keywords: tuple[str, ...],
    set_pending: Callable[[PendingTelegramSnsBulkUpdate], None],
) -> TelegramTextReplyPlan:
    """Preview for ``sns_bulk_remove_filter`` — symmetric to the add plan
    but the message text describes a removal, and the pending state
    carries ``action="remove"``."""
    if sns_db is None:
        return TelegramTextReplyPlan(ack=None, reply="SNS 監控尚未啟用（sns_db 未設定）。")
    if not target_domain:
        return TelegramTextReplyPlan(ack=None, reply="我看不太懂你要對哪類帳號移除 filter 關鍵字，請重講一次。")
    if not keywords:
        return TelegramTextReplyPlan(ack=None, reply="請告訴我要移除哪個 filter 關鍵字。")

    try:
        from sns_monitor.bulk_filter import (
            find_accounts_matching_domain,
            resolve_target_domain_set,
        )
    except ImportError:
        logger.exception("sns_monitor.bulk_filter import failed")
        return TelegramTextReplyPlan(ack=None, reply="SNS bulk-filter 模組載入失敗。")

    target_set = resolve_target_domain_set(target_domain)
    accounts = find_accounts_matching_domain(sns_db, target_set)
    if not accounts:
        return TelegramTextReplyPlan(
            ack=None,
            reply=f"找不到任何 domain 跟 {target_domain} 有交集的 SNS 帳號。",
        )

    drop_set = {kw.casefold() for kw in keywords if kw}
    affected_count = sum(
        1 for r in accounts if any(kw.casefold() in drop_set for kw in r.include_keywords)
    )

    kw_display = "、".join(keywords)
    lines = [
        f"🗑 找到 {len(accounts)} 個 {target_domain} 相關帳號，要從 filter 移除：{kw_display}",
        f"   其中 {affected_count} 個目前有這些 keyword（會被改），{len(accounts) - affected_count} 個沒有（不會動）",
        "",
    ]
    for rule in accounts[:10]:
        existing = (
            f" 現有 filter[{', '.join(rule.include_keywords)}]"
            if rule.include_keywords else " 現有 filter[]"
        )
        domain_label = f" domain[{', '.join(rule.domains)}]" if rule.domains else ""
        lines.append(f"- @{rule.screen_name}{domain_label}{existing}")
    if len(accounts) > 10:
        lines.append(f"  …以及另外 {len(accounts) - 10} 筆")
    lines.append("")
    lines.append(f"確認要從這 {len(accounts)} 個帳號的 filter 移除「{kw_display}」嗎？")

    pending = PendingTelegramSnsBulkUpdate(
        chat_id=str(chat_id),
        bulk_target_domain=target_domain,
        keywords=keywords,
        affected_rule_ids=tuple(r.rule_id for r in accounts),
        action="remove",
    )
    set_pending(pending)

    reply_markup = {
        "inline_keyboard": [
            [{"text": f"✓ 全部移除 ({affected_count})", "callback_data": "bulk:c"}],
            [{"text": "✖️ 取消", "callback_data": "bulk:x"}],
        ]
    }
    return TelegramTextReplyPlan(
        ack=None,
        reply="\n".join(lines),
        reply_markup=reply_markup,
    )


def build_sns_bulk_update_schedule_plan(
    sns_db,
    *,
    chat_id: str | int,
    target_domain: str,
    minutes: int | None,
    set_pending: Callable[[PendingTelegramSnsBulkUpdate], None],
) -> TelegramTextReplyPlan:
    """Preview for ``sns_bulk_update_schedule`` — same pattern but the
    pending state carries ``action="set_schedule"`` and ``schedule_minutes``."""
    if sns_db is None:
        return TelegramTextReplyPlan(ack=None, reply="SNS 監控尚未啟用（sns_db 未設定）。")
    if not target_domain:
        return TelegramTextReplyPlan(ack=None, reply="我看不太懂你要對哪類帳號改 schedule，請重講一次。")
    if minutes is None or not (5 <= minutes <= 1440):
        return TelegramTextReplyPlan(ack=None, reply="請給一個有效的分鐘數 (5-1440)。")

    try:
        from sns_monitor.bulk_filter import (
            find_accounts_matching_domain,
            resolve_target_domain_set,
        )
    except ImportError:
        logger.exception("sns_monitor.bulk_filter import failed")
        return TelegramTextReplyPlan(ack=None, reply="SNS bulk-filter 模組載入失敗。")

    target_set = resolve_target_domain_set(target_domain)
    accounts = find_accounts_matching_domain(sns_db, target_set)
    if not accounts:
        return TelegramTextReplyPlan(
            ack=None,
            reply=f"找不到任何 domain 跟 {target_domain} 有交集的 SNS 帳號。",
        )
    already_count = sum(1 for r in accounts if r.schedule_minutes == minutes)
    will_change = len(accounts) - already_count

    lines = [
        f"⏱ 找到 {len(accounts)} 個 {target_domain} 相關帳號，要把 schedule 改成 {minutes} 分鐘",
        f"   其中 {already_count} 個本來就是 {minutes} 分鐘（不會動），{will_change} 個會被改",
        "",
    ]
    for rule in accounts[:10]:
        domain_label = f" domain[{', '.join(rule.domains)}]" if rule.domains else ""
        lines.append(f"- @{rule.screen_name}{domain_label} 目前 schedule={rule.schedule_minutes}m")
    if len(accounts) > 10:
        lines.append(f"  …以及另外 {len(accounts) - 10} 筆")
    lines.append("")
    lines.append(f"確認要把這 {len(accounts)} 個帳號的 schedule 改成 {minutes} 分鐘嗎？")

    pending = PendingTelegramSnsBulkUpdate(
        chat_id=str(chat_id),
        bulk_target_domain=target_domain,
        keywords=(),
        affected_rule_ids=tuple(r.rule_id for r in accounts),
        action="set_schedule",
        schedule_minutes=minutes,
    )
    set_pending(pending)

    reply_markup = {
        "inline_keyboard": [
            [{"text": f"✓ 全部修改 ({will_change})", "callback_data": "bulk:c"}],
            [{"text": "✖️ 取消", "callback_data": "bulk:x"}],
        ]
    }
    return TelegramTextReplyPlan(
        ack=None,
        reply="\n".join(lines),
        reply_markup=reply_markup,
    )


def handle_sns_bulk_update_callback(
    *,
    sns_db,
    pop_pending: Callable[[str | int], PendingTelegramSnsBulkUpdate | None],
    action: str,
    chat_id: str | int,
    original_text: str,
) -> tuple[str | None, str | None, dict[str, object] | None]:
    """Resolve a `bulk:c` / `bulk:x` callback (confirm / cancel of an SNS
    bulk filter-add preview). Returns (toast, edit_text, edit_reply_markup).

    The pending preview state is popped on either action so a second tap is
    harmless. Confirm applies the keyword merge via
    ``sns_monitor.bulk_filter.apply_bulk_keyword_filter_add`` against the
    rule ids captured at preview time — fresh DB lookups, so any concurrent
    deletion shows up as fewer updates.
    """
    pending = pop_pending(chat_id)
    if pending is None:
        return "操作已過期，請重新輸入", f"{original_text}\n\n（操作已過期）", None

    if action == "x":
        return "已取消", f"{original_text}\n\n（已取消）", None

    if action != "c":
        return "未知操作", None, None

    if sns_db is None:
        return "SNS 監控未啟用", f"{original_text}\n\n（SNS 監控未啟用）", None

    try:
        from sns_monitor.bulk_filter import (
            apply_bulk_keyword_filter_add,
            apply_bulk_keyword_filter_remove,
            apply_bulk_schedule_update,
        )
    except ImportError:
        logger.exception("sns_monitor.bulk_filter import failed in bulk callback")
        return "套用失敗", f"{original_text}\n\n（套用失敗：模組載入錯誤）", None

    fresh_accounts = []
    for rule_id in pending.affected_rule_ids:
        rule = sns_db.get_watch_rule(rule_id)
        if rule is not None:
            fresh_accounts.append(rule)

    if pending.action == "remove":
        updated = apply_bulk_keyword_filter_remove(
            sns_db, fresh_accounts, pending.keywords
        )
        kw_display = "、".join(pending.keywords)
        if not updated:
            return (
                "沒有需要更新的帳號",
                f"{original_text}\n\n（沒有帳號需要更新，沒人有「{kw_display}」）",
                None,
            )
        toast = f"已修改 {len(updated)} 個帳號"
        new_text = f"{original_text}\n\n✓ 已從 {len(updated)} 個帳號 filter 移除「{kw_display}」"
        return toast, new_text, None

    if pending.action == "set_schedule":
        if pending.schedule_minutes is None:
            return "套用失敗", f"{original_text}\n\n（套用失敗：schedule_minutes 未設定）", None
        updated = apply_bulk_schedule_update(
            sns_db, fresh_accounts, pending.schedule_minutes
        )
        if not updated:
            return (
                "沒有需要更新的帳號",
                f"{original_text}\n\n（沒有帳號需要更新，全部已經是 {pending.schedule_minutes} 分鐘）",
                None,
            )
        toast = f"已修改 {len(updated)} 個帳號"
        new_text = f"{original_text}\n\n✓ 已把 {len(updated)} 個帳號的 schedule 改成 {pending.schedule_minutes} 分鐘"
        return toast, new_text, None

    # Default / "add"
    updated = apply_bulk_keyword_filter_add(
        sns_db, fresh_accounts, pending.keywords
    )

    kw_display = "、".join(pending.keywords)
    if not updated:
        return (
            "沒有需要更新的帳號",
            f"{original_text}\n\n（沒有帳號需要更新，全部已經有「{kw_display}」）",
            None,
        )
    toast = f"已修改 {len(updated)} 個帳號"
    new_text = f"{original_text}\n\n✓ 已修改 {len(updated)} 個帳號 filter 加上「{kw_display}」"
    return toast, new_text, None
