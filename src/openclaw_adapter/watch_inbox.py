"""Inbox queue for Telegram → price_monitor_service write requests.

Telegram reads monitor.sqlite3 (WAL, read-only) and pushes watch CRUD
operations here.  price_monitor_service is the sole writer and polls this
inbox every few seconds to apply changes.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT    NOT NULL,
    payload     TEXT    NOT NULL DEFAULT '{}',
    chat_id     TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending',
    error_msg   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_watch_inbox_status ON watch_inbox (status);
"""


class WatchInbox:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def push(self, action: str, payload: dict, chat_id: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO watch_inbox (action, payload, chat_id) VALUES (?,?,?)",
                (action, json.dumps(payload, ensure_ascii=False), chat_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def pop_pending(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watch_inbox WHERE status='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def mark_done(self, req_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watch_inbox SET status='done', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (req_id,),
            )

    def mark_error(self, req_id: int, msg: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watch_inbox SET status='error', error_msg=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (msg[:500], req_id),
            )
