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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .knowledge_db import KnowledgeDatabase as KnowledgeDB, KnowledgeEntry

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
    """ISO timestamp for today at 00:00:00 local time."""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _format_entry_message(entry: KnowledgeEntry, index: int, total: int) -> str:
    summary = entry.summary[:400] if entry.summary else "（無摘要）"
    sources = "、".join(entry.source_urls[:2]) if entry.source_urls else ""
    lines = [
        f"📚 今日 RAG 新知（{index}/{total}）",
        "",
        f"【{entry.entity_canonical}】",
        summary,
    ]
    if sources:
        lines += ["", f"來源：{sources}"]
    lines += ["", f"類型：{entry.entity_type}　信心：{entry.confidence:.0%}"]
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
    ) -> None:
        self._db_path = Path(db_path)
        self._chat_ids = chat_ids
        self._send_fn = send_fn
        self._hour = hour
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

    def _send_digest(self) -> None:
        if not self._db_path.exists():
            return
        db = KnowledgeDB(self._db_path)
        entries = db.entries_since(_today_start_iso())
        if not entries:
            logger.info("RagDailyDigestScheduler: no new entries today — silent")
            return
        total = len(entries)
        logger.info("RagDailyDigestScheduler: sending %d entry digests", total)
        for i, entry in enumerate(entries, 1):
            text = _format_entry_message(entry, i, total)
            markup = _make_reply_markup(entry.entry_id)
            for chat_id in self._chat_ids:
                try:
                    self._send_fn(chat_id, text, markup)
                except Exception:
                    logger.exception(
                        "RagDailyDigestScheduler: send failed chat_id=%s entry_id=%s",
                        chat_id, entry.entry_id,
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
) -> tuple[str, None]:
    """Deletes the entry and returns updated text with buttons cleared."""
    db = KnowledgeDB(db_path)
    deleted = db.delete_entry(entry_id)
    suffix = "🗑️ 已從知識庫刪除" if deleted else "⚠️ 找不到條目（可能已刪除）"
    return f"{original_text}\n\n{suffix}", None
