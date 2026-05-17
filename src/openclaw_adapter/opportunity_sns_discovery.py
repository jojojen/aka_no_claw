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


def discover_tcg_sns_accounts(
    *,
    sns_db: SnsDatabase,
    search_fn,
    llm_fn,
    telegram_notify_fn=None,
    chat_id: str = "",
    queries: Sequence[str] = DEFAULT_DISCOVERY_QUERIES,
    max_new_per_run: int = 2,
    min_confidence: float = 0.7,
    results_per_query: int = 6,
) -> list[AccountWatch]:
    """Run one discovery pass. Returns the list of newly added rules.

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
        rule_id = SnsDatabase._watch_rule_id("account", candidate.handle)
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
                ]
                if verdict.get("reason"):
                    lines.append(f"原因：{verdict['reason']}")
                if candidate.url and candidate.url != account_url:
                    lines.append(f"觸發來源：{candidate.url}")
                lines.append(f"不滿意可：/snsdelete @{candidate.handle}")
                telegram_notify_fn("\n".join(lines))
            except Exception:
                logger.exception("SnsAccountAutoDiscovery Telegram notify failed handle=@%s", candidate.handle)
        if len(added) >= max_new_per_run:
            break

    return added


def _classify_candidate(candidate: _DiscoveryCandidate, *, llm_fn) -> dict:
    prompt = (
        "你在幫使用者篩選值得追蹤的 X (Twitter) 帳號。判斷下面這個帳號是否真的是 TCG (寶可夢 / 遊戲王 / Weiss Schwarz / Union Arena) 相關內容。\n"
        f"handle: @{candidate.handle}\n"
        f"網頁 title: {candidate.title}\n"
        f"網頁 snippet: {candidate.snippet}\n"
        f"網頁 URL: {candidate.url}\n\n"
        "推薦 domain 清單 (請只挑這裡面的)：\n"
        f"{', '.join(RECOMMENDED_DOMAINS)}\n\n"
        "請嚴格回 JSON：\n"
        '{"is_tcg": true/false, "domains": ["pokemon"], "confidence": 0-1, "reason": "一句話"}\n'
        "is_tcg=true 必須真的看到 TCG 相關內容；不確定就 false。"
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
