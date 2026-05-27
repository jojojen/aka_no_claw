"""Telegram-side dispatcher for the ``/knowledge`` (alias ``/kb``) command.

Subcommands:
  /knowledge add <entity> | <summary>
  /knowledge add <entity> [as <type>] | <summary>
  /knowledge list                 — recent entries
  /knowledge get <entity>         — show one entry
  /knowledge alias <entity> = <alias1>, <alias2>
  /knowledge remove <entity>      — delete an entry

Manual entries are stored with ``origin='manual'`` and ``confidence=1.0`` so
they win over any web-research backfill.
"""

from __future__ import annotations

import logging
from typing import Callable

from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

_VALID_TYPES = ("ip", "product", "set", "creator", "event", "store", "other")


def build_knowledge_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], str]:
    """Return a handler that price_monitor_bot's ``/knowledge`` dispatcher
    calls. Late-imports KnowledgeDatabase so this module stays cheap to load
    even when the knowledge feature isn't being used in a given session."""

    from .knowledge_db import KnowledgeDatabase

    db_path = settings.knowledge_db_path
    db = KnowledgeDatabase(db_path)  # bootstraps schema on first call

    def handler(raw: str, chat_id: str) -> str:
        text = (raw or "").strip()
        if not text:
            return _usage_text()
        head, _, rest = text.partition(" ")
        action = head.lower().strip()
        rest = rest.strip()
        try:
            if action == "add":
                return _do_add(db, rest)
            if action == "list":
                return _do_list(db, rest)
            if action == "get":
                return _do_get(db, rest)
            if action == "alias":
                return _do_alias(db, rest)
            if action == "remove" or action == "delete":
                return _do_remove(db, rest)
        except Exception as exc:
            logger.exception("knowledge_command: action=%s failed", action)
            return f"知識庫指令失敗：{exc}"
        return _usage_text()

    return handler


def _usage_text() -> str:
    return (
        "用法：\n"
        "  /knowledge add <entity> | <summary>\n"
        "  /knowledge add <entity> as <ip|product|set|creator|event|store|other> | <summary>\n"
        "  /knowledge list\n"
        "  /knowledge get <entity>\n"
        "  /knowledge alias <entity> = <alias1>, <alias2>\n"
        "  /knowledge remove <entity>"
    )


def _do_add(db, rest: str) -> str:
    if "|" not in rest:
        return "請用 `|` 分隔 entity 與 summary。例如：/knowledge add pjsk | プロセカ 是 SEGA 的節奏音遊…"
    head, summary = rest.split("|", 1)
    head = head.strip()
    summary = summary.strip()
    if not head or not summary:
        return "請同時提供 entity 名稱與 summary 內容。"
    entity_type = "other"
    if " as " in head.lower():
        entity_part, _, type_part = _split_as(head)
        head = entity_part.strip()
        candidate_type = type_part.strip().lower()
        if candidate_type in _VALID_TYPES:
            entity_type = candidate_type
        else:
            return (
                f"未知的 entity_type：{candidate_type}（合法值：{', '.join(_VALID_TYPES)}）"
            )
    if not head:
        return "請提供 entity 名稱。"
    try:
        db.upsert_entry(
            entity_canonical=head,
            entity_type=entity_type,
            summary=summary,
            source_urls=(),
            confidence=1.0,
            origin="manual",
            aliases=(),
        )
    except Exception as exc:
        return f"寫入失敗：{exc}"
    return f"✅ 已記入知識庫：{head} (type={entity_type}, {len(summary)} 字)"


def _split_as(head: str) -> tuple[str, str, str]:
    """Case-insensitive split on ' as ' once."""
    lowered = head.lower()
    idx = lowered.find(" as ")
    return head[:idx], " as ", head[idx + 4 :]


def _do_list(db, rest: str) -> str:
    limit = 20
    if rest:
        try:
            limit = max(1, min(50, int(rest)))
        except ValueError:
            return "limit 必須是數字，例如：/knowledge list 30"
    entries = db.recent_entries(limit=limit)
    if not entries:
        return "知識庫尚無條目。"
    lines = [f"📚 最近 {len(entries)} 條知識庫條目："]
    for e in entries:
        preview = (e.summary or "").strip().splitlines()[0][:80]
        lines.append(
            f"• {e.entity_canonical} ({e.entity_type}, "
            f"conf={e.confidence:.2f}, src={e.origin}) — {preview}"
        )
    return "\n".join(lines)


def _do_get(db, rest: str) -> str:
    entity = rest.strip()
    if not entity:
        return "請提供 entity 名稱。例如：/knowledge get pjsk"
    entry = db.get_entry(entity)
    if entry is None:
        canonical = db.lookup_canonical(entity)
        if canonical is None:
            return f"找不到 entity「{entity}」（也不在 alias 表）。"
        entry = db.get_entry(canonical)
        if entry is None:
            return f"找不到 entity「{entity}」。"
    return (
        f"📖 {entry.entity_canonical} ({entry.entity_type})\n"
        f"confidence={entry.confidence:.2f}  origin={entry.origin}\n"
        f"updated_at={entry.updated_at}\n\n{entry.summary}"
    )


def _do_alias(db, rest: str) -> str:
    if "=" not in rest:
        return "用法：/knowledge alias <entity> = <alias1>, <alias2>"
    entity, _, alias_str = rest.partition("=")
    entity = entity.strip()
    aliases = [a.strip() for a in alias_str.split(",") if a.strip()]
    if not entity or not aliases:
        return "請提供 entity 與至少一個 alias。"
    if db.get_entry(entity) is None:
        return f"entity「{entity}」尚未在知識庫中，請先 /knowledge add 建立。"
    added = 0
    for alias in aliases:
        if db.add_alias(alias, entity):
            added += 1
    return f"✅ 已為 {entity} 加入 {added} 個 alias。"


def _do_remove(db, rest: str) -> str:
    entity = rest.strip()
    if not entity:
        return "請提供 entity 名稱。"
    # KnowledgeDatabase.delete_entry isn't a hard requirement of the plan; fall
    # back to a direct DELETE so the command still works without API changes.
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_entries WHERE entity_canonical = ?",
                (entity.strip().lower(),),
            )
            removed = cur.rowcount
            conn.commit()
    except Exception as exc:
        return f"刪除失敗：{exc}"
    return "✅ 已刪除。" if removed else f"找不到 entity「{entity}」。"
