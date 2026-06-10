"""Telegram command/callback handlers for /hunt and opportunity inline buttons.

All write operations go through OpportunityInbox (single-writer-per-file pattern).
Read operations (status display, list view) query opportunities.sqlite3 directly.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Callable, Sequence

from price_monitor_bot.list_view import (
    LIST_VIEW_MODE_READ,
    ListRow,
    build_list_view,
)

from .opportunity_agent import (
    _resolve_candidate_selector,  # noqa: PLC2701
    _row_string_list,              # noqa: PLC2701
    format_opportunity_status,
    list_opportunity_targets,
)
from .opportunity_store import OpportunityStore

if TYPE_CHECKING:
    from assistant_runtime import AssistantSettings
    from .opportunity_inbox import OpportunityInbox

logger = logging.getLogger(__name__)

# ── Action-keyword helpers (ported from price_monitor_bot/bot.py) ─────────────

_HUNT_PIN_KEYWORDS: tuple[str, ...] = (
    "pin",
    "target",
    "watch",
    "track",
    "加入目標",
    "鎖定",
    "盯",
    "釘",
)

_HUNT_UNPIN_KEYWORDS: tuple[str, ...] = (
    "unpin",
    "untarget",
    "unwatch",
    "untrack",
    "取消鎖定",
    "解除目標",
    "解鎖",
)


def _action_keyword_match(raw: str, keywords: tuple[str, ...]) -> str | None:
    stripped = raw.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for keyword in keywords:
        key_lower = keyword.lower()
        if lowered == key_lower or lowered.startswith(f"{key_lower} "):
            return keyword
        if stripped == keyword or stripped.startswith(f"{keyword} "):
            return keyword
    return None


def _strip_action_keyword(raw: str, keyword: str) -> str:
    stripped = raw.strip()
    lowered_stripped = stripped.lower()
    key_lower = keyword.lower()
    if lowered_stripped == key_lower or stripped == keyword:
        return ""
    if lowered_stripped.startswith(f"{key_lower} "):
        return stripped[len(keyword):].strip()
    if stripped.startswith(f"{keyword} "):
        return stripped[len(keyword):].strip()
    return stripped


def _is_hunt_pin_action(raw: str) -> bool:
    if _action_keyword_match(raw, _HUNT_UNPIN_KEYWORDS) is not None:
        return False
    return _action_keyword_match(raw, _HUNT_PIN_KEYWORDS) is not None


def _extract_hunt_pin_target(raw: str) -> str:
    matched = _action_keyword_match(raw, _HUNT_PIN_KEYWORDS)
    if matched is None:
        return ""
    return _strip_action_keyword(raw, matched)


def _is_hunt_unpin_action(raw: str) -> bool:
    return _action_keyword_match(raw, _HUNT_UNPIN_KEYWORDS) is not None


def _extract_hunt_unpin_target(raw: str) -> str:
    matched = _action_keyword_match(raw, _HUNT_UNPIN_KEYWORDS)
    if matched is None:
        return ""
    return _strip_action_keyword(raw, matched)


def _is_hunt_remove_action(raw: str) -> bool:
    lowered = raw.strip().lower()
    if not lowered:
        return False
    return any(
        lowered == keyword or lowered.startswith(f"{keyword} ")
        for keyword in (
            "remove", "delete", "dismiss", "hide", "ignore", "drop",
            "not interested", "no interest",
            "移除", "刪除", "删除", "不要", "不感興趣", "不感兴趣",
            "沒興趣", "没兴趣", "外して", "削除",
        )
    )


def _extract_hunt_remove_target(raw: str) -> str:
    target = raw.strip()
    for phrase in (
        "not interested in", "not interested", "no interest in", "no interest",
        "remove", "delete", "dismiss", "hide", "ignore", "drop", "target",
        "candidate", "opportunity",
        "移除", "刪除", "删除", "不要", "不感興趣", "不感兴趣",
        "沒興趣", "没兴趣", "外して", "削除", "目標", "目标", "候選", "候选",
    ):
        target = re.sub(re.escape(phrase), " ", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:第)?\s*(\d{1,2})\s*(?:個|个|項|项|筆|笔|番)?$", r"\1", target.strip())
    target = re.sub(r"[，、。！？!?：:；;]", " ", target)
    return " ".join(target.split()).strip()


def _split_alias_names(tail: str) -> list[str]:
    standardized = tail.replace("，", ",").replace("、", ",").strip()
    if not standardized:
        return []
    if "," in standardized:
        return [part.strip() for part in standardized.split(",") if part.strip()]
    return [standardized]


# ── List view helpers ─────────────────────────────────────────────────────────

def _build_huntlist_view(
    settings: "AssistantSettings",
    *,
    page: int = 0,
    mode: str = LIST_VIEW_MODE_READ,
) -> tuple[str, dict | None, int]:
    """Build the paginated /hunt candidate list view. Returns (text, markup, page)."""
    try:
        candidates = list(list_opportunity_targets(settings))
    except Exception as exc:
        logger.exception("Hunt list provider failed")
        return f"列表失敗：{exc}", None, 0

    items: list[ListRow] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            continue
        game = str(candidate.get("game") or "?")
        product_type = str(candidate.get("product_type") or "other")
        title = str(candidate.get("title") or "(no title)")
        heat = candidate.get("heat_score")
        heat_text = f"{float(heat):.0f}" if heat is not None else "?"
        search_query = str(candidate.get("search_query") or "")
        text_block = (
            f"[{game} / {product_type}] {title}\n"
            f"  heat={heat_text}  search: {search_query}"
        )
        short = f"[{game}] {title[:18]}"
        items.append(ListRow(id=candidate_id, text=text_block, short_label=short))

    return build_list_view(
        list_kind="hl",
        items=items,
        page=page,
        mode=mode,
        list_title="📋 Opportunity 候選",
        empty_message="目前沒有 Opportunity 候選。等下一輪 agent tick 收集到目標後再看。",
    )


# ── Write-path helpers (push to inbox) ───────────────────────────────────────

def _handle_dismiss(
    settings: "AssistantSettings",
    raw: str,
    inbox: "OpportunityInbox | None",
) -> str:
    target = _extract_hunt_remove_target(raw)
    if not target:
        return "請提供要移除的目標，例如：/hunt remove 2 或 /hunt remove Umbreon ex SAR"

    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    candidates = store.list_recent_candidates(limit=30)
    if not candidates:
        return "目前沒有可移除的機會目標。"

    resolved = _resolve_candidate_selector(candidates, target)
    if isinstance(resolved, str):
        return resolved

    candidate_id = str(resolved["candidate_id"])
    if inbox is not None:
        inbox.push("dismiss_candidate", {"candidate_id": candidate_id})
        return (
            "已排入移除隊列，稍後生效\n"
            f"目標：[{resolved['game']}] {resolved['title']}"
        )
    # Fallback: direct write (single-process mode / tests without agent)
    from .opportunity_agent import dismiss_opportunity_target
    return dismiss_opportunity_target(settings, target)


def _handle_pin(
    settings: "AssistantSettings",
    raw: str,
    inbox: "OpportunityInbox | None",
) -> str:
    name = _extract_hunt_pin_target(raw)
    if not name:
        return "請提供要釘為目標的商品名，例如：/hunt pin アビスアイ box"

    if inbox is not None:
        inbox.push("pin_by_name", {"name": name})
        return f"已排入目標釘選隊列：{name}（稍後生效）"

    from .opportunity_agent import pin_opportunity_target
    return pin_opportunity_target(settings, name)


def _handle_unpin(
    settings: "AssistantSettings",
    raw: str,
    inbox: "OpportunityInbox | None",
) -> str:
    selector = _extract_hunt_unpin_target(raw)
    if not selector:
        return "請提供要從目標清單移除的編號或名稱，例如：/hunt unpin 1"

    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    candidates = store.list_recent_candidates(limit=30)
    if not candidates:
        return "目前沒有可調整的目標。"

    resolved = _resolve_candidate_selector(candidates, selector)
    if isinstance(resolved, str):
        return resolved

    candidate_id = str(resolved["candidate_id"])
    if inbox is not None:
        inbox.push("set_is_target", {"candidate_id": candidate_id, "is_target": False})
        return (
            "已排入取消目標隊列，稍後生效\n"
            f"目標：[{resolved['game']}] {resolved['title']}\n"
            "若要完全移除請改用 /hunt remove。"
        )

    from .opportunity_agent import unpin_opportunity_target
    return unpin_opportunity_target(settings, selector)


def _handle_hunt_alias(
    settings: "AssistantSettings",
    raw: str,
    inbox: "OpportunityInbox | None",
) -> str | None:
    match = re.match(
        r"^\s*(?P<kind>alias|aliases|related|related_keywords)\s+(?P<rest>.+)$",
        raw,
        re.IGNORECASE,
    )
    if match is None:
        return None
    kind_raw = match.group("kind").lower()
    kind = "aliases" if kind_raw.startswith("alias") else "related"
    rest = match.group("rest").strip()
    action_match = re.search(r"\b(add|remove|rm|del|delete)\b", rest, re.IGNORECASE)
    if action_match is None:
        return f"請指定 add 或 remove。例如：/hunt {kind_raw} <編號或id> add 別名A"
    selector = rest[: action_match.start()].strip()
    if not selector:
        return f"請提供候選編號或 id。例如：/hunt {kind_raw} 2 add 別名A"
    action_word = action_match.group(1).lower()
    action = "remove" if action_word in {"remove", "rm", "del", "delete"} else "add"
    tail = rest[action_match.end():].strip()
    if not tail:
        return f"請提供至少一個名稱。例如：/hunt {kind_raw} {selector} {action} ピカチュウex SAR"
    names = _split_alias_names(tail)
    if not names:
        return "請提供至少一個有效的名稱。"

    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()
    candidates = store.list_recent_candidates(limit=30)
    if not candidates:
        return "目前沒有可編輯的候選目標。"

    resolved = _resolve_candidate_selector(candidates, selector)
    if isinstance(resolved, str):
        return resolved

    candidate_id = str(resolved["candidate_id"])
    label_kind = "別名" if kind == "aliases" else "相關關鍵字"

    if inbox is not None:
        inbox.push("update_string_list", {
            "candidate_id": candidate_id,
            "kind": kind,
            "action": action,
            "names": list(names),
        })
        return (
            f"已排入{label_kind}更新隊列，稍後生效\n"
            f"目標：[{resolved['game']}] {resolved['title']}\n"
            f"action={action}, names={', '.join(names[:5])}"
        )

    from .opportunity_agent import update_opportunity_string_list
    return update_opportunity_string_list(settings, selector, kind=kind, action=action, names=names)


# ── Public builders ───────────────────────────────────────────────────────────

def build_hunt_handler(
    settings: "AssistantSettings",
    opportunity_inbox: "OpportunityInbox | None" = None,
) -> Callable[[str, str], object]:
    """Return (remainder, chat_id) -> str | tuple[str, dict|None] for /hunt."""

    def handler(remainder: str, chat_id: str) -> object:
        action = remainder.strip().lower()
        if action in {"", "list", "candidates", "targets", "目標", "候選"}:
            text, markup, _ = _build_huntlist_view(settings)
            return text, markup
        if action in {"status"}:
            return format_opportunity_status(settings)
        if _is_hunt_remove_action(remainder):
            return _handle_dismiss(settings, remainder, opportunity_inbox)
        if _is_hunt_unpin_action(remainder):
            return _handle_unpin(settings, remainder, opportunity_inbox)
        if _is_hunt_pin_action(remainder):
            return _handle_pin(settings, remainder, opportunity_inbox)
        alias_reply = _handle_hunt_alias(settings, remainder, opportunity_inbox)
        if alias_reply is not None:
            return alias_reply
        return (
            "可用格式：\n"
            "  /hunt status\n"
            "  /hunt pin <商品名>           ← 🎯 主動加入目標清單\n"
            "  /hunt unpin <編號或名稱>      ← 從目標清單移除（保留 candidate）\n"
            "  /hunt remove <編號或名稱>    ← 永久封殺該 candidate\n"
            "  /hunt alias <編號或id> add 別名A, 別名B\n"
            "  /hunt alias <編號或id> remove 別名A\n"
            "  /hunt related <編號或id> add 關鍵字"
        )

    return handler


def build_hunt_callback_handler(
    settings: "AssistantSettings",
    opportunity_inbox: "OpportunityInbox | None" = None,
) -> Callable[[str, str, str], tuple[object, object, object]]:
    """Return (payload, original_text, chat_id) -> (toast, new_text, new_markup) for oppfb."""

    def handler(payload: str, original_text: str, chat_id: str) -> tuple[object, object, object]:
        kind, _, rec_id = payload.partition(":")
        if kind not in {"up", "down", "bought"} or not rec_id:
            return "未知回饋", None, None

        if opportunity_inbox is not None:
            opportunity_inbox.push(
                "record_feedback",
                {"recommendation_id": rec_id, "kind": kind},
                chat_id=chat_id,
            )
            if kind == "up":
                toast = "✅ 已記錄 👍"
            elif kind == "bought":
                toast = "✅ 已記錄 💰"
            else:
                toast = "✅ 已標記不感興趣（24h cooldown）"
            return toast, f"{original_text}\n\n{toast}", None
        else:
            # Fallback: direct write when no inbox (single-process / tests)
            from .opportunity_feedback import record_opportunity_feedback
            try:
                result = record_opportunity_feedback(
                    recommendation_id=rec_id, kind=kind, settings=settings
                )
            except Exception:
                logger.exception("oppfb direct write failed rec_id=%s kind=%s", rec_id, kind)
                return "回饋寫入失敗，請看 log", None, None
            if result.get("status") != "ok":
                toast = f"記錄失敗：{result.get('reason', 'unknown')}"
                return toast, None, None
            side_effects = list(result.get("side_effects") or ())
            if kind == "up":
                toast = "✓ 已記錄 👍" + (" + 升級為目標" if "promoted_to_target" in side_effects else "")
            elif kind == "bought":
                toast = "✓ 已記錄 💰" + (" + 升級為目標" if "promoted_to_target" in side_effects else "")
            else:
                if "auto_dismissed" in side_effects:
                    toast = "✓ 已標記不感興趣（累計過閾值，自動 dismiss）"
                else:
                    toast = "✓ 已標記不感興趣（24h cooldown）"
            return toast, f"{original_text}\n\n{toast}", None

    return handler


def build_huntlist_view_fn(
    settings: "AssistantSettings",
) -> Callable[..., tuple[str, dict | None, int]]:
    """Return a view function for the 'hl' view_handlers registry."""

    def view_fn(*, page: int = 0, mode: str = LIST_VIEW_MODE_READ) -> tuple[str, dict | None, int]:
        return _build_huntlist_view(settings, page=page, mode=mode)

    return view_fn


def build_huntlist_item_deleter(
    settings: "AssistantSettings",
    opportunity_inbox: "OpportunityInbox | None" = None,
) -> tuple[Callable[[str], bool], str]:
    """Return (deleter_fn, label) for the 'hl' item_deleter_handlers registry."""

    def deleter(candidate_id: str) -> bool:
        if opportunity_inbox is not None:
            try:
                opportunity_inbox.push("dismiss_candidate", {"candidate_id": candidate_id})
                return True
            except Exception:
                logger.exception("huntlist delete via inbox failed candidate_id=%s", candidate_id)
                return False
        # Fallback: direct write
        try:
            store = OpportunityStore(settings.opportunity_db_path)
            store.bootstrap()
            return store.dismiss_candidate(candidate_id)
        except Exception:
            logger.exception("huntlist direct delete failed candidate_id=%s", candidate_id)
            return False

    return deleter, "Opportunity 候選"
