"""Autonomous SNS account discovery — let the bot find TCG-related X / Twitter
accounts on the public web, vet them with the local LLM, and `save_watch_rule`
the ones that look genuine.

Replaces the original deny-list design. Every newly-added rule comes with a
`domains` tag (LLM-supplied, must intersect TCG_DOMAINS), so cross-topic
agents that later share the same SNS DB are automatically protected.

Safety net: confidence floor, intersection with TCG_DOMAINS, cap on adds per
run, and a Telegram notification so the user sees every auto-addition and
can `/snsdelete @X` or `/snsadd @X domain[...]` to override.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from sns_monitor.models import (
    RECOMMENDED_DOMAINS,
    TCG_DOMAINS,
    AccountWatch,
    normalize_domains,
)
from sns_monitor.storage import SnsDatabase

logger = logging.getLogger(__name__)


DEFAULT_DISCOVERY_QUERIES: tuple[str, ...] = (
    "site:twitter.com ポケモンカード 抽選",
    "site:twitter.com 遊戯王 新弾",
    "site:twitter.com Weiss Schwarz",
    "site:x.com Pokemon TCG restock Japan",
)


_HANDLE_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter|x)\.com/(?!status\b|search\b|i/|hashtag/|home\b|explore\b|notifications\b|messages\b)([A-Za-z0-9_]{2,15})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _DiscoveryCandidate:
    handle: str
    title: str
    snippet: str
    url: str


DEFAULT_MIN_CONFIDENCE: float = 0.88
DEFAULT_ACTIONABLE_FLOOR: float = 0.75


def discover_tcg_sns_accounts(
    *,
    sns_db: SnsDatabase,
    search_fn,
    llm_fn,
    telegram_notify_fn=None,
    chat_id: str = "",
    queries: Sequence[str] = DEFAULT_DISCOVERY_QUERIES,
    max_new_per_run: int = 2,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    actionable_floor: float = DEFAULT_ACTIONABLE_FLOOR,
    results_per_query: int = 6,
) -> list[AccountWatch]:
    """Run one discovery pass. Returns the list of newly added rules.

    Two LLM-supplied gates must clear before an account is auto-added:

    1. ``is_tcg`` AND ``confidence >= min_confidence`` — the account is
       genuinely TCG-related and the model is sure.
    2. ``actionable_for_investing >= max(actionable_floor, per-domain
       trust threshold)`` — the account posts content the user can
       actually act on (抽選 / restock / 二手市場 / 漏網好物), not generic
       hobby chatter. The per-domain threshold ratchets UP on rejections
       (``record_auto_discovery_feedback`` polarity='negative') and never
       loosens, so noisy domains tighten themselves over time.

    Parameters
    ----------
    sns_db : SnsDatabase
        Used to skip handles that are already followed and to save new rules.
    search_fn : callable(query, limit=N) -> Sequence[WebSearchResult]
        e.g. `search_duckduckgo`.
    llm_fn : callable(prompt) -> str
        Returns the LLM's JSON response for a single relevance probe.
    telegram_notify_fn : callable(text) | None
        Notifies the user about each auto-added handle. Optional.
    chat_id : str
        Stored on each new AccountWatch so the SNS monitor can notify the
        user when new tweets land. Defaults to "" if unknown.
    """
    if not queries:
        return []
    existing_handles = {
        rule.screen_name.lower()
        for rule in sns_db.list_watch_rules()
        if isinstance(rule, AccountWatch) and rule.screen_name
    }
    # Exclude handles the user already deleted — avoids re-adding rejected accounts.
    try:
        rejected_handles = sns_db.list_rejected_handles(days=90)
    except Exception:
        logger.warning("SnsAccountAutoDiscovery: could not load rejected handles; proceeding without exclusion")
        rejected_handles = frozenset()

    seen_in_run: set[str] = set()
    candidates: list[_DiscoveryCandidate] = []
    for query in queries:
        try:
            results = search_fn(query, max_results=results_per_query)
        except Exception:
            logger.exception("SnsAccountAutoDiscovery search failed query=%s", query)
            continue
        for result in results:
            for handle in _HANDLE_RE.findall(result.url or ""):
                key = handle.lower()
                if key in existing_handles or key in seen_in_run:
                    continue
                if key in rejected_handles:
                    logger.info(
                        "SnsAccountAutoDiscovery skipped @%s — previously rejected by user", handle
                    )
                    continue
                seen_in_run.add(key)
                candidates.append(
                    _DiscoveryCandidate(
                        handle=handle,
                        title=result.title or "",
                        snippet=result.snippet or "",
                        url=result.url or "",
                    )
                )

    if not candidates:
        logger.info("SnsAccountAutoDiscovery found no new handles to evaluate")
        return []

    added: list[AccountWatch] = []
    for candidate in candidates:
        try:
            verdict = _classify_candidate(candidate, llm_fn=llm_fn)
        except Exception:
            logger.exception(
                "SnsAccountAutoDiscovery LLM probe failed handle=%s",
                candidate.handle,
            )
            continue
        if not verdict.get("is_tcg"):
            logger.info(
                "SnsAccountAutoDiscovery rejected handle=%s reason=%s",
                candidate.handle,
                verdict.get("reason", "(no reason)"),
            )
            continue
        confidence = verdict.get("confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            confidence_value = 0.0
        if confidence_value < min_confidence:
            logger.info(
                "SnsAccountAutoDiscovery skipped handle=%s — below confidence floor (%.2f < %.2f)",
                candidate.handle,
                confidence_value,
                min_confidence,
            )
            continue
        domains = normalize_domains(verdict.get("domains"))
        if not (set(domains) & TCG_DOMAINS):
            logger.info(
                "SnsAccountAutoDiscovery skipped handle=%s — domains %s do not intersect TCG set",
                candidate.handle,
                domains,
            )
            continue
        # Second gate: actionable-for-investing. Per-domain trust ratchets
        # this threshold up on past rejections — a noisy domain quietly
        # tightens itself without manual config tuning.
        actionable_raw = verdict.get("actionable_for_investing")
        try:
            actionable_score = float(actionable_raw) if actionable_raw is not None else 0.0
        except (TypeError, ValueError):
            actionable_score = 0.0
        try:
            domain_threshold = sns_db.effective_actionable_threshold(
                domains, default=actionable_floor,
            )
        except Exception:
            logger.exception(
                "SnsAccountAutoDiscovery: failed to read per-domain threshold; falling back to floor"
            )
            domain_threshold = actionable_floor
        effective_threshold = max(actionable_floor, domain_threshold)
        if actionable_score < effective_threshold:
            logger.info(
                "SnsAccountAutoDiscovery skipped handle=%s — actionable score %.2f below threshold %.2f (floor=%.2f, per-domain=%.2f)",
                candidate.handle,
                actionable_score,
                effective_threshold,
                actionable_floor,
                domain_threshold,
            )
            continue
        # Rule_id is hashed from the legacy sentinel to keep IDs stable across
        # the migration — pre-existing auto_discovery rule_ids already exist in
        # users' DBs and we don't want to orphan them.
        rule_id = SnsDatabase._watch_rule_id("account", candidate.handle, source="auto_discovery")
        rule = AccountWatch(
            rule_id=rule_id,
            screen_name=candidate.handle,
            user_id=None,
            label=f"@{candidate.handle}",
            include_keywords=(),
            domains=domains,
            enabled=True,
            schedule_minutes=15,
            chat_id=chat_id,
            last_checked_at=None,
            source="x",  # discovery currently only mines twitter.com / x.com handles
            is_auto_discovered=True,
        )
        sns_db.save_watch_rule(rule)
        added.append(rule)
        existing_handles.add(candidate.handle.lower())
        logger.info(
            "SnsAccountAutoDiscovery added @%s domains=%s confidence=%.2f reason=%s",
            candidate.handle,
            list(domains),
            confidence_value,
            verdict.get("reason", ""),
        )
        if telegram_notify_fn is not None:
            try:
                account_url = f"https://x.com/{candidate.handle}"
                lines = [
                    f"🔎 自動加入追蹤 @{candidate.handle}",
                    f"帳號：{account_url}",
                    f"領域：{', '.join(domains)}",
                    f"信心 {confidence_value:.2f}（投資價值 {actionable_score:.2f}）",
                ]
                if verdict.get("reason"):
                    lines.append(f"原因：{verdict['reason']}")
                if candidate.url and candidate.url != account_url:
                    lines.append(f"觸發來源：{candidate.url}")
                reply_markup = {
                    "inline_keyboard": [[
                        {
                            "text": "👍 對投資有幫助",
                            "callback_data": f"snsaddok:{candidate.handle}",
                        },
                        {
                            "text": "❌ 沒幫助/雜訊",
                            "callback_data": f"snsdel:{candidate.handle}",
                        },
                    ]]
                }
                telegram_notify_fn("\n".join(lines), reply_markup=reply_markup)
            except Exception:
                logger.exception("SnsAccountAutoDiscovery Telegram notify failed handle=@%s", candidate.handle)
        if len(added) >= max_new_per_run:
            break

    return added


def _classify_candidate(candidate: _DiscoveryCandidate, *, llm_fn) -> dict:
    prompt = (
        "你在幫使用者篩選值得追蹤的 X (Twitter) 帳號，目標是「對投資 / 抽選操作真的有幫助」的訊號源，而不是泛泛的 TCG 同好。\n\n"
        f"handle: @{candidate.handle}\n"
        f"網頁 title: {candidate.title}\n"
        f"網頁 snippet: {candidate.snippet}\n"
        f"網頁 URL: {candidate.url}\n\n"
        "請判斷兩個獨立的分數（兩個都過才會被加進追蹤）：\n\n"
        "1. is_tcg + confidence (0-1)：這個帳號真的在發 TCG (寶可夢 / 遊戲王 / Weiss Schwarz / Union Arena) 相關內容嗎？不確定就 is_tcg=false。\n\n"
        "2. actionable_for_investing (0-1)：這個帳號發的內容是否能直接讓使用者「下手」？\n"
        "   高分 (0.75-1.0) — 帳號類型：\n"
        "     • 抽選販售情報 / 補貨時間 / 預訂開搶通知\n"
        "     • 二手市場價格異動 / 未開封 BOX 投資機會\n"
        "     • PSA / BGS 鑑定漏網好物 / 跨地區套利機會\n"
        "     • 拍賣低於行情的物件提示\n"
        "   中分 (0.4-0.7) — 個人玩家但偶爾發買賣情報、店家但只發新貨上架（無折扣 / 套利訊息）\n"
        "   低分 (0.0-0.4) — 帳號類型：\n"
        "     • 對戰心得 / decklist / 玩法分享\n"
        "     • 開箱炫耀 / 卡圖鑑賞 / cosplay\n"
        "     • 自家店面廣告 / 純自己賣東西的轉售\n"
        "     • 純聊天 / 紀錄日常 / 抽到爛卡哀號\n\n"
        "推薦 domain 清單 (請只挑這裡面的)：\n"
        f"{', '.join(RECOMMENDED_DOMAINS)}\n\n"
        "請嚴格回 JSON：\n"
        '{"is_tcg": true/false, "confidence": 0-1, "actionable_for_investing": 0-1, "domains": ["pokemon"], "reason": "一句話說明 actionable 分數的依據"}\n'
        "若你不確定某分數，寧可給低分 — 使用者要的是少而精的訊號源，不是覆蓋率。"
    )
    raw = llm_fn(prompt)
    return _safe_json_loads(raw) or {}


_JSON_FRAGMENT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_json_loads(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        match = _JSON_FRAGMENT_RE.search(raw)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
