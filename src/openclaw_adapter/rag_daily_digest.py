"""Daily RAG knowledge digest (sent at 22:00 local time).

Queries knowledge_db for entries created today and sends each as a separate
Telegram message with [✅ 保留] / [🗑️ 刪除] inline buttons.

Callback prefix:
  ragkeep:<entry_id>   → acknowledge (clear buttons, show confirmed)
  ragdel:<entry_id>    → delete from knowledge_db + edit message
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .knowledge_db import (
    KnowledgeDatabase as KnowledgeDB,
    KnowledgeEntry,
    is_insufficient_entry,
    is_operational_cache_entry,
    is_source_id,
)
from .domain_registry import domain_citation_label
from .url_canonicalize import source_domain

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, dict | None], None]  # (chat_id, text, reply_markup)

_DIGEST_HOUR = 22   # local time


def _seconds_until_next(hour: int) -> float:
    """Seconds from now until the next occurrence of *hour*:00 local time."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _today_start_iso() -> str:
    """UTC ISO timestamp for the start of *today in local time*.

    Returned in UTC so it compares correctly against ``created_at`` (stored in
    UTC) in ``entries_since``'s string comparison. A naive local-midnight
    threshold would be lexically greater than today's UTC-stamped entries in the
    hours after local midnight (when local is ahead of UTC), silently dropping
    them from the digest."""
    local_midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(timezone.utc).isoformat()


# Product-intelligence entries are tied to a specific product / store and are
# inherently time-sensitive (prices, availability) — the user triages them
# differently from durable knowledge (IP / character / set / creator facts that
# stay true). The split is by the entry's *structured* entity_type, not by
# scanning text, so it adds no open-world recognition (Rule G).
_PRODUCT_INTEL_ENTITY_TYPES: frozenset[str] = frozenset({"product", "store"})

_SECTION_DURABLE = "📚 今日 RAG 新知（長效知識）"
_SECTION_PRODUCT_INTEL = "🛒 今日 RAG 新知（商品情報）"

# Human labels for the structured signal vocabularies, kept tiny and local.
_ACTIONABILITY_LABEL: dict[str, str] = {
    "actionable": "可下手",
    "informational": "情報",
    "blocked": "暫不推薦",
}


def _is_product_intelligence(entry: KnowledgeEntry) -> bool:
    return entry.entity_type in _PRODUCT_INTEL_ENTITY_TYPES


def _render_citation(ref: str, db: KnowledgeDB | None) -> str:
    """Render one source ref as a compact, traceable citation.

    - ``S<n>`` source ids resolve (via *db*) to ``[S1] Suruga-ya (Marketplace)``
      — the compact form issue #9 wants, now enriched with the issue #11 domain
      label (display name + source type) for seeded hosts, falling back to the
      bare domain for unseeded ones.
    - Legacy raw URLs (interned before the registry, or when interning failed)
      degrade to their domain label so citations stay readable either way.
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    if is_source_id(ref):
        rec = db.get_source(ref) if db is not None else None
        if rec is not None:
            host = rec.domain or source_domain(rec.canonical_url) or rec.canonical_url
            return f"[{rec.source_id}] {domain_citation_label(host)}"
        return f"[{ref}]"
    return domain_citation_label(ref) or source_domain(ref) or ref


def _format_entry_message(
    entry: KnowledgeEntry,
    index: int,
    total: int,
    *,
    section_title: str,
    db: KnowledgeDB | None = None,
) -> str:
    summary = entry.summary[:400] if entry.summary else "（無摘要）"
    citations = [c for c in (_render_citation(r, db) for r in entry.source_urls[:2]) if c]
    sources = "、".join(citations)
    lines = [
        f"{section_title}（{index}/{total}）",
        "",
        f"【{entry.entity_canonical}】",
        summary,
    ]
    if sources:
        lines += ["", f"來源：{sources}"]
    lines += ["", f"類型：{entry.entity_type}　信心：{entry.confidence:.0%}"]
    return "\n".join(lines)


def _format_signal_message(
    signal,
    index: int,
    total: int,
    *,
    section_title: str,
) -> str:
    """Render one CollectibleSignal as the product-intelligence digest entry.

    Signals are the structured product-intel source of truth (issue #8 finding
    4): they already carry product identity / price band / official code / market
    evidence, so they replace the ad-hoc ``product`` knowledge entries here."""
    headline = signal.title.strip() or signal.ip_canonical.strip() or signal.signal_id
    detail_bits: list[str] = []
    if signal.ip_canonical and signal.ip_canonical != headline:
        detail_bits.append(f"IP：{signal.ip_canonical}")
    if signal.product_type and signal.product_type != "other":
        detail_bits.append(f"類別：{signal.product_type}")
    if signal.official_code:
        detail_bits.append(f"型番：{signal.official_code}")
    if signal.retail_price_jpy:
        detail_bits.append(f"定価 ¥{signal.retail_price_jpy:,}")
    state = _ACTIONABILITY_LABEL.get(signal.actionability, signal.actionability)
    if signal.actionability == "blocked" and signal.block_reason:
        state = f"{state}（{signal.block_reason}）"

    citations = [domain_citation_label(u) or source_domain(u) or u
                 for u in signal.source_urls[:2]]
    sources = "、".join(c for c in citations if c)

    lines = [
        f"{section_title}（{index}/{total}）",
        "",
        f"【{headline}】",
    ]
    if detail_bits:
        lines.append(" ｜ ".join(detail_bits))
    if sources:
        lines += ["", f"來源：{sources}"]
    lines += [
        "",
        f"狀態：{state}　領域：{signal.collectible_domain}　信心：{signal.confidence:.0%}",
    ]
    return "\n".join(lines)


def _make_reply_markup(entry_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ 保留", "callback_data": f"ragkeep:{entry_id}"},
            {"text": "🗑️ 刪除", "callback_data": f"ragdel:{entry_id}"},
        ]]
    }


class RagDailyDigestScheduler:
    """Daemon thread that sends the daily RAG digest at _DIGEST_HOUR."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        chat_ids: tuple[str, ...],
        send_fn: SendFn,
        hour: int = _DIGEST_HOUR,
        signal_db_path: str | Path | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._chat_ids = chat_ids
        self._send_fn = send_fn
        self._hour = hour
        # When set, the 商品情報 section reads structured signals from the
        # collectible signal store instead of ``product`` knowledge entries
        # (issue #8 finding 4).
        self._signal_db_path = Path(signal_db_path) if signal_db_path else None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="rag-daily-digest", daemon=True,
        )
        self._thread.start()
        delay = _seconds_until_next(self._hour)
        logger.info(
            "RagDailyDigestScheduler started — first fire in %.0f min at %02d:00",
            delay / 60, self._hour,
        )

    def _loop(self) -> None:
        while True:
            time.sleep(_seconds_until_next(self._hour))
            try:
                self._send_digest()
            except Exception:
                logger.exception("RagDailyDigestScheduler: digest send failed")
            # Sleep 23h to avoid double-firing in the same minute
            time.sleep(23 * 3600)

    def _load_today_signals(self) -> list:
        """Today's structured product-intelligence signals, or [] when the store
        is not wired / unavailable. Blocked signals are skipped — they are not
        product news the user should triage."""
        if self._signal_db_path is None or not self._signal_db_path.exists():
            return []
        try:
            from .collectible_signal_store import CollectibleSignalStore
            store = CollectibleSignalStore(self._signal_db_path)
            signals = store.signals_since(_today_start_iso())
        except Exception:
            logger.exception("RagDailyDigestScheduler: signal store read failed")
            return []
        return [s for s in signals if s.actionability != "blocked"]

    def _send_digest(self) -> None:
        if not self._db_path.exists():
            return
        db = KnowledgeDB(self._db_path)
        entries = db.entries_since(_today_start_iso())
        # Never push entries that carry no human-reviewable knowledge: 資料不足/一般常識
        # no-data stubs (internal negative cache) and operational caches such as the
        # 遊々亭 game-code mapping (internal plumbing kept only to avoid re-searching).
        entries = [
            e for e in entries
            if not is_insufficient_entry(e) and not is_operational_cache_entry(e)
        ]
        durable = [e for e in entries if not _is_product_intelligence(e)]

        # Product intelligence source of truth (issue #8 finding 4): the
        # structured signal store when wired, otherwise the legacy ``product``
        # knowledge entries (keeps behaviour identical where no store is set).
        signals = self._load_today_signals()
        if self._signal_db_path is not None:
            product_entries: list = []
        else:
            product_entries = [e for e in entries if _is_product_intelligence(e)]

        if not durable and not product_entries and not signals:
            logger.info("RagDailyDigestScheduler: no new entries today — silent")
            return
        logger.info(
            "RagDailyDigestScheduler: sending %d durable + %d product-intel(entries) "
            "+ %d product-intel(signals) digests",
            len(durable), len(product_entries), len(signals),
        )

        # Durable knowledge (with keep/delete buttons), then legacy product
        # entries (same buttons), then structured signals (no buttons — the
        # intelligence layer is auto-curated, not hand-triaged here).
        for section_title, group in (
            (_SECTION_DURABLE, durable),
            (_SECTION_PRODUCT_INTEL, product_entries),
        ):
            total = len(group)
            for i, entry in enumerate(group, 1):
                text = _format_entry_message(
                    entry, i, total, section_title=section_title, db=db,
                )
                markup = _make_reply_markup(entry.entry_id)
                for chat_id in self._chat_ids:
                    try:
                        self._send_fn(chat_id, text, markup)
                    except Exception:
                        logger.exception(
                            "RagDailyDigestScheduler: send failed chat_id=%s entry_id=%s",
                            chat_id, entry.entry_id,
                        )

        total_signals = len(signals)
        for i, signal in enumerate(signals, 1):
            text = _format_signal_message(
                signal, i, total_signals, section_title=_SECTION_PRODUCT_INTEL,
            )
            for chat_id in self._chat_ids:
                try:
                    self._send_fn(chat_id, text, None)
                except Exception:
                    logger.exception(
                        "RagDailyDigestScheduler: signal send failed chat_id=%s id=%s",
                        chat_id, signal.signal_id,
                    )


def handle_ragkeep_callback(
    *,
    entry_id: str,
    original_text: str,
) -> tuple[str, None]:
    """Returns (new_text, None) — clears the buttons, marks confirmed."""
    return f"{original_text}\n\n✅ 已確認保留", None


def handle_ragdel_callback(
    *,
    entry_id: str,
    original_text: str,
    db_path: str | Path,
    knowledge_inbox=None,
) -> tuple[str, None]:
    """Deletes the entry and returns updated text with buttons cleared."""
    if knowledge_inbox is not None:
        knowledge_inbox.push("delete_entry", {"entry_id": entry_id})
        return f"{original_text}\n\n🗑️ 已排入刪除佇列", None
    db = KnowledgeDB(db_path)
    deleted = db.delete_entry(entry_id)
    suffix = "🗑️ 已從知識庫刪除" if deleted else "⚠️ 找不到條目（可能已刪除）"
    return f"{original_text}\n\n{suffix}", None
