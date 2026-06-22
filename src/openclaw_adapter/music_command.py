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

A play only ever targets a *single* unambiguous song: an exact filename match or
a lone substring/fuzzy hit. When a query is broad enough to match several songs
(e.g. an artist or album name) the handler returns a short candidate list rather
than guessing and audibly playing the wrong track.

To stop *only* OpenClaw's own player and never an unrelated ``afplay`` the user
launched themselves, the persisted state records the process *identity* — pid
plus its start time — and ``/music stop`` re-verifies both before signalling, so
a recorded pid that the OS later reused for a different ``afplay`` is left alone.

The process helpers (``_spawn_player`` / ``_pid_alive`` / ``_pid_is_player`` /
``_pid_start_time`` / ``_terminate``) are module-level so tests can stub real
playback.
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
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from assistant_runtime import AssistantSettings

from .music_favorites import FavoritesStore

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


def _pid_start_time(pid: int) -> str | None:
    """The process's absolute start time (``ps -o lstart``), used as a stable
    identity token alongside the pid. If the OS later reuses this pid for a
    different process, its start time differs, so we can tell it is not the
    afplay we launched and refuse to signal it."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 — ps missing/erroring => unknown identity
        return None
    return (out.stdout or "").strip() or None


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
    _write_json(
        index_path,
        {
            "signature": sig,
            "root": str(root),
            "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(entries),
            "entries": entries,
        },
    )
    return MusicIndex(entries=entries, signature=sig, rebuilt=True)


# --- callback path safety --------------------------------------------------
def validate_song_path(path: str, music_dir: str) -> bool:
    """True iff ``path`` is safe to play from a Telegram callback: it resolves
    to a real ``.flac`` file (not an AppleDouble ``._*`` sidecar) located under
    ``music_dir``. Tokens come from user-clickable buttons, so a resolved path
    must be re-validated before playback to prevent escaping the music root."""
    try:
        resolved = Path(path).resolve()
        root = Path(music_dir).resolve()
    except OSError:
        return False
    if root != resolved and root not in resolved.parents:
        return False
    name = resolved.name
    if name.startswith("._"):
        return False
    if not name.lower().endswith(_AUDIO_SUFFIXES):
        return False
    return resolved.is_file()


# --- search ----------------------------------------------------------------
def _normalize(text: str) -> str:
    # NFKC + casefold so half/full-width and composed/decomposed Japanese
    # filenames match a query that differs only by Unicode normalization.
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


_MAX_CANDIDATES = 5


@dataclass(frozen=True)
class SearchResult:
    """Outcome of a query against the index.

    ``kind`` is one of:
      * ``"exact"`` / ``"single"`` — one unambiguous song in ``entry``; play it.
      * ``"ambiguous"`` — several close songs in ``candidates``; ask, don't play.
      * ``"none"`` — nothing matched.
    """

    kind: str
    entry: dict | None = None
    candidates: tuple[dict, ...] = ()


def _search(entries: list[dict], query: str) -> SearchResult:
    nq = _normalize(query)
    if not nq:
        return SearchResult("none")
    substring: list[tuple[int, int, dict]] = []
    for e in entries:
        nn = _normalize(e.get("name", ""))
        if nq == nn:
            return SearchResult("exact", entry=e)  # exact normalized match wins
        if nq in nn:
            substring.append((0 if nn.startswith(nq) else 1, len(nn), e))
    if substring:
        substring.sort(key=lambda t: (t[0], t[1]))
        ordered = [t[2] for t in substring]
        if len(ordered) == 1:
            return SearchResult("single", entry=ordered[0])
        return SearchResult("ambiguous", candidates=tuple(ordered[:_MAX_CANDIDATES]))
    names = [_normalize(e.get("name", "")) for e in entries]
    close = difflib.get_close_matches(nq, names, n=_MAX_CANDIDATES, cutoff=0.6)
    if not close:
        return SearchResult("none")
    matched = [entries[names.index(c)] for c in close]
    if len(matched) == 1:
        return SearchResult("single", entry=matched[0])
    return SearchResult("ambiguous", candidates=tuple(matched))


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
    if not (_pid_alive(pid) and _pid_is_player(pid)):
        _clear_state(state_path)
        return None
    # Even a live afplay on this pid is only ours if its start time still
    # matches what we recorded — otherwise the OS reused the pid for a
    # different (possibly user-launched) afplay and we must not signal it.
    recorded_start = state.get("start")
    if not recorded_start or recorded_start != _pid_start_time(pid):
        _clear_state(state_path)
        return None
    return state


def _clear_state(state_path: str) -> None:
    try:
        Path(state_path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear player state %s", state_path)


def _terminate_running(state_path: str) -> None:
    """Stop and clear the currently recorded OpenClaw track, if any."""
    running = _current_running(state_path)
    if running is None:
        return
    try:
        _terminate(int(running["pid"]))
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception("music: failed to stop previous track")
    _clear_state(state_path)


def _start_song(entry: dict, state_path: str) -> int:
    """Stop the previous track, spawn ``entry``, persist its identity, return
    the pid. Shared by user-initiated plays and the playbest loop; callers that
    are *not* the playbest loop must stop playbest first (see :func:`_play`)."""
    _terminate_running(state_path)
    pid = _spawn_player(entry["path"])
    _write_json(
        state_path,
        {
            "pid": pid,
            "name": entry["name"],
            "path": entry["path"],
            "start": _pid_start_time(pid),
        },
    )
    return pid


def _stop(state_path: str) -> str:
    # /music stop ends both the current song AND any continuous mode (random or
    # playbest, which share the controller), and it must not auto-restart after.
    was_continuous = _PLAYBEST.stop()
    running = _current_running(state_path)
    if running is not None:
        try:
            _terminate(int(running["pid"]))
        except Exception as exc:  # noqa: BLE001
            logger.exception("music: stop failed")
            return f"停止播放失敗：{exc}"
        _clear_state(state_path)
        return "已停止目前由龍蝦播放的音樂。"
    if was_continuous:
        return "已停止連續播放。"
    return "目前沒有由龍蝦播放中的音樂。"


def _play(entry: dict, state_path: str) -> str:
    # A user-initiated single play cancels playbest so the loop never fights it
    # for the player (and never leaves two OpenClaw songs playing at once).
    _PLAYBEST.stop()
    try:
        _start_song(entry, state_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("music: playback failed path=%s", entry.get("path"))
        return f"播放失敗：{exc}"
    return f"正在播放：\n{entry['name']}"


# --- playbest: continuous shuffled favorite playback -----------------------
_PLAYBEST_POLL_SECONDS = 1.0


class PlaybestScheduler:
    """Yields favorites to play with round semantics: every favorite plays once
    per round before any repeats, then the list reshuffles for the next round.
    Missing files are skipped so a deleted favorite never stalls the loop.

    Pure and synchronous (no threads/sleeps) so the round/no-repeat/skip rules
    are unit-testable directly; the threaded controller drives it."""

    def __init__(self, entries_provider, exists_fn=None, shuffler=None) -> None:
        self._provider = entries_provider
        self._exists = exists_fn or os.path.exists
        self._shuffle = shuffler or random.shuffle
        self._round: list[dict] = []

    def next(self) -> dict | None:
        while True:
            if not self._round:
                fresh = [e for e in self._provider() if self._exists(e.get("path", ""))]
                if not fresh:
                    return None
                self._shuffle(fresh)
                self._round = fresh
            entry = self._round.pop(0)
            if self._exists(entry.get("path", "")):  # may have vanished mid-round
                return entry


class PlaybestController:
    """Runs :class:`PlaybestScheduler` in a daemon thread, auto-advancing to the
    next favorite when the current song's process exits. Stoppable and
    non-restarting once stopped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._thread: threading.Thread | None = None
        self._current_pid: int | None = None

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def start(self, entries_provider, state_path: str, on_play=None, is_playable=None) -> None:
        self.stop()
        with self._lock:
            self._active = True
            thread = threading.Thread(
                target=self._loop,
                args=(entries_provider, state_path, on_play, is_playable),
                daemon=True,
                name="music-playbest",
            )
            self._thread = thread
        thread.start()

    def stop(self) -> bool:
        with self._lock:
            was_active = self._active
            self._active = False
            pid = self._current_pid
            self._current_pid = None
            thread = self._thread
            self._thread = None
        if pid is not None:
            try:
                _terminate(pid)
            except Exception:  # noqa: BLE001
                logger.exception("playbest: terminate on stop failed")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return was_active

    def _loop(self, entries_provider, state_path: str, on_play, is_playable=None) -> None:
        scheduler = PlaybestScheduler(entries_provider, exists_fn=is_playable)
        while self.is_active():
            entry = scheduler.next()
            if entry is None:  # favorites empty / all missing → stop cleanly
                break
            # Defence in depth: re-validate immediately before spawning so an
            # auto-advanced favorite that escapes the music root (stale-root or
            # hand-edited path) is skipped, not played — matching play_path().
            if is_playable is not None and not is_playable(entry.get("path", "")):
                logger.warning("playbest: skipping unplayable favorite path=%s", entry.get("path"))
                continue
            try:
                pid = _start_song(entry, state_path)
            except Exception:  # noqa: BLE001
                logger.exception("playbest: spawn failed path=%s", entry.get("path"))
                continue
            with self._lock:
                self._current_pid = pid
            if not self.is_active():  # stopped during spawn → don't leave it playing
                _terminate(pid)
                break
            if on_play is not None:
                try:
                    on_play(entry)
                except Exception:  # noqa: BLE001
                    logger.exception("playbest: on_play failed")
            while self.is_active() and _pid_alive(pid):
                time.sleep(_PLAYBEST_POLL_SECONDS)
            if not self.is_active():
                break
        with self._lock:
            self._active = False


_PLAYBEST = PlaybestController()


def _play_random_continuous(music_dir: str, index_path: str, state_path: str) -> str:
    """Continuous shuffled playback over the WHOLE library — the same controller
    that drives playbest, just sourcing every indexed song instead of favorites.
    Auto-advances to the next random track when the current one ends; /music stop
    halts it. The provider re-reads the index each round (cache hit when nothing
    changed) so songs added mid-session are picked up."""
    def provider() -> list[dict]:
        try:
            return load_or_build_index(music_dir, index_path).entries
        except Exception:  # noqa: BLE001
            logger.exception("music: random index build failed dir=%s", music_dir)
            return []

    if not provider():
        return f"音樂資料夾找不到可播放的音檔（.flac）：{music_dir}"

    def is_playable(path: str) -> bool:
        return validate_song_path(path, music_dir)

    _PLAYBEST.start(
        entries_provider=provider,
        state_path=state_path,
        is_playable=is_playable,
    )
    return "開始隨機播放（自動接續下一首）。用 ⏹ 停止 可停止。"


def _play_best(store: FavoritesStore, state_path: str, music_dir: str) -> str:
    favorites = store.list()
    if not favorites:
        return "最愛清單是空的，無法開始連續播放。"

    # Favorites are persisted paths that may predate a music-root change or be
    # hand-edited, so playbest must apply the SAME root/suffix/sidecar check as
    # the single-play callback (play_path) — never just os.path.exists, which
    # would let a still-present file outside OPENCLAW_MUSIC_DIR be played.
    def is_playable(path: str) -> bool:
        return validate_song_path(path, music_dir)

    playable = [e for e in favorites if is_playable(e.get("path", ""))]
    if not playable:
        return "最愛清單中沒有可播放的歌曲（檔案不存在、不是 .flac、或已不在音樂資料夾內）。"
    _PLAYBEST.start(
        entries_provider=store.list,
        state_path=state_path,
        on_play=lambda e: store.mark_played(e.get("id", "")),
        is_playable=is_playable,
    )
    return "開始連續隨機播放最愛歌曲。用 /music stop 可停止。"


# --- shared play entry points (used by handler + Telegram callbacks) -------
def play_random(settings: AssistantSettings) -> str:
    """Start continuous shuffled playback over the whole library (auto-advances
    to the next random song, like playbest). Returns a user-facing message."""
    music_dir = settings.openclaw_music_dir
    err = _music_dir_problem(music_dir)
    if err:
        return err
    return _play_random_continuous(
        music_dir,
        settings.openclaw_music_index_path,
        settings.openclaw_music_player_state_path,
    )


def play_path(settings: AssistantSettings, path: str, name: str | None = None) -> str:
    """Validate and play a specific song path (for callback buttons). Rejects
    anything that is not a real ``.flac`` under the music root."""
    if not validate_song_path(path, settings.openclaw_music_dir):
        return "這個檔案無法播放（不存在、不是 .flac、或不在音樂資料夾內）。"
    entry = {"path": os.path.abspath(path), "name": name or Path(path).stem}
    return _play(entry, settings.openclaw_music_player_state_path)


def stop_playback(settings: AssistantSettings) -> str:
    return _stop(settings.openclaw_music_player_state_path)


def start_playbest(settings: AssistantSettings, store: FavoritesStore) -> str:
    return _play_best(
        store, settings.openclaw_music_player_state_path, settings.openclaw_music_dir
    )


def add_current_to_favorites(settings: AssistantSettings, store: FavoritesStore) -> str:
    """`/musicnowbest`: add the currently playing OpenClaw song to favorites."""
    running = _current_running(settings.openclaw_music_player_state_path)
    if running is None:
        return "目前沒有播放中的音樂，無法加入最愛。"
    path = running.get("path")
    name = running.get("name") or (Path(path).stem if path else "")
    if not path:
        return "目前播放狀態缺少檔案路徑，無法加入最愛。"
    added, entry = store.add(path, name)
    if added:
        return f"已加入最愛：\n{entry['name']}"
    return f"這首已經在最愛清單中：\n{entry['name']}"


def now_playing(settings: AssistantSettings) -> str | None:
    """Name of the song OpenClaw is currently playing, or None when nothing is
    playing. Reads the persisted player state (verified live) so it reflects both
    single plays and continuous random/playbest."""
    running = _current_running(settings.openclaw_music_player_state_path)
    if running is None:
        return None
    name = running.get("name")
    if name:
        return name
    path = running.get("path")
    return Path(path).stem if path else None


def _music_dir_problem(music_dir: str) -> str | None:
    root = Path(music_dir)
    if not root.exists():
        return f"找不到音樂資料夾：{music_dir}"
    if not root.is_dir():
        return f"音樂路徑不是資料夾：{music_dir}"
    return None


# --- command handler -------------------------------------------------------
def build_music_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], "str | tuple[str, dict]"]:
    music_dir = settings.openclaw_music_dir
    index_path = settings.openclaw_music_index_path
    state_path = settings.openclaw_music_player_state_path
    store = FavoritesStore(settings.openclaw_music_best_path)

    def handler(raw: str, chat_id: str) -> "str | tuple[str, dict]":
        arg = (raw or "").strip()
        if not arg:
            return _menu_text(), _menu_markup()
        low = arg.lower()
        if low == "stop":
            return _stop(state_path)
        if low == "playbest":
            return _play_best(store, state_path, music_dir)

        problem = _music_dir_problem(music_dir)
        if problem:
            return problem
        try:
            index = load_or_build_index(music_dir, index_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("music: index build failed dir=%s", music_dir)
            return f"音樂索引建立失敗：{exc}"
        if not index.entries:
            return f"音樂資料夾找不到可播放的音檔（.flac）：{music_dir}"

        if low == "random":
            return _play_random_continuous(music_dir, index_path, state_path)
        result = _search(index.entries, arg)
        if result.kind in ("exact", "single"):
            return _play(result.entry, state_path)
        if result.kind == "ambiguous":
            return _candidates_text(arg, result.candidates)
        return f"找不到符合「{arg}」的歌曲。"

    return handler


def build_musicnowbest_handler(settings: AssistantSettings) -> Callable[[str, str], str]:
    store = FavoritesStore(settings.openclaw_music_best_path)

    def handler(raw: str, chat_id: str) -> str:
        return add_current_to_favorites(settings, store)

    return handler


def _candidates_text(query: str, candidates: tuple[dict, ...]) -> str:
    lines = [f"找到多首符合「{query}」的歌曲，請輸入更精確的名稱："]
    lines.extend(f"{i}. {e['name']}" for i, e in enumerate(candidates, 1))
    return "\n".join(lines)


def _menu_text() -> str:
    return (
        "🎵 音樂控制\n"
        "也可直接輸入：/music random、/music <歌曲名>、/music stop、/music playbest\n"
        "音量：/musicmute、/musiclouder、/musiclower"
    )


def _menu_markup() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🔀 隨機播放", "callback_data": "music:rnd"},
                {"text": "⏹ 停止", "callback_data": "music:stop"},
            ],
            [{"text": "📂 瀏覽全部歌曲", "callback_data": "music:ls:root:0"}],
            [
                {"text": "🎶 最愛清單", "callback_data": "pg:mb:0:r"},
                {"text": "▶️ 播放最愛", "callback_data": "music:pb"},
            ],
            [{"text": "⭐ 收藏目前歌曲", "callback_data": "music:now"}],
            [
                {"text": "🔇 靜音", "callback_data": "music:mute"},
                {"text": "🔉 音量降低", "callback_data": "music:lower"},
                {"text": "🔊 音量提高", "callback_data": "music:louder"},
            ],
        ]
    }
