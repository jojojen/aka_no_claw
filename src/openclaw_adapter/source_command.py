"""Telegram-side dispatcher for the ``/source`` command (issue #9 D6/D7).

``/source S<n>`` inspects one source-registry record so a compact ``[S1]``
citation in a RAG digest or /research evidence list stays *traceable* — the
user can expand it back to the real URL and fetch the original article.

Usage:
  /source S1            — show title / domain / canonical URL / fetched_at / raw URL

Storage policy (D7)
-------------------
The source registry (``sources`` table in knowledge.sqlite3) stores, per
distinct source, exactly what is needed to render a compact citation and trace
it back to the original:

  * ``canonical_url`` — the deduplication key. Produced by
    :func:`url_canonicalize.canonicalize_url` with a strict **no-network**
    policy: known-redirector wrappers (google ``?q=``/``url=``, ddg ``uddg=`` …)
    are unwrapped from their own query params only, tracking params (utm_*,
    fbclid, gclid …) are stripped, host is lower-cased, fragment + trailing
    slash dropped. Opaque redirects (e.g. Yahoo listing blobs) are **not**
    fetched — resolving them would mean an extra third-party request and risks
    rate-limiting / bans (priority ②不被封鎖, SKILL.md C7). Because they cannot
    be expanded back to the original article offline, they are **refused** at
    intern time (``is_traceable_source``) and never become source records.
  * ``raw_url`` — the original, pre-canonicalization URL, kept for audit /
    manual recovery when canonicalization changed the link.
  * ``title`` / ``domain`` — display labels (``domain`` powers ``[S1] domain``).
  * ``fetched_at`` — when the source was first interned (UTC ISO).

Source ids are stable and never reused (SQLite AUTOINCREMENT), so a ``[S1]``
citation rendered today resolves to the same record indefinitely.
"""

from __future__ import annotations

import logging
from typing import Callable

from assistant_runtime import AssistantSettings

from .domain_registry import (
    get_domain,
    get_domain_trust,
    get_source_type,
    source_type_label,
)
from .knowledge_db import is_source_id

logger = logging.getLogger(__name__)


def build_source_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], str]:
    """Return the read-only ``/source`` command handler for the registry."""

    from .knowledge_db import KnowledgeDatabase

    db_path = settings.knowledge_db_path

    def handler(raw: str, chat_id: str) -> str:
        token = (raw or "").strip().split()[0] if (raw or "").strip() else ""
        if not token:
            return _usage_text()
        if not is_source_id(token):
            return f"「{token}」不是合法的來源 id（格式為 S<數字>，例如 S1）。"
        try:
            rec = KnowledgeDatabase(db_path).get_source(token)
        except Exception as exc:
            logger.exception("source_command: lookup failed token=%s", token)
            return f"來源查詢失敗：{exc}"
        if rec is None:
            return f"找不到來源 {token.upper()}。"
        domain_key = rec.domain_id or rec.domain or rec.canonical_url
        dom = get_domain(domain_key)
        lines = [
            f"🔗 來源 {rec.source_id}",
            f"標題：{rec.title or '（無）'}",
            f"網域：{rec.domain or '（無）'}",
        ]
        if dom is not None:
            lines += [
                f"來源類型：{dom.display_name}（{source_type_label(dom.source_type)}）",
                f"信任度：{dom.trust_score:.0%}",
            ]
        else:
            stype = get_source_type(domain_key)
            lines += [
                f"來源類型：{source_type_label(stype)}（未收錄網域，採預設）",
                f"信任度：{get_domain_trust(domain_key):.0%}",
            ]
        lines += [
            f"擷取時間：{rec.fetched_at or '（無）'}",
            "",
            f"連結：{rec.canonical_url}",
        ]
        if rec.raw_url and rec.raw_url != rec.canonical_url:
            lines += ["", f"原始連結：{rec.raw_url}"]
        return "\n".join(lines)

    return handler


def _usage_text() -> str:
    return "用法：/source S<編號>\n例如：/source S1"
