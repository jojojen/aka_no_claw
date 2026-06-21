"""Telegram inline folder browser + favorites callbacks for ``/music`` (#34).

`/musiclistall` walks ``OPENCLAW_MUSIC_DIR`` one directory level at a time using
inline buttons. Telegram caps ``callback_data`` at 64 bytes — far too small for a
full file path — so every folder/song button carries a short *token* and the
token→absolute-path mapping is persisted to a gitignored runtime cache. Every
playback callback re-resolves its token and re-validates the path (must be a real
``.flac`` under the music root) before doing anything.

A single ``music:`` callback prefix multiplexes the actions:

    music:ls:<token>:<page>   browse a folder (token "root" = music root)
    music:sd:<token>          show a song's detail view
    music:play:<token>        play a browsed song
    music:fav:<token>         add a browsed song to favorites
    music:pf:<id>             play a favorite (id is the favorites stable id)
    music:rnd / music:stop / music:pb / music:now   menu shortcuts
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from assistant_runtime import AssistantSettings

from . import music_command as mc
from . import music_volume as mv
from .music_favorites import FavoritesStore, stable_id

logger = logging.getLogger(__name__)

_BROWSE_PAGE_SIZE = 8
_ROOT_TOKEN = "root"
_AUDIO_SUFFIXES = (".flac",)


class TokenCache:
    """Persistent token→absolute-path map (gitignored runtime JSON).

    Tokens are deterministic (a hash of the normalized path) so re-rendering a
    folder reproduces the same token, and the cache survives bot restarts so a
    button clicked long after rendering still resolves."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def put(self, abspath: str) -> str:
        token = stable_id(abspath)
        data = self._load()
        if data.get(token) != os.path.abspath(abspath):
            data[token] = os.path.abspath(abspath)
            self._save(data)
        return token

    def resolve(self, token: str) -> str | None:
        return self._load().get(token)


def _list_dir(directory: Path) -> tuple[list[Path], list[Path]]:
    """Return (subfolders, flac files) directly under ``directory``, sorted,
    excluding AppleDouble sidecars. Hidden dot-folders are skipped."""
    folders: list[Path] = []
    songs: list[Path] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return [], []
    for child in children:
        name = child.name
        if name.startswith("._"):
            continue
        if child.is_dir():
            if not name.startswith("."):
                folders.append(child)
        elif name.lower().endswith(_AUDIO_SUFFIXES):
            songs.append(child)
    return folders, songs


def _resolve_dir_token(token: str, music_dir: str, cache: TokenCache) -> Path | None:
    root = Path(music_dir).resolve()
    if token == _ROOT_TOKEN:
        return root
    resolved = cache.resolve(token)
    if not resolved:
        return None
    path = Path(resolved).resolve()
    if path != root and root not in path.parents:
        return None
    return path if path.is_dir() else None


def _dir_token(path: Path, music_dir: str, cache: TokenCache) -> str:
    if path.resolve() == Path(music_dir).resolve():
        return _ROOT_TOKEN
    return cache.put(str(path))


def build_folder_view(
    settings: AssistantSettings,
    cache: TokenCache,
    *,
    token: str,
    page: int,
) -> tuple[str, dict]:
    """Render one folder level as (text, reply_markup)."""
    music_dir = settings.openclaw_music_dir
    problem = mc._music_dir_problem(music_dir)
    if problem:
        return problem, {"inline_keyboard": []}
    directory = _resolve_dir_token(token, music_dir, cache)
    if directory is None:
        return "找不到這個資料夾（可能已被移動或移除）。", {"inline_keyboard": []}

    folders, songs = _list_dir(directory)
    items: list[tuple[str, str]] = []  # (button_text, callback_data)
    for f in folders:
        items.append((f"📁 {f.name}", f"music:ls:{cache.put(str(f))}:0"))
    for s in songs:
        items.append((f"🎵 {s.stem}", f"music:sd:{cache.put(str(s))}"))

    rows: list[list[dict]] = []
    total = len(items)
    if total == 0:
        body = f"📂 {_display_path(directory, music_dir)}\n（此資料夾沒有子資料夾或 .flac）"
    else:
        total_pages = max(1, (total + _BROWSE_PAGE_SIZE - 1) // _BROWSE_PAGE_SIZE)
        clamped = max(0, min(page, total_pages - 1))
        start = clamped * _BROWSE_PAGE_SIZE
        for text, cb in items[start : start + _BROWSE_PAGE_SIZE]:
            rows.append([{"text": text, "callback_data": cb}])
        body = (
            f"📂 {_display_path(directory, music_dir)}\n"
            f"第 {clamped + 1}/{total_pages} 頁（共 {total} 項）"
        )
        nav: list[dict] = []
        if clamped > 0:
            nav.append({"text": "⬅️ 上頁", "callback_data": f"music:ls:{token}:{clamped - 1}"})
        if clamped < total_pages - 1:
            nav.append({"text": "下頁 ➡️", "callback_data": f"music:ls:{token}:{clamped + 1}"})
        if nav:
            rows.append(nav)

    nav_row = _parent_nav(directory, music_dir, cache)
    if nav_row:
        rows.append(nav_row)
    return body, {"inline_keyboard": rows}


def _parent_nav(directory: Path, music_dir: str, cache: TokenCache) -> list[dict]:
    root = Path(music_dir).resolve()
    if directory.resolve() == root:
        return []
    parent = directory.parent
    parent_token = _dir_token(parent, music_dir, cache)
    return [{"text": "🔙 上一層", "callback_data": f"music:ls:{parent_token}:0"}]


def build_song_detail(
    settings: AssistantSettings, cache: TokenCache, *, token: str
) -> tuple[str, dict]:
    music_dir = settings.openclaw_music_dir
    path = cache.resolve(token)
    if not path or not mc.validate_song_path(path, music_dir):
        return "找不到這首歌（可能已被移動或移除）。", {"inline_keyboard": []}
    song = Path(path)
    parent_token = _dir_token(song.parent, music_dir, cache)
    text = f"🎵 {song.stem}"
    markup = {
        "inline_keyboard": [
            [
                {"text": "▶️ 播放", "callback_data": f"music:play:{token}"},
                {"text": "⭐ 加入最愛", "callback_data": f"music:fav:{token}"},
            ],
            [{"text": "🔙 返回資料夾", "callback_data": f"music:ls:{parent_token}:0"}],
        ]
    }
    return text, markup


def _display_path(directory: Path, music_dir: str) -> str:
    root = Path(music_dir).resolve()
    d = directory.resolve()
    if d == root:
        return "（根目錄）"
    try:
        return str(d.relative_to(root))
    except ValueError:
        return d.name


def build_musiclistall_handler(settings: AssistantSettings):
    cache = TokenCache(settings.openclaw_music_token_cache_path)

    def handler(raw: str, chat_id: str):
        return build_folder_view(settings, cache, token=_ROOT_TOKEN, page=0)

    return handler


def build_music_callback_handler(settings: AssistantSettings):
    """Return the ``music:`` prefix callback handler.

    Signature matches the dispatcher's registry: ``(payload, original_text,
    chat_id) -> (toast, new_text, new_reply_markup)``. ``new_text`` is non-None
    only for actions that re-render the message (folder browse / song detail);
    play/stop/favorite actions reply with just a toast."""
    cache = TokenCache(settings.openclaw_music_token_cache_path)
    store = FavoritesStore(settings.openclaw_music_best_path)

    def cb(payload: str, original_text: str, chat_id: str):
        action, _, rest = payload.partition(":")

        if action == "rnd":
            return mc.play_random(settings), None, None
        if action == "stop":
            return mc.stop_playback(settings), None, None
        if action == "pb":
            return mc.start_playbest(settings, store), None, None
        if action == "now":
            return mc.add_current_to_favorites(settings, store), None, None
        if action == "mute":
            return mv.mute_music(settings), None, None
        if action == "louder":
            return mv.louder_music(settings), None, None
        if action == "lower":
            return mv.lower_music(settings), None, None

        if action == "ls":
            token, _, page_str = rest.partition(":")
            try:
                page = int(page_str)
            except ValueError:
                page = 0
            text, markup = build_folder_view(settings, cache, token=token or _ROOT_TOKEN, page=page)
            return None, text, markup

        if action == "sd":
            text, markup = build_song_detail(settings, cache, token=rest)
            return None, text, markup

        if action == "play":
            path = cache.resolve(rest)
            if not path:
                return "找不到這首歌（可能已被移動或移除）。", None, None
            return mc.play_path(settings, path), None, None

        if action == "fav":
            path = cache.resolve(rest)
            if not path or not mc.validate_song_path(path, settings.openclaw_music_dir):
                return "找不到這首歌（可能已被移動或移除）。", None, None
            added, entry = store.add(path, Path(path).stem)
            return (f"已加入最愛：{entry['name']}" if added else "這首已經在最愛清單中。"), None, None

        if action == "pf":
            entry = store.get(rest)
            if entry is None:
                return "這首已不在最愛清單。", None, None
            return mc.play_path(settings, entry.get("path", ""), entry.get("name")), None, None

        return "未知的音樂動作。", None, None

    return cb
