"""Telegram-side dispatcher for the ``/knowledge`` (alias ``/kb``) command.

Subcommands:
  /knowledge market               — paginated RAG market knowledge view
  /knowledge coding               — paginated coding knowledge view
  /knowledge add <entity> | <summary>
  /knowledge add <entity> [as <type>] | <summary>
  /knowledge list                 — recent entries (text)
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
    knowledge_inbox=None,
) -> Callable[[str, str], "str | tuple[str, dict | None]"]:
    """Return the /knowledge command handler for the registry.

    Returns a string for most sub-commands, or a ``(text, markup)`` tuple
    for the ``market`` and ``coding`` paginated views.

    When ``knowledge_inbox`` is provided, write operations (add/alias/remove) go
    through the inbox (sns_monitor service is the sole writer to knowledge.sqlite3).
    Read operations (list/get/market/coding) always use the DB directly.
    """

    from .knowledge_db import KnowledgeDatabase

    db_path = settings.knowledge_db_path
    db = KnowledgeDatabase(db_path)
    market_view = build_knowledge_market_view_fn(settings)
    coding_view = build_knowledge_coding_view_fn(settings)

    def handler(raw: str, chat_id: str) -> "str | tuple[str, dict | None]":
        text = (raw or "").strip()
        if not text:
            return _usage_text()
        head, _, rest = text.partition(" ")
        action = head.lower().strip()
        rest = rest.strip()
        try:
            if action == "market":
                body, markup, _ = market_view(page=0)
                return body, markup
            if action == "coding":
                body, markup, _ = coding_view(page=0)
                return body, markup
            if action == "add":
                return _do_add(db, rest, knowledge_inbox=knowledge_inbox)
            if action == "list":
                return _do_list(db, rest)
            if action == "get":
                return _do_get(db, rest)
            if action == "alias":
                return _do_alias(db, rest, knowledge_inbox=knowledge_inbox)
            if action in ("remove", "delete"):
                return _do_remove(db, rest, knowledge_inbox=knowledge_inbox)
        except Exception as exc:
            logger.exception("knowledge_command: action=%s failed", action)
            return f"知識庫指令失敗：{exc}"
        return _usage_text()

    return handler


def build_knowledge_market_view_fn(
    settings: AssistantSettings,
) -> Callable[..., "tuple[str, dict | None, int]"]:
    """Return a callable ``(*, page=0, mode='r') -> (text, markup, clamped_page)``
    for the market knowledge paginated list view (list_kind ``km``)."""

    from .knowledge_db import KnowledgeDatabase
    from price_monitor_bot.list_view import LIST_VIEW_MODE_READ, ListRow, build_list_view

    db_path = settings.knowledge_db_path

    def view_fn(*, page: int = 0, mode: str = LIST_VIEW_MODE_READ):
        db = KnowledgeDatabase(db_path)
        entries = db.recent_entries(limit=200)
        items = []
        for e in entries:
            preview = (e.summary or "").strip().replace("\n", " ")[:80]
            items.append(ListRow(
                id=e.entry_id,
                text=f"• {e.entity_canonical} ({e.entity_type}, {e.confidence:.0%})\n  {preview}",
                short_label=e.entity_canonical,
            ))
        return build_list_view(
            list_kind="km",
            items=items,
            page=page,
            mode=mode,
            list_title="📚 RAG 市場知識",
            empty_message="知識庫尚無市場條目。",
        )

    return view_fn


def build_knowledge_coding_view_fn(
    settings: AssistantSettings,
) -> Callable[..., "tuple[str, dict | None, int]"]:
    """Return a callable ``(*, page=0, mode='r') -> (text, markup, clamped_page)``
    for the coding knowledge paginated list view (list_kind ``kc``)."""

    from .knowledge_db import KnowledgeDatabase
    from price_monitor_bot.list_view import LIST_VIEW_MODE_READ, ListRow, build_list_view

    db_path = settings.knowledge_db_path

    def view_fn(*, page: int = 0, mode: str = LIST_VIEW_MODE_READ):
        db = KnowledgeDatabase(db_path)
        rows = db.all_codegen_knowledge()
        items = []
        for r in rows:
            preview = (r.technique or "").strip().replace("\n", " ")[:80]
            items.append(ListRow(
                id=r.knowledge_id,
                text=f"• [{r.category}] {r.title} ({r.confidence:.0%})\n  {preview}",
                short_label=r.title,
            ))
        return build_list_view(
            list_kind="kc",
            items=items,
            page=page,
            mode=mode,
            list_title="🧠 龍蝦 Coding 知識",
            empty_message="尚無 coding 知識條目。",
        )

    return view_fn


def build_knowledge_item_deleters(
    settings: AssistantSettings,
) -> "dict[str, tuple[Callable[[str], bool], str]]":
    """Return deleter registry entries for ``km`` and ``kc`` list kinds."""

    from .knowledge_db import KnowledgeDatabase

    db_path = settings.knowledge_db_path

    def delete_market_entry(entry_id: str) -> bool:
        try:
            return KnowledgeDatabase(db_path).delete_entry(entry_id)
        except Exception:
            logger.exception("knowledge market delete failed entry_id=%s", entry_id)
            return False

    def delete_coding_entry(knowledge_id: str) -> bool:
        try:
            return KnowledgeDatabase(db_path).delete_codegen(knowledge_id)
        except Exception:
            logger.exception("knowledge coding delete failed knowledge_id=%s", knowledge_id)
            return False

    return {
        "km": (delete_market_entry, "市場知識條目"),
        "kc": (delete_coding_entry, "Coding 知識條目"),
    }


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


def _do_add(db, rest: str, knowledge_inbox=None) -> str:
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
        if knowledge_inbox is not None:
            knowledge_inbox.push("save_entry", {
                "entity_canonical": head,
                "entity_type": entity_type,
                "summary": summary,
                "confidence": 1.0,
                "origin": "manual",
            })
        else:
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


def _do_alias(db, rest: str, knowledge_inbox=None) -> str:
    if "=" not in rest:
        return "用法：/knowledge alias <entity> = <alias1>, <alias2>"
    entity, _, alias_str = rest.partition("=")
    entity = entity.strip()
    aliases = [a.strip() for a in alias_str.split(",") if a.strip()]
    if not entity or not aliases:
        return "請提供 entity 與至少一個 alias。"
    if db.get_entry(entity) is None:
        return f"entity「{entity}」尚未在知識庫中，請先 /knowledge add 建立。"
    try:
        if knowledge_inbox is not None:
            for alias in aliases:
                knowledge_inbox.push("alias_entry", {"canonical": entity, "alias": alias})
            return f"✅ 已為 {entity} 排入 {len(aliases)} 個 alias。"
        added = 0
        for alias in aliases:
            if db.add_alias(alias, entity):
                added += 1
        return f"✅ 已為 {entity} 加入 {added} 個 alias。"
    except Exception as exc:
        return f"alias 寫入失敗：{exc}"


def _do_remove(db, rest: str, knowledge_inbox=None) -> str:
    entity = rest.strip()
    if not entity:
        return "請提供 entity 名稱。"
    try:
        if knowledge_inbox is not None:
            knowledge_inbox.push("delete_entry", {"entity_canonical": entity.strip().lower()})
            return "✅ 已排入刪除佇列。"
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
