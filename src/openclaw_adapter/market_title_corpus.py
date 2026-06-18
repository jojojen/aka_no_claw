"""Append-only corpus of marketplace listing titles for PR2 historical IDF.

Both /research and /opportunity already fetch marketplace search results as part
of their normal work. Every result carries a listing title, and that title is the
exact population the comp filter has to score. This module catches those titles —
a free byproduct of queries already being made, so it adds **zero** new external
requests — and de-duplicates them into a small SQLite store that the offline DF
builder later distills into `data/market_title_df.json`.

Design constraints:
- Fail-safe: capturing titles must never break a /research or /opportunity run.
  Every public entry point swallows and logs its own errors.
- Multi-writer: the telegram bot and the opportunity service are separate
  processes writing the same file, so WAL + a busy timeout are mandatory.
- One document per distinct raw title: the same listing is seen on many scans;
  counting it once keeps document frequency honest (see research_command).
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

_CORPUS_PATH = pathlib.Path(__file__).resolve().parents[2] / "data" / "market_title_corpus.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    raw_title TEXT PRIMARY KEY,
    source TEXT,
    first_seen_at TEXT NOT NULL
);
"""


def _resolve_path(path: pathlib.Path | str | None) -> pathlib.Path:
    return pathlib.Path(path) if path is not None else _CORPUS_PATH


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=4000")
    conn.execute(_SCHEMA)
    return conn


def record_titles(
    titles: Iterable[str],
    *,
    source: str,
    path: pathlib.Path | str | None = None,
) -> int:
    """De-duplicate and persist marketplace titles. Returns the number of NEW
    titles inserted. Never raises — a capture failure must not abort the caller's
    /research or /opportunity run."""
    try:
        cleaned = []
        seen: set[str] = set()
        for title in titles:
            text = str(title or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        target = _resolve_path(path)
        conn = _connect(target)
        try:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO titles (raw_title, source, first_seen_at) VALUES (?, ?, ?)",
                [(text, source, now) for text in cleaned],
            )
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()
    except Exception:
        logger.exception("market title corpus capture failed source=%s", source)
        return 0


def iter_titles(path: pathlib.Path | str | None = None) -> list[str]:
    """All distinct stored titles. Returns [] if the store does not yet exist."""
    target = _resolve_path(path)
    if not target.exists():
        return []
    conn = _connect(target)
    try:
        rows = conn.execute("SELECT raw_title FROM titles").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def corpus_size(path: pathlib.Path | str | None = None) -> int:
    """Number of distinct titles accumulated so far (0 if the store is absent)."""
    target = _resolve_path(path)
    if not target.exists():
        return 0
    conn = _connect(target)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0])
    finally:
        conn.close()
