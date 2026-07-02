"""Favorite-song store for the ``/music`` feature (issue #34).

Persists a small favorite list to a gitignored runtime JSON file. Each entry is
keyed by a stable id derived from the song's normalized absolute path, so the
same file always maps to the same id (adding it twice never duplicates) and the
id is short enough to ride inside a 64-byte Telegram ``callback_data``.

The store also exposes a view-fn / item-deleter pair so ``/musiclistbest`` can
reuse the shared paginated list view (``mb`` list kind) — the same edit/remove
flow the watchlist and other lists already use.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from telegram_core.list_view import (
    LIST_VIEW_MODE_READ,
    ListRow,
    build_list_view,
)

logger = logging.getLogger(__name__)

_ID_LEN = 16
MUSIC_BEST_LIST_KIND = "mb"


def stable_id(path: str) -> str:
    """A short, stable id for a song path: ``sha1`` of its NFC-normalized
    absolute path. NFC so HFS+'s decomposed (NFD) filenames and a composed
    query resolve to the same id."""
    norm = unicodedata.normalize("NFC", os.path.abspath(path))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:_ID_LEN]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FavoritesStore:
    """Thin JSON-backed list of favorite songs.

    Not designed for concurrent writers; the Telegram bot is single-process and
    favorite edits are user-paced, so a read-modify-write per mutation is fine.
    """

    def __init__(self, path: str) -> None:
        self._path = path

    # --- persistence ------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def _save(self, entries: list[dict]) -> None:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(p)

    # --- queries ----------------------------------------------------------
    def list(self) -> list[dict]:
        return self._load()

    def get(self, song_id: str) -> dict | None:
        for e in self._load():
            if e.get("id") == song_id:
                return e
        return None

    def contains(self, song_id: str) -> bool:
        return self.get(song_id) is not None

    # --- mutations --------------------------------------------------------
    def add(self, path: str, name: str) -> tuple[bool, dict]:
        """Add a song. Returns ``(added, entry)``; ``added`` is False when the
        song was already a favorite (no duplicate created)."""
        sid = stable_id(path)
        entries = self._load()
        for e in entries:
            if e.get("id") == sid:
                return False, e
        entry = {
            "id": sid,
            "name": name,
            "path": os.path.abspath(path),
            "added_at": _now(),
            "last_played_at": None,
            "play_count": 0,
        }
        entries.append(entry)
        self._save(entries)
        return True, entry

    def remove(self, song_id: str) -> bool:
        entries = self._load()
        kept = [e for e in entries if e.get("id") != song_id]
        if len(kept) == len(entries):
            return False
        self._save(kept)
        return True

    def mark_played(self, song_id: str) -> None:
        entries = self._load()
        changed = False
        for e in entries:
            if e.get("id") == song_id:
                e["last_played_at"] = _now()
                e["play_count"] = int(e.get("play_count") or 0) + 1
                changed = True
                break
        if changed:
            self._save(entries)


# --- shared list view (``/musiclistbest`` edit flow) ----------------------
def _short(name: str, limit: int = 20) -> str:
    name = name or ""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _build_best_view(store: FavoritesStore, *, page: int, mode: str):
    entries = store.list()
    rows: list[ListRow] = []
    for e in entries:
        name = e.get("name") or "(未命名)"
        plays = int(e.get("play_count") or 0)
        rows.append(
            ListRow(
                id=str(e.get("id") or ""),
                text=f"🎵 {name}" + (f"（已播 {plays} 次）" if plays else ""),
                short_label=_short(name),
                extra_buttons=(
                    {"text": f"▶️ {_short(name)}", "callback_data": f"music:pf:{e.get('id')}"},
                ),
            )
        )
    return build_list_view(
        list_kind=MUSIC_BEST_LIST_KIND,
        items=rows,
        page=page,
        mode=mode,
        list_title="🎶 最愛歌曲",
        empty_message="最愛清單是空的。用歌曲詳情的「加入最愛」或 /musicnowbest 加入。",
        read_mode_row_buttons=True,
    )


def build_music_best_view_fn(store: FavoritesStore):
    def view_fn(*, page: int = 0, mode: str = LIST_VIEW_MODE_READ):
        return _build_best_view(store, page=page, mode=mode)

    return view_fn


def build_music_best_item_deleter(store: FavoritesStore):
    def deleter(song_id: str) -> bool:
        try:
            return store.remove(song_id)
        except Exception:
            logger.exception("music: favorite delete failed id=%s", song_id)
            return False

    return deleter, "最愛歌曲"
