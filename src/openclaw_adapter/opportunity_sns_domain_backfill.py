"""One-time-per-rule LLM backfill of `domains` for legacy SNS watch rules.

When the `domains` field was first introduced, existing rules in the SNS
DB had no tags. The TCG opportunity agent reads only rules whose `domains`
intersect `TCG_DOMAINS`, so legacy rules become invisible until labelled.

This module's `backfill_missing_domains` walks the watch_rules table,
picks one untagged enabled rule per call, peeks at a few recent tweets
from that account / keyword, and asks the local Ollama text model to
choose 1–3 tags from `RECOMMENDED_DOMAINS`. The result is saved back via
`save_watch_rule` and a Telegram note goes to the user so they can see /
override the auto-tag (e.g. via `/snsadd @X domain[...]`).

By default the opportunity-agent cron tick calls this with `limit=1`,
so even a 20-rule database is fully tagged within ~20 ticks (~5 hours
on the 15-minute schedule) without any LLM storm.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from sns_monitor.models import (
    RECOMMENDED_DOMAINS,
    AccountWatch,
    KeywordWatch,
    TrendWatch,
    WatchRule,
    normalize_domains,
)

logger = logging.getLogger(__name__)


def backfill_missing_domains(
    *,
    sns_db,
    sns_db_path: str | Path,
    llm_fn,
    telegram_notify_fn=None,
    limit: int = 1,
) -> list[WatchRule]:
    """Backfill `domains` for up to *limit* rules. Returns the rules that
    were updated.

    Parameters
    ----------
    sns_db:
        A `SnsDatabase`-shaped object exposing `list_watch_rules_missing_domains`
        and `save_watch_rule`.
    sns_db_path:
        Path to the SNS SQLite DB (needed to peek at `seen_tweets`).
    llm_fn:
        Callable ``(prompt: str) -> str`` returning the LLM's JSON response.
    telegram_notify_fn:
        Optional callable ``(text: str) -> None``. When provided, a "auto-tagged
        @X domains=[...]" note goes to the user.
    limit:
        Maximum rules to backfill per call. Default 1 so the LLM only runs
        once per opportunity tick.
    """
    pending = sns_db.list_watch_rules_missing_domains(limit=limit)
    updated: list[WatchRule] = []
    for rule in pending:
        try:
            sample_tweets = _peek_recent_tweets_for_rule(sns_db_path, rule)
            domains = _classify_rule_domains(rule, sample_tweets, llm_fn=llm_fn)
        except Exception:
            logger.exception(
                "Domain backfill LLM failed rule_id=%s — leaving rule untouched",
                rule.rule_id,
            )
            continue
        if not domains:
            logger.info(
                "Domain backfill produced no domains rule_id=%s label=%s",
                rule.rule_id,
                rule.label,
            )
            continue
        new_rule = replace(rule, domains=domains)
        sns_db.save_watch_rule(new_rule)
        updated.append(new_rule)
        logger.info(
            "Domain backfill applied rule_id=%s label=%s domains=%s",
            rule.rule_id,
            rule.label,
            list(domains),
        )
        if telegram_notify_fn is not None:
            label = _describe_rule(rule)
            try:
                telegram_notify_fn(
                    f"🏷 自動標記 {label}\n領域：{', '.join(domains)}\n如需修改：/snsadd {label} domain[a,b]"
                )
            except Exception:
                logger.exception("Domain backfill Telegram notify failed rule_id=%s", rule.rule_id)
    return updated


def _peek_recent_tweets_for_rule(
    sns_db_path: str | Path,
    rule: WatchRule,
    *,
    limit: int = 5,
) -> list[str]:
    path = Path(sns_db_path)
    if not path.exists():
        return []
    try:
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT text
                FROM seen_tweets
                WHERE rule_id = ?
                ORDER BY first_seen_at DESC
                LIMIT ?
                """,
                (rule.rule_id, limit),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("Failed to peek seen_tweets for rule_id=%s", rule.rule_id)
        return []
    return [str(row["text"]) for row in rows if row["text"]]


def _classify_rule_domains(
    rule: WatchRule,
    sample_tweets: Sequence[str],
    *,
    llm_fn,
) -> tuple[str, ...]:
    descriptor = _describe_rule(rule)
    posts_block = (
        "\n".join(f"- {t.strip()[:180]}" for t in sample_tweets[:5])
        if sample_tweets
        else "(no recent posts captured)"
    )
    prompt = (
        "你正在替使用者的 SNS 追蹤規則加上 1-3 個領域標籤 (domain tag)。\n"
        f"規則：{descriptor}\n"
        f"最近的相關貼文：\n{posts_block}\n\n"
        "從下列推薦清單中挑選 1-3 個最貼近這個帳號 / 關鍵字的標籤；其他主題就略過：\n"
        f"{', '.join(RECOMMENDED_DOMAINS)}\n\n"
        '請嚴格回 JSON：{"domains": ["pokemon"], "reason": "一句話原因"}\n'
        "如果無法判斷，至少回 {\"domains\": [\"other\"]}。"
    )
    raw = llm_fn(prompt)
    parsed = _safe_json_loads(raw)
    if not isinstance(parsed, dict):
        return ()
    return normalize_domains(parsed.get("domains"))


def _describe_rule(rule: WatchRule) -> str:
    if isinstance(rule, AccountWatch):
        return f"@{rule.screen_name}"
    if isinstance(rule, KeywordWatch):
        return f'keyword:{rule.query}'
    if isinstance(rule, TrendWatch):
        return f"trend:{rule.category}"
    return rule.label or rule.rule_id


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
