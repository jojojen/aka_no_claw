"""Local music playback for the ``/music`` Telegram command (issue #33).

Plays ``.flac`` files from the configured Mac mini music folder via ``afplay``,
launched as a detached background process whose pid + track are persisted to a
state file so a later ``/music stop`` can terminate exactly that process — and
nothing else (an unrelated music app or a reused pid is never killed).

Supported MVP commands:

    /music random      — play one random indexed song
    /music <歌曲名>     — search indexed filenames and play the best match
    /music stop        — stop the song OpenClaw is currently playing

The folder is indexed (filename stems) into a cache under the gitignored
``.openclaw_tmp/`` so repeat plays skip the rescan; the cache is keyed by a
signature over every indexed file's path/size/mtime, so adding, removing or
renaming files transparently rebuilds the index on the next play. AppleDouble
metadata sidecars (``._*.flac``) are excluded and never selectable.

The process helpers (``_spawn_player`` / ``_pid_alive`` / ``_pid_is_player`` /
``_terminate``) are module-level so tests can stub real playback.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = (".flac",)
_PLAYER_BINARY = "afplay"


# --- process primitives (module-level so tests can monkeypatch) -----------
def _spawn_player(path: str) -> int:
    """Start ``afplay`` detached in its own session (so signals to the bot's
    process group don't reach it) and return its pid.

    A daemon thread reaps the child once it exits — whether the song ends on its
    own or ``/music stop`` kills it — so the long-running bot never accumulates
    zombie processes (and ``_pid_alive`` reports it dead promptly after a stop,
    instead of seeing an unreaped zombie as still alive)."""
    proc = subprocess.Popen(
        [_PLAYER_BINARY, path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_is_player(pid: int) -> bool:
    """True only if pid is an ``afplay`` process. Guards against killing an
    unrelated process that happens to have reused our recorded pid."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 — ps missing/erroring => treat as not ours
        return False
    return (out.stdout or "").strip().endswith(_PLAYER_BINARY)


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


# --- index build / cache / invalidation -----------------------------------
def _iter_audio_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.startswith("._"):  # macOS AppleDouble sidecar
                continue
            if fn.lower().endswith(_AUDIO_SUFFIXES):
                yield Path(dirpath) / fn


def _scan(root: Path) -> list[tuple[str, int, int]]:
    """Sorted ``(abspath, size, mtime_ns)`` for every indexable audio file."""
    scanned: list[tuple[str, int, int]] = []
    for p in _iter_audio_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        scanned.append((str(p), st.st_size, st.st_mtime_ns))
    scanned.sort()
    return scanned


def _signature(scanned: list[tuple[str, int, int]], root: Path) -> str:
    h = hashlib.sha1()
    h.update(str(root).encode("utf-8"))
    for path, size, mtime in scanned:
        h.update(f"\0{path}\0{size}\0{mtime}".encode("utf-8"))
    return h.hexdigest()


def _entries_from_scan(scanned: list[tuple[str, int, int]]) -> list[dict]:
    return [{"path": path, "name": Path(path).stem} for path, _size, _mtime in scanned]


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


@dataclass(frozen=True)
class MusicIndex:
    entries: list[dict]
    signature: str
    rebuilt: bool


def load_or_build_index(music_dir: str, index_path: str) -> MusicIndex:
    """Return the song index, rebuilding only when the folder's signature has
    changed (files added/removed/renamed/edited); otherwise reuse the cache."""
    root = Path(music_dir)
    scanned = _scan(root)
    sig = _signature(scanned, root)
    cached = _read_json(index_path)
    if cached is not None and cached.get("signature") == sig:
        entries = cached.get("entries")
        if isinstance(entries, list):
            return MusicIndex(entries=entries, signature=sig, rebuilt=False)
    entries = _entries_from_scan(scanned)
    _write_json(index_path, {"signature": sig, "entries": entries})
    return MusicIndex(entries=entries, signature=sig, rebuilt=True)


# --- search ----------------------------------------------------------------
def _normalize(text: str) -> str:
    # NFKC + casefold so half/full-width and composed/decomposed Japanese
    # filenames match a query that differs only by Unicode normalization.
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


def _search(entries: list[dict], query: str) -> dict | None:
    nq = _normalize(query)
    if not nq:
        return None
    substring: list[tuple[int, int, dict]] = []
    for e in entries:
        nn = _normalize(e.get("name", ""))
        if nq == nn:
            return e  # exact normalized match wins outright
        if nq in nn:
            substring.append((0 if nn.startswith(nq) else 1, len(nn), e))
    if substring:
        substring.sort(key=lambda t: (t[0], t[1]))
        return substring[0][2]
    names = [_normalize(e.get("name", "")) for e in entries]
    close = difflib.get_close_matches(nq, names, n=1, cutoff=0.6)
    if close:
        return entries[names.index(close[0])]
    return None


# --- player state ----------------------------------------------------------
def _current_running(state_path: str) -> dict | None:
    """Return the persisted player state iff its process is still a live
    ``afplay``; otherwise clear the stale state and return None."""
    state = _read_json(state_path)
    if not state:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        _clear_state(state_path)
        return None
    if _pid_alive(pid) and _pid_is_player(pid):
        return state
    _clear_state(state_path)
    return None


def _clear_state(state_path: str) -> None:
    try:
        Path(state_path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear player state %s", state_path)


def _stop(state_path: str) -> str:
    running = _current_running(state_path)
    if running is None:
        return "目前沒有由龍蝦播放中的音樂。"
    try:
        _terminate(int(running["pid"]))
    except Exception as exc:  # noqa: BLE001
        logger.exception("music: stop failed")
        return f"停止播放失敗：{exc}"
    _clear_state(state_path)
    return "已停止目前由龍蝦播放的音樂。"


def _play(entry: dict, state_path: str) -> str:
    # Stop any previous OpenClaw-started track first so we never leave two
    # playing at once.
    running = _current_running(state_path)
    if running is not None:
        try:
            _terminate(int(running["pid"]))
        except Exception:  # noqa: BLE001 — best-effort; we overwrite state below
            logger.exception("music: failed to stop previous track")
        _clear_state(state_path)
    try:
        pid = _spawn_player(entry["path"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("music: playback failed path=%s", entry.get("path"))
        return f"播放失敗：{exc}"
    _write_json(state_path, {"pid": pid, "name": entry["name"], "path": entry["path"]})
    return f"正在播放：\n{entry['name']}"


# --- command handler -------------------------------------------------------
def build_music_handler(settings: AssistantSettings) -> Callable[[str, str], str]:
    music_dir = settings.openclaw_music_dir
    index_path = settings.openclaw_music_index_path
    state_path = settings.openclaw_music_player_state_path

    def handler(raw: str, chat_id: str) -> str:
        arg = (raw or "").strip()
        if not arg:
            return _usage_text()
        if arg.lower() == "stop":
            return _stop(state_path)

        root = Path(music_dir)
        if not root.exists():
            return f"找不到音樂資料夾：{music_dir}"
        if not root.is_dir():
            return f"音樂路徑不是資料夾：{music_dir}"
        try:
            index = load_or_build_index(music_dir, index_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("music: index build failed dir=%s", music_dir)
            return f"音樂索引建立失敗：{exc}"
        if not index.entries:
            return f"音樂資料夾找不到可播放的音檔（.flac）：{music_dir}"

        if arg.lower() == "random":
            return _play(random.choice(index.entries), state_path)
        entry = _search(index.entries, arg)
        if entry is None:
            return f"找不到符合「{arg}」的歌曲。"
        return _play(entry, state_path)

    return handler


def _usage_text() -> str:
    return (
        "用法：\n"
        "/music random — 隨機播放一首\n"
        "/music <歌曲名> — 搜尋並播放最相符的歌曲\n"
        "/music stop — 停止目前由龍蝦播放的音樂"
    )
