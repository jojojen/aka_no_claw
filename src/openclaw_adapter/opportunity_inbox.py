"""Inbox for Telegram → opportunity_agent write requests.

Telegram process INSERTs pending rows; opportunity_agent service polls and
applies them to data/opportunities.sqlite3 then marks done.
Single-writer-per-file: opportunities.sqlite3 is owned by opportunity_agent.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS opportunity_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_opportunity_requests_status ON opportunity_requests (status);
"""


class OpportunityInbox:
    """Thread-safe inbox for opportunity DB write operations from the Telegram process.

    *action* values understood by the agent:
    - ``dismiss_candidate``  — payload = ``{"candidate_id": ...}``
    - ``set_is_target``      — payload = ``{"candidate_id": ..., "is_target": bool}``
    - ``update_string_list`` — payload = ``{"candidate_id": ..., "kind": ..., "action": ..., "names": [...]}``
    - ``pin_by_name``        — payload = ``{"name": ...}``
    - ``record_feedback``    — payload = ``{"recommendation_id": ..., "kind": ...}``
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self._lock = threading.Lock()

    def bootstrap(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CREATE_SQL)

    def push(self, action: str, payload: dict, chat_id: str = "") -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO opportunity_requests (chat_id, action, payload, created_at) VALUES (?,?,?,?)",
                (chat_id, action, json.dumps(payload, default=str), _utc_now()),
            )
            conn.commit()
            return cursor.lastrowid

    def pop_pending(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, chat_id, action, payload FROM opportunity_requests "
                "WHERE status='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "chat_id": r[1], "action": r[2], "payload": json.loads(r[3])}
            for r in rows
        ]

    def mark_done(self, req_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE opportunity_requests SET status='done', processed_at=? WHERE id=?",
                (_utc_now(), req_id),
            )
            conn.commit()

    def mark_error(self, req_id: int, msg: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE opportunity_requests SET status='error', processed_at=?, payload=? WHERE id=?",
                (_utc_now(), json.dumps({"error": msg[:500]}), req_id),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
