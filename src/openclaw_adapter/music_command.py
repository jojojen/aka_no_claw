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
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
import uuid
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


# A freshly-spawned afplay that cannot read its file — vanished file, or an
# external drive that is asleep/unmounted/spinning up — exits within ~0.1s.
# _spawn_player still returns its (already-dead) pid, so without this check we
# would record the song as "playing" and report 正在播放 while no sound plays.
_PLAYBACK_VERIFY_SECONDS = 0.3
_PLAYBACK_VERIFY_POLL = 0.05


def _verify_playing(pid: int) -> bool:
    """True iff the just-spawned afplay is still our live player after a brief
    moment — i.e. it actually started decoding rather than dying instantly on an
    unreadable file. Returns early as soon as the pid is seen dead."""
    deadline = time.monotonic() + _PLAYBACK_VERIFY_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return False
        time.sleep(_PLAYBACK_VERIFY_POLL)
    return _pid_alive(pid) and _pid_is_player(pid)


# Switching tracks kills the previous afplay then immediately spawns the next;
# CoreAudio may not have released the output device yet, so the new afplay can
# die on startup with "AudioQueueStart failed". A short bounded retry rides out
# that transient instead of leaving the user with silence.
_PLAYBACK_SPAWN_ATTEMPTS = 3
_PLAYBACK_RETRY_SECONDS = 0.25
# afplay dies within ~0.1s on an unreadable file / unusable output device, so a
# short blocking probe is enough to recover the real reason the detached
# (DEVNULL) spawn discards.
_PROBE_TIMEOUT_SECONDS = 3


def _safe_output_device() -> "tuple[str | None, str | None]":
    """``(device_name, error)`` for the current macOS output device, best-effort.
    Never raises — health/diagnostic callers degrade gracefully when
    SwitchAudioSource is missing instead of crashing the command."""
    try:
        from .music_audio_device import current_output_device

        return current_output_device(), None
    except Exception as exc:  # noqa: BLE001 — missing binary / failure → unknown
        return None, str(exc)


def _recover_wedged_output() -> bool:
    """Best-effort clear of a wedged CoreAudio output (the ``-66681`` /
    "AudioQueueStart failed" HAL state) by restarting coreaudiod, so the next
    spawn attempt can actually start. Never raises."""
    try:
        from .audio_recovery import restart_coreaudiod

        return restart_coreaudiod()
    except Exception:  # noqa: BLE001 — recovery is best-effort, never fatal
        return False


def _probe_spawn_failure(path: str) -> str | None:
    """Run ONE bounded blocking afplay to capture the real failure reason
    (stderr / return code) after every detached attempt died. Returns a short
    diagnostic string, or ``None`` when no clear reason is available."""
    try:
        proc = subprocess.run(
            [_PLAYER_BINARY, path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None  # it actually kept playing this time — no failure to report
    except OSError as exc:
        return f"afplay 無法執行：{exc}"
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "").strip()
    return detail or f"afplay 回傳碼 {proc.returncode}"


class PlaybackSpawnError(RuntimeError):
    """Every spawn attempt for a track died on startup. Carries structured
    diagnostics (attempt count, current output device, captured reason) so the
    failure says WHY — an unreadable file vs. an unusable/busy audio device —
    instead of a generic line, and so logs can record the same fields."""

    def __init__(
        self,
        *,
        path: str,
        attempts: int,
        output_device: str | None,
        device_error: str | None,
        reason: str | None,
    ) -> None:
        self.path = path
        self.attempts = attempts
        self.output_device = output_device
        self.device_error = device_error
        self.reason = reason
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        parts = [f"afplay 連續 {self.attempts} 次啟動失敗"]
        if self.reason:
            parts.append(f"原因：{self.reason}")
        if self.output_device:
            parts.append(f"目前輸出裝置：{self.output_device}")
        elif self.device_error:
            parts.append(f"輸出裝置未知：{self.device_error}")
        parts.append(
            "（檔案可能無法讀取，或音訊輸出裝置忙碌／不可用——"
            "可用 /music 的音源選單切換到可用裝置）"
        )
        return "；".join(parts)


def _spawn_with_retry(path: str, *, allow_recovery: bool = True) -> int:
    """Spawn afplay and confirm it actually started, retrying a couple of times
    to absorb the transient audio-device-busy race. Returns the live pid, or
    raises :class:`PlaybackSpawnError` (with diagnostics) if every attempt died
    on startup (genuinely unreadable file / no usable audio device).

    If all quick attempts fail (the ``-66681`` CoreAudio wedge — quick retries
    can't clear it), restart coreaudiod once and try one more full pass before
    giving up. ``allow_recovery=False`` on the recursive pass bounds it to a
    single daemon restart per play."""
    for attempt in range(_PLAYBACK_SPAWN_ATTEMPTS):
        pid = _spawn_player(path)
        if _verify_playing(pid):
            return pid
        _terminate(pid)  # dead/zombie afplay — never leave it around
        if attempt + 1 < _PLAYBACK_SPAWN_ATTEMPTS:
            time.sleep(_PLAYBACK_RETRY_SECONDS)
    if allow_recovery and _recover_wedged_output():
        return _spawn_with_retry(path, allow_recovery=False)
    device, device_error = _safe_output_device()
    raise PlaybackSpawnError(
        path=path,
        attempts=_PLAYBACK_SPAWN_ATTEMPTS,
        output_device=device,
        device_error=device_error,
        reason=_probe_spawn_failure(path),
    )


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


# --- continuous-mode failure record ---------------------------------------
# When continuous playback stops because every spawn keeps failing (output
# device gone, external drive asleep…), the loop runs with no chat to report to.
# It records the reason next to the player state so /music now and /musicdiag can
# show WHY the music stopped instead of leaving the user guessing.
def _failure_state_path(state_path: str) -> str:
    return str(Path(state_path).with_name("music_playback_failure.json"))


def _write_failure(state_path: str, reason: str, count: int) -> None:
    _write_json(
        _failure_state_path(state_path),
        {
            "reason": reason,
            "count": count,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def _read_failure(state_path: str) -> dict | None:
    data = _read_json(_failure_state_path(state_path))
    return data if isinstance(data, dict) and data.get("reason") else None


def _clear_failure(state_path: str) -> None:
    try:
        Path(_failure_state_path(state_path)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear playback failure record %s", state_path)


# --- voice-interruption resume marker (issue #47) --------------------------
# When /saynow needs the speakers, it stops the current music and writes this
# marker (mode + track + a one-shot epoch). After the announcement it resumes
# the SAME playback — UNLESS the marker is gone (the user ran /music stop during
# the interruption) or a newer epoch superseded it. Lives next to the player
# state so the decision survives across the bot and the LAN command-bridge.
def _interrupt_path(state_path: str) -> str:
    return str(Path(state_path).with_name("music_interrupt.json"))


def _write_interrupt(state_path: str, data: dict) -> None:
    _write_json(_interrupt_path(state_path), data)


def _read_interrupt(state_path: str) -> dict | None:
    data = _read_json(_interrupt_path(state_path))
    return data if isinstance(data, dict) and data.get("epoch") else None


def _clear_interrupt(state_path: str) -> None:
    try:
        Path(_interrupt_path(state_path)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear interrupt marker %s", state_path)


# --- playback health probe -------------------------------------------------
HEALTH_NO_PLAYBACK = "no_playback"
HEALTH_PLAYING = "playing"
HEALTH_STALE_DEAD = "stale_dead"  # recorded process no longer alive
HEALTH_PID_REUSE = "pid_reuse"  # pid alive but not our afplay (reused identity)
HEALTH_MISSING_FILE = "missing_file"  # recorded as playing but the file vanished


@dataclass(frozen=True)
class PlaybackHealth:
    """A read-only snapshot of whether OpenClaw playback is actually healthy,
    crossing the boundaries that make Mac mini playback fragile: process
    liveness/identity, the track file, the music root, and the output device."""

    status: str
    has_state: bool
    pid: int | None
    pid_alive: bool
    is_player: bool
    identity_ok: bool
    track_name: str | None
    track_path: str | None
    track_exists: bool
    music_root: str
    music_root_ok: bool
    output_device: str | None
    output_device_error: str | None
    last_failure: dict | None


def playback_health(settings: AssistantSettings) -> PlaybackHealth:
    """Probe current playback health without mutating state (so it is safe to
    call from diagnostics). Distinguishes healthy playing, stale/dead process,
    pid reuse, a vanished current file, a missing music root, and no playback."""
    state_path = settings.openclaw_music_player_state_path
    music_root = settings.openclaw_music_dir
    root_ok = Path(music_root).is_dir()
    device, device_err = _safe_output_device()
    last_failure = _read_failure(state_path)
    state = _read_json(state_path)
    pid = state.get("pid") if isinstance(state, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return PlaybackHealth(
            status=HEALTH_NO_PLAYBACK, has_state=bool(state), pid=None,
            pid_alive=False, is_player=False, identity_ok=False,
            track_name=None, track_path=None, track_exists=False,
            music_root=music_root, music_root_ok=root_ok,
            output_device=device, output_device_error=device_err,
            last_failure=last_failure,
        )
    name = state.get("name")
    path = state.get("path")
    track_exists = bool(path) and Path(path).is_file()
    alive = _pid_alive(pid)
    is_player = alive and _pid_is_player(pid)
    identity_ok = (
        is_player and bool(state.get("start")) and state.get("start") == _pid_start_time(pid)
    )
    if not alive:
        status = HEALTH_STALE_DEAD
    elif not is_player or not identity_ok:
        status = HEALTH_PID_REUSE
    elif not track_exists:
        status = HEALTH_MISSING_FILE
    else:
        status = HEALTH_PLAYING
    return PlaybackHealth(
        status=status, has_state=True, pid=pid, pid_alive=alive,
        is_player=is_player, identity_ok=identity_ok, track_name=name,
        track_path=path, track_exists=track_exists, music_root=music_root,
        music_root_ok=root_ok, output_device=device,
        output_device_error=device_err, last_failure=last_failure,
    )


def _format_health_text(h: PlaybackHealth) -> str:
    lines: list[str] = []
    if h.status == HEALTH_PLAYING:
        name = h.track_name or (Path(h.track_path).stem if h.track_path else "?")
        lines.append(f"▶️ 正在播放：{name}")
    elif h.status == HEALTH_NO_PLAYBACK:
        lines.append("⏹ 目前沒有由龍蝦播放中的音樂。")
    elif h.status == HEALTH_STALE_DEAD:
        lines.append("⚠️ 播放狀態殘留：紀錄的程序已結束（視為未播放，下次操作會自動清除）。")
    elif h.status == HEALTH_PID_REUSE:
        lines.append("⚠️ 播放狀態殘留：pid 已被系統重用為其他程序（不會誤殺，視為未播放）。")
    elif h.status == HEALTH_MISSING_FILE:
        lines.append(f"⚠️ 仍記為播放中，但檔案已不存在：{h.track_path}")
    if h.output_device:
        lines.append(f"輸出裝置：{h.output_device}")
    elif h.output_device_error:
        lines.append(f"輸出裝置未知：{h.output_device_error}")
    if not h.music_root_ok:
        lines.append(f"⚠️ 音樂資料夾不可用（外接碟可能未掛載／休眠）：{h.music_root}")
    if h.last_failure and h.status != HEALTH_PLAYING:
        lines.append(
            f"上次連續播放停止原因（連續 {h.last_failure.get('count')} 次失敗）："
            f"{h.last_failure.get('reason')}"
        )
    return "\n".join(lines)


# --- continuous-mode session (cross-process) -------------------------------
# Continuous playback (playbest / random) runs as an in-memory loop in WHICHEVER
# process started it — but the Telegram poller and the LAN command-bridge are
# SEPARATE processes, each with their own _PLAYBEST. So a /music stop issued in
# one process cannot reach the other's loop, and the music "resumes" right after
# a stop (the other loop auto-advances). The fix: persist a tiny session token
# next to the player state. start() claims it; every loop checks it each tick and
# halts the instant the token is gone or replaced; /music stop and single-play
# delete it, signalling EVERY process's loop to stop.
def _session_path(state_path: str) -> str:
    return str(Path(state_path).with_name("music_playbest_session.json"))


def _write_session(state_path: str, token: str) -> None:
    _write_json(_session_path(state_path), {"token": token})


def _read_session_token(state_path: str) -> str | None:
    data = _read_json(_session_path(state_path))
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    return token if isinstance(token, str) else None


def _clear_session(state_path: str) -> None:
    try:
        Path(_session_path(state_path)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear playbest session for %s", state_path)


# --- queue navigation (#60): previous / next within a continuous queue -------
# The continuous loop is forward-only; #60's "lighter" navigation layers a tiny
# two-slot history (prev/current) and a one-shot "forced next" file on top of it.
# `next` simply ends the current track so the loop auto-advances; `previous`
# writes the prior track as the forced next so the loop replays it, after which
# forward playback resumes by reshuffle. Both files live next to the player state
# so the SAME signal crosses the Telegram-poller / command-bridge process split,
# exactly like the session token.
_NO_QUEUE_MSG = (
    "目前沒有播放清單。\n請先使用：\n- /music playbest\n- 全部隨機播放"
)


def _nav_path(state_path: str) -> str:
    return str(Path(state_path).with_name("music_playback_nav.json"))


def _read_nav(state_path: str) -> dict:
    data = _read_json(_nav_path(state_path))
    return data if isinstance(data, dict) else {}


def _shift_nav(state_path: str, entry: dict) -> None:
    """Record ``entry`` as the current track, demoting the old current to prev."""
    nav = _read_nav(state_path)
    _write_json(
        _nav_path(state_path),
        {"prev": nav.get("current"), "current": {"name": entry.get("name"), "path": entry.get("path")}},
    )


def _clear_nav(state_path: str) -> None:
    try:
        Path(_nav_path(state_path)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear playback nav for %s", state_path)


def _forced_path(state_path: str) -> str:
    return str(Path(state_path).with_name("music_playback_forced.json"))


def _read_forced(state_path: str) -> dict | None:
    data = _read_json(_forced_path(state_path))
    if isinstance(data, dict) and data.get("path"):
        return data
    return None


def _write_forced(state_path: str, entry: dict) -> None:
    _write_json(
        _forced_path(state_path),
        {"name": entry.get("name"), "path": entry.get("path")},
    )


def _clear_forced(state_path: str) -> None:
    try:
        Path(_forced_path(state_path)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("music: could not clear forced track for %s", state_path)


def _has_active_queue(state_path: str) -> bool:
    """A navigable queue exists only while a continuous session (playbest/random)
    is running — the shared session token is its marker."""
    return _read_session_token(state_path) is not None


def _set_paused(state_path: str, paused: bool) -> None:
    state = _read_json(state_path)
    if not isinstance(state, dict):
        return
    state["paused"] = paused
    _write_json(state_path, state)


def _signal_pid(pid: int, sig: int) -> bool:
    """Send ``sig`` to ``pid``; return False if the process is already gone.
    Module-level so tests can stub it alongside the other process primitives."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False


def _continue_if_paused(state_path: str, running: dict) -> None:
    """SIGCONT a paused track before terminating it, so a queued SIGTERM is not
    held until the process is resumed (a stopped process never sees SIGTERM)."""
    if not running.get("paused"):
        return
    try:
        _signal_pid(int(running["pid"]), signal.SIGCONT)
    except (ValueError, KeyError):
        pass


def _pause(state_path: str) -> str:
    running = _current_running(state_path)
    if running is None:
        return "目前沒有播放中的音樂。"
    if not _signal_pid(int(running["pid"]), signal.SIGSTOP):
        return "目前沒有播放中的音樂。"
    _set_paused(state_path, True)
    return "⏸ 已暫停播放。"


def _resume(state_path: str) -> str:
    running = _current_running(state_path)
    if running is None:
        return "目前沒有可繼續播放的音樂。"
    if not _signal_pid(int(running["pid"]), signal.SIGCONT):
        return "目前沒有可繼續播放的音樂。"
    _set_paused(state_path, False)
    return "▶️ 已繼續播放。"


def _toggle_pause(state_path: str) -> str:
    """One-button ⏯: resume if currently paused, otherwise pause."""
    running = _current_running(state_path)
    if running is None:
        return "目前沒有播放中的音樂。"
    return _resume(state_path) if running.get("paused") else _pause(state_path)


def _next(state_path: str) -> str:
    if not _has_active_queue(state_path):
        return _NO_QUEUE_MSG
    running = _current_running(state_path)
    if running is None:
        # Token present but nothing playing: the loop will advance on its own.
        return "⏭ 已跳到下一首。"
    _continue_if_paused(state_path, running)
    _terminate(int(running["pid"]))  # loop auto-advances to the next track
    return "⏭ 已跳到下一首。"


def _previous(state_path: str) -> str:
    if not _has_active_queue(state_path):
        return _NO_QUEUE_MSG
    prev = _read_nav(state_path).get("prev")
    if not prev or not prev.get("path"):
        return "已經是第一首了，沒有上一首。"
    _write_forced(state_path, prev)  # loop replays this instead of reshuffling
    running = _current_running(state_path)
    if running is not None:
        _continue_if_paused(state_path, running)
        _terminate(int(running["pid"]))
    return f"⏮ 回到上一首：\n{prev.get('name', '')}"


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


def _start_song(entry: dict, state_path: str, mode: str = "single") -> int:
    """Stop the previous track, spawn ``entry``, persist its identity, return
    the pid. Shared by user-initiated plays and the playbest loop; callers that
    are *not* the playbest loop must stop playbest first (see :func:`_play`).

    ``mode`` (single / random / playbest) is recorded next to the pid so a voice
    interruption can later resume the SAME kind of playback (issue #47)."""
    _terminate_running(state_path)
    pid = _spawn_with_retry(entry["path"])
    _write_json(
        state_path,
        {
            "pid": pid,
            "name": entry["name"],
            "path": entry["path"],
            "start": _pid_start_time(pid),
            "mode": mode,
        },
    )
    _clear_failure(state_path)  # a healthy spawn clears any stale failure record
    return pid


def _stop(state_path: str) -> str:
    # /music stop ends both the current song AND any continuous mode (random or
    # playbest), and it must not auto-restart after. Clearing the shared session
    # token FIRST halts the continuous loop in EVERY process (incl. the LAN
    # command-bridge), not just this one — otherwise that loop auto-advances to
    # the next song the moment we kill the current track.
    had_session = _read_session_token(state_path) is not None
    _clear_session(state_path)
    _clear_nav(state_path)  # #60: a stop ends the navigable queue
    _clear_forced(state_path)
    _clear_failure(state_path)  # a manual stop resets any continuous-failure record
    # A manual stop also cancels any pending voice-interruption resume: if the
    # user deliberately stopped during/after a /saynow, music must NOT come back.
    _clear_interrupt(state_path)
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
    if was_continuous or had_session:
        return "已停止連續播放。"
    return "目前沒有由龍蝦播放中的音樂。"


def _play(entry: dict, state_path: str) -> str:
    # A user-initiated single play cancels playbest so the loop never fights it
    # for the player (and never leaves two OpenClaw songs playing at once). Clear
    # the shared session too, so a continuous loop in another process also stops.
    _clear_session(state_path)
    _clear_nav(state_path)  # #60: a single play is queue-less; drop any queue nav
    _clear_forced(state_path)
    _PLAYBEST.stop()
    try:
        _start_song(entry, state_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("music: playback failed path=%s", entry.get("path"))
        return f"播放失敗：{exc}"
    return f"正在播放：\n{entry['name']}"


# --- playbest: continuous shuffled favorite playback -----------------------
_PLAYBEST_POLL_SECONDS = 1.0
# Continuous mode skips a single bad track, but after this many *consecutive*
# spawn failures it stops instead of spinning forever on a broken environment
# (output device unavailable, external drive asleep/unmounted). Matches the
# per-track _PLAYBACK_SPAWN_ATTEMPTS so a transient device-busy race never trips
# it, only a sustained failure does.
_MAX_CONSECUTIVE_SPAWN_FAILURES = 3


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

    def start(
        self, entries_provider, state_path: str, on_play=None, is_playable=None,
        mode: str = "playbest",
    ) -> None:
        self.stop()
        # Claim the shared session: this token is what the loop checks each tick.
        # A later start() (even in another process) overwrites it, and a stop()
        # deletes it — either way every running loop sees the change and halts.
        token = uuid.uuid4().hex
        _write_session(state_path, token)
        # #60: a fresh continuous session starts with an empty navigation history
        # and no leftover forced track from a prior session.
        _clear_nav(state_path)
        _clear_forced(state_path)
        with self._lock:
            self._active = True
            thread = threading.Thread(
                target=self._loop,
                args=(entries_provider, state_path, on_play, is_playable, token, mode),
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

    def _loop(self, entries_provider, state_path: str, on_play, is_playable=None, token=None, mode: str = "playbest") -> None:
        scheduler = PlaybestScheduler(entries_provider, exists_fn=is_playable)
        consecutive_failures = 0

        def _live() -> bool:
            # Run only while locally active AND we still own the shared session.
            # A stop() (any process) deletes the token; a newer start() replaces
            # it — both make this False so the loop halts and stops auto-advancing.
            return self.is_active() and (token is None or _read_session_token(state_path) == token)

        while _live():
            # #60: a `/music previous` writes the prior track as a one-shot forced
            # next; consume it before the scheduler so the loop replays it instead
            # of reshuffling forward. A forced replay does not shift the nav slots,
            # so navigation stays single-level (replay last, then resume forward).
            forced = _read_forced(state_path)
            if forced is not None:
                _clear_forced(state_path)
                entry: dict | None = forced
                is_forced = True
            else:
                entry = scheduler.next()
                is_forced = False
            if entry is None:  # favorites empty / all missing → stop cleanly
                break
            # Defence in depth: re-validate immediately before spawning so an
            # auto-advanced favorite that escapes the music root (stale-root or
            # hand-edited path) is skipped, not played — matching play_path().
            if is_playable is not None and not is_playable(entry.get("path", "")):
                logger.warning("playbest: skipping unplayable favorite path=%s", entry.get("path"))
                continue
            # Spawn + record the pid while holding the lock so a concurrent
            # stop() can never slip between "song ended → auto-advance spawns
            # next" and "pid recorded": stop() blocks on the same lock, then
            # sees the just-recorded pid and kills it. Without this, an
            # auto-advanced track started in that gap escapes /music stop and
            # the music "resumes" right after a stop.
            try:
                with self._lock:
                    if not self._active:  # stopped before we could spawn
                        break
                    pid = _start_song(entry, state_path, mode=mode)
                    self._current_pid = pid
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.exception(
                    "playbest: spawn failed (%d/%d) path=%s",
                    consecutive_failures, _MAX_CONSECUTIVE_SPAWN_FAILURES,
                    entry.get("path"),
                )
                # Bounded recovery: skip one bad track, but stop spinning forever
                # when the environment is broken (output device gone, drive
                # asleep). Record the reason so /music now & /musicdiag explain it.
                if consecutive_failures >= _MAX_CONSECUTIVE_SPAWN_FAILURES:
                    reason = str(exc)
                    _write_failure(state_path, reason, consecutive_failures)
                    _clear_session(state_path)
                    logger.error(
                        "playbest: stopping continuous mode after %d consecutive "
                        "spawn failures; last reason: %s",
                        consecutive_failures, reason,
                    )
                    break
                continue
            consecutive_failures = 0
            if not is_forced:
                _shift_nav(state_path, entry)
            if on_play is not None:
                try:
                    on_play(entry)
                except Exception:  # noqa: BLE001
                    logger.exception("playbest: on_play failed")
            while _live() and _pid_alive(pid):
                time.sleep(_PLAYBEST_POLL_SECONDS)
            if not _live():  # stopped (here or cross-process) → leave nothing playing
                _terminate(pid)
                _clear_state(state_path)
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
        mode="random",
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
        mode="playbest",
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


def pause_playback(settings: AssistantSettings) -> str:
    return _pause(settings.openclaw_music_player_state_path)


def resume_playback(settings: AssistantSettings) -> str:
    return _resume(settings.openclaw_music_player_state_path)


def toggle_pause(settings: AssistantSettings) -> str:
    return _toggle_pause(settings.openclaw_music_player_state_path)


def next_track(settings: AssistantSettings) -> str:
    return _next(settings.openclaw_music_player_state_path)


def previous_track(settings: AssistantSettings) -> str:
    return _previous(settings.openclaw_music_player_state_path)


@dataclass(frozen=True)
class ResumeToken:
    """A narrow snapshot of what was playing when a voice interruption took the
    speakers, so ``/saynow`` can resume the SAME playback afterwards without
    knowing any music internals (issue #47). ``epoch`` is a one-shot id matched
    against the on-disk interrupt marker: a manual ``/music stop`` deletes the
    marker, so resume becomes a no-op."""

    mode: str  # single / random / playbest
    track_path: str | None
    track_name: str | None
    epoch: str


def acquire_audio_session(
    settings: AssistantSettings, *, reason: str = ""
) -> "ResumeToken | None":
    """The one shared primitive for taking exclusive use of the Mac mini's audio
    output. The Bluetooth/AirPlay sink can't mix two ``afplay`` streams, so any
    consumer about to play (e.g. ``/saynow`` voice) must free the device through
    THIS call rather than each caller separately remembering to stop music.

    Snapshots healthy playback into a :class:`ResumeToken` (and records an
    interrupt marker) BEFORE stopping it, so the caller can later
    :func:`resume_after_voice`. Returns ``None`` when nothing healthy was
    playing (nothing to resume). Stopping is best-effort and never raises."""
    state_path = settings.openclaw_music_player_state_path
    logger.info(
        "audio-session: acquiring exclusive output%s",
        f" for {reason}" if reason else "",
    )
    token = _snapshot_resume_token(settings)
    try:
        _stop(state_path)  # frees the device + clears any stale interrupt marker
    except Exception:  # noqa: BLE001 — never let freeing the device crash the caller
        logger.exception("audio-session: failed to stop music before exclusive audio")
    if token is not None:
        _write_interrupt(
            state_path,
            {
                "epoch": token.epoch,
                "reason": reason,
                "mode": token.mode,
                "path": token.track_path,
                "name": token.track_name,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    return token


def _snapshot_resume_token(settings: AssistantSettings) -> "ResumeToken | None":
    """Capture the current playback as a resume token, or ``None`` when nothing
    is healthily playing (a stale/dead/reused pid is not worth resuming)."""
    health = playback_health(settings)
    if health.status != HEALTH_PLAYING:
        return None
    state = _read_json(settings.openclaw_music_player_state_path)
    mode = state.get("mode") if isinstance(state, dict) else None
    return ResumeToken(
        mode=mode or "single",
        track_path=health.track_path,
        track_name=health.track_name,
        epoch=uuid.uuid4().hex,
    )


def resume_after_voice(
    settings: AssistantSettings, token: "ResumeToken | None"
) -> bool:
    """Resume the playback captured by :func:`acquire_audio_session` after a voice
    announcement finishes. No-ops (returns ``False``) when there is no token, the
    interrupt marker is gone (user ran ``/music stop`` meanwhile), or a newer
    epoch superseded it. Returns ``True`` only when it actually restarted music."""
    if token is None:
        return False
    state_path = settings.openclaw_music_player_state_path
    marker = _read_interrupt(state_path)
    if marker is None or marker.get("epoch") != token.epoch:
        return False  # user stopped during the interruption, or it was superseded
    _clear_interrupt(state_path)
    try:
        if token.mode == "random":
            play_random(settings)
        elif token.mode == "playbest":
            start_playbest(settings, FavoritesStore(settings.openclaw_music_best_path))
        elif token.track_path:
            play_path(settings, token.track_path, token.track_name)
        else:
            return False
    except Exception:  # noqa: BLE001 — a failed resume must not crash the caller
        logger.exception("audio-session: failed to resume music after voice")
        return False
    return True


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
        if low == "pause":
            return _pause(state_path)
        if low == "resume":
            return _resume(state_path)
        if low == "next":
            return _next(state_path)
        if low in ("previous", "prev"):
            return _previous(state_path)
        if low == "now":
            return _format_health_text(playback_health(settings))
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


def _musicdiag_text(settings: AssistantSettings) -> str:
    """One-shot Mac mini music diagnostic (issue #47): tool availability, current
    output device, music-root status, and live playback health. Read-only — it
    never mutates system power settings or playback state."""
    lines = ["🩺 音樂診斷（Mac mini）"]
    afplay = shutil.which(_PLAYER_BINARY)
    lines.append(f"afplay：{('可用 ' + afplay) if afplay else '找不到（此功能僅支援 macOS）'}")

    device, device_err = _safe_output_device()
    if device is not None:
        lines.append("SwitchAudioSource：可用")
        lines.append(f"目前輸出裝置：{device}")
    else:
        lines.append(f"SwitchAudioSource／輸出裝置：不可用（{device_err}）")

    root = settings.openclaw_music_dir
    rp = Path(root)
    if rp.is_dir():
        lines.append(f"音樂資料夾：可用 {root}")
    elif rp.exists():
        lines.append(f"音樂資料夾：路徑不是資料夾 {root}")
    else:
        lines.append(f"音樂資料夾：找不到 {root}（外接碟可能未掛載／休眠）")

    lines.append("——")
    lines.append(_format_health_text(playback_health(settings)))

    # launchd's PATH omits /opt/homebrew/bin; flag it only when a tool is missing
    # from PATH yet may well be installed via Homebrew.
    if device is None and shutil.which(_BINARY_HINT) is None:
        lines.append(
            "提示：服務在 launchd 下 PATH 不含 /opt/homebrew/bin；"
            "若工具其實已安裝卻找不到，請確認 Homebrew 路徑。"
        )
    return "\n".join(lines)


_BINARY_HINT = "SwitchAudioSource"


def build_musicdiag_handler(settings: AssistantSettings) -> Callable[[str, str], str]:
    def handler(raw: str, chat_id: str) -> str:
        return _musicdiag_text(settings)

    return handler


def _candidates_text(query: str, candidates: tuple[dict, ...]) -> str:
    lines = [f"找到多首符合「{query}」的歌曲，請輸入更精確的名稱："]
    lines.extend(f"{i}. {e['name']}" for i, e in enumerate(candidates, 1))
    return "\n".join(lines)


def _menu_text() -> str:
    return (
        "🎵 音樂控制\n"
        "也可直接輸入：/music random、/music <歌曲名>、/music stop、/music playbest\n"
        "播放控制：/music previous、/music pause、/music resume、/music next\n"
        "（上一首／下一首僅適用於 /music playbest 或全部隨機播放）\n"
        "音量：/musicmute、/musiclouder、/musiclower"
    )


def _menu_markup() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🔀 隨機播放", "callback_data": "music:rnd"},
                {"text": "⏹ 停止", "callback_data": "music:stop"},
            ],
            [
                {"text": "⏮ 上一首", "callback_data": "music:prev"},
                {"text": "⏯ 暫停／繼續", "callback_data": "music:playpause"},
                {"text": "⏭ 下一首", "callback_data": "music:next"},
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
            [{"text": "🔈 切換音源", "callback_data": "music:dev"}],
        ]
    }
