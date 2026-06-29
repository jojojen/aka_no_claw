"""Issue #34 — /music folder browser, favorites store, and music: callbacks.

Real playback is stubbed via the same module-level process primitives used by
the #33 tests, so these assert UI/control logic: folder browsing, pagination,
back navigation, song detail, the play/add-favorite callbacks, favorite
list/edit/remove, duplicate prevention, and callback path safety — without ever
launching afplay.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openclaw_adapter import music_browser as mb
from openclaw_adapter import music_command as mc
from openclaw_adapter.music_favorites import (
    FavoritesStore,
    build_music_best_item_deleter,
    build_music_best_view_fn,
    stable_id,
)


@pytest.fixture
def music_dir(tmp_path):
    root = tmp_path / "Music"
    (root / "AlbumA").mkdir(parents=True)
    (root / "AlbumB" / "Disc1").mkdir(parents=True)
    for i in range(10):  # 10 songs in AlbumA to force pagination (page size 8)
        (root / "AlbumA" / f"{i:02d} Song.flac").write_bytes(b"x")
    (root / "AlbumB" / "Disc1" / "Deep Track.flac").write_bytes(b"x")
    (root / "AlbumA" / "._hidden.flac").write_bytes(b"junk")  # AppleDouble sidecar
    (root / "AlbumA" / "cover.jpg").write_bytes(b"img")
    return root


@pytest.fixture
def settings(tmp_path, music_dir):
    return SimpleNamespace(
        openclaw_music_dir=str(music_dir),
        openclaw_music_index_path=str(tmp_path / ".t" / "idx.json"),
        openclaw_music_player_state_path=str(tmp_path / ".t" / "state.json"),
        openclaw_music_best_path=str(tmp_path / ".t" / "best.json"),
        openclaw_music_token_cache_path=str(tmp_path / ".t" / "tokens.json"),
    )


@pytest.fixture
def proc_table(monkeypatch):
    state = {"next_pid": 1000, "alive": set(), "spawned": [], "killed": [], "start": {}}

    def _spawn(path):
        state["next_pid"] += 1
        pid = state["next_pid"]
        state["alive"].add(pid)
        state["spawned"].append((pid, path))
        state["start"][pid] = f"start-{pid}"
        return pid

    monkeypatch.setattr(mc, "_spawn_player", _spawn)
    monkeypatch.setattr(mc, "_pid_alive", lambda p: p in state["alive"])
    monkeypatch.setattr(mc, "_pid_is_player", lambda p: p in state["alive"])
    monkeypatch.setattr(mc, "_pid_start_time", lambda p: state["start"].get(p) if p in state["alive"] else None)
    monkeypatch.setattr(mc, "_terminate", lambda p: (state["killed"].append(p), state["alive"].discard(p)))
    yield state
    mc._PLAYBEST.stop()


def _all_cbs(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


# --- folder browsing -------------------------------------------------------
def test_listall_shows_first_level(settings):
    handler = mb.build_musiclistall_handler(settings)
    text, markup = handler("", "c")
    cbs = _all_cbs(markup)
    # two albums at the root, each a folder button; no back button at root
    assert sum(1 for c in cbs if c.startswith("music:ls:")) == 2
    assert not any("🔙" in b["text"] for row in markup["inline_keyboard"] for b in row)


def test_enter_folder_then_back(settings):
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    # enter AlbumB
    albumb = Path(settings.openclaw_music_dir) / "AlbumB"
    token = cache.put(str(albumb))
    text, markup = mb.build_folder_view(settings, cache, token=token, page=0)
    cbs = _all_cbs(markup)
    assert any(c.startswith("music:ls:") for c in cbs)  # Disc1 subfolder
    back = [b for row in markup["inline_keyboard"] for b in row if "🔙" in b["text"]]
    assert back and back[0]["callback_data"] == "music:ls:root:0"  # back to root


def test_pagination_for_long_folder(settings):
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    albuma = Path(settings.openclaw_music_dir) / "AlbumA"
    token = cache.put(str(albuma))
    text, markup = mb.build_folder_view(settings, cache, token=token, page=0)
    cbs = _all_cbs(markup)
    assert "第 1/2 頁" in text  # 10 songs / page size 8 → 2 pages
    assert f"music:ls:{token}:1" in cbs  # next-page button
    # page 2 has the previous-page button
    _, markup2 = mb.build_folder_view(settings, cache, token=token, page=1)
    assert f"music:ls:{token}:0" in _all_cbs(markup2)


def test_excludes_appledouble_and_non_flac(settings):
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    albuma = Path(settings.openclaw_music_dir) / "AlbumA"
    token = cache.put(str(albuma))
    # gather all song tokens across both pages, resolve, ensure none are junk
    paths = []
    for page in (0, 1):
        _, markup = mb.build_folder_view(settings, cache, token=token, page=page)
        for c in _all_cbs(markup):
            if c.startswith("music:sd:"):
                paths.append(cache.resolve(c.split(":", 2)[2]))
    names = {Path(p).name for p in paths}
    assert not any(n.startswith("._") for n in names)
    assert "cover.jpg" not in names
    assert len([n for n in names if n.endswith(".flac")]) == 10


# --- song detail + play/fav callbacks --------------------------------------
def test_song_detail_has_play_and_fav(settings):
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    song = Path(settings.openclaw_music_dir) / "AlbumA" / "00 Song.flac"
    token = cache.put(str(song))
    text, markup = mb.build_song_detail(settings, cache, token=token)
    cbs = _all_cbs(markup)
    assert f"music:play:{token}" in cbs
    assert f"music:fav:{token}" in cbs


def test_play_callback_plays_valid_song(settings, proc_table):
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    song = Path(settings.openclaw_music_dir) / "AlbumA" / "00 Song.flac"
    token = cache.put(str(song))
    cb = mb.build_music_callback_handler(settings)
    toast, new_text, _ = cb(f"play:{token}", "", "c")
    assert toast.startswith("正在播放")
    assert new_text is None
    assert len(proc_table["spawned"]) == 1


def test_play_callback_rejects_unknown_token(settings, proc_table):
    cb = mb.build_music_callback_handler(settings)
    toast, _, _ = cb("play:deadbeefdeadbeef", "", "c")
    assert "找不到" in toast
    assert proc_table["spawned"] == []


def test_fav_callback_adds_then_no_duplicate(settings, proc_table):
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    song = Path(settings.openclaw_music_dir) / "AlbumA" / "00 Song.flac"
    token = cache.put(str(song))
    cb = mb.build_music_callback_handler(settings)
    toast1, _, _ = cb(f"fav:{token}", "", "c")
    assert "已加入最愛" in toast1
    toast2, _, _ = cb(f"fav:{token}", "", "c")
    assert "已經在最愛" in toast2
    store = FavoritesStore(settings.openclaw_music_best_path)
    assert len(store.list()) == 1


def test_ls_callback_rerenders_message(settings):
    cb = mb.build_music_callback_handler(settings)
    toast, new_text, markup = cb("ls:root:0", "", "c")
    assert toast is None  # rerender, not a toast
    assert new_text is not None
    assert markup["inline_keyboard"]


def test_play_callback_rejects_path_escape(settings, proc_table, tmp_path):
    # A token that resolves to a file OUTSIDE the music root must be refused.
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"x")
    cache = mb.TokenCache(settings.openclaw_music_token_cache_path)
    token = cache.put(str(outside))
    cb = mb.build_music_callback_handler(settings)
    toast, _, _ = cb(f"play:{token}", "", "c")
    assert "無法播放" in toast
    assert proc_table["spawned"] == []


# --- favorites store + list view (mb list kind) ----------------------------
def test_favorites_stable_id_is_path_based():
    p = "/Music/AlbumA/Song.flac"
    assert stable_id(p) == stable_id(p)
    assert stable_id(p) != stable_id("/Music/AlbumA/Other.flac")


def test_favorites_add_remove_roundtrip(settings):
    store = FavoritesStore(settings.openclaw_music_best_path)
    added, entry = store.add("/m/a.flac", "a")
    assert added is True
    assert store.contains(entry["id"])
    assert store.remove(entry["id"]) is True
    assert store.list() == []
    assert store.remove(entry["id"]) is False  # already gone


def test_best_view_empty_message(settings):
    view = build_music_best_view_fn(FavoritesStore(settings.openclaw_music_best_path))
    text, markup, _ = view(page=0, mode="r")
    assert "最愛清單是空的" in text
    assert markup is None


def test_best_view_lists_with_play_buttons(settings):
    store = FavoritesStore(settings.openclaw_music_best_path)
    store.add("/m/a.flac", "Song A")
    view = build_music_best_view_fn(store)
    text, markup, _ = view(page=0, mode="r")
    assert "Song A" in text
    cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert any(c.startswith("music:pf:") for c in cbs)


def test_best_item_deleter_removes(settings):
    store = FavoritesStore(settings.openclaw_music_best_path)
    _, entry = store.add("/m/a.flac", "Song A")
    deleter, label = build_music_best_item_deleter(store)
    assert deleter(entry["id"]) is True
    assert store.list() == []


def test_pf_callback_plays_favorite(settings, proc_table):
    store = FavoritesStore(settings.openclaw_music_best_path)
    song = Path(settings.openclaw_music_dir) / "AlbumA" / "00 Song.flac"
    _, entry = store.add(str(song), song.stem)
    cb = mb.build_music_callback_handler(settings)
    toast, _, _ = cb(f"pf:{entry['id']}", "", "c")
    assert toast.startswith("正在播放")
    assert len(proc_table["spawned"]) == 1


# --- #60 queue-control callbacks (menu buttons) ----------------------------
def test_menu_markup_has_queue_control_buttons():
    cbs = _all_cbs(mc._menu_markup())
    assert "music:prev" in cbs
    assert "music:playpause" in cbs
    assert "music:next" in cbs


def test_prev_next_callbacks_refuse_without_queue(settings, proc_table):
    cb = mb.build_music_callback_handler(settings)
    assert cb("prev", "", "c")[0] == mc._NO_QUEUE_MSG
    assert cb("next", "", "c")[0] == mc._NO_QUEUE_MSG


def test_next_callback_skips_current_track(settings, proc_table):
    state_path = settings.openclaw_music_player_state_path
    mc._write_session(state_path, "tok")  # active continuous queue
    pid = mc._start_song({"name": "Cur", "path": "/m/Cur.flac"}, state_path, mode="random")
    cb = mb.build_music_callback_handler(settings)
    toast, new_text, _ = cb("next", "", "c")
    assert toast == "⏭ 已跳到下一首。"
    assert new_text is None
    assert pid in proc_table["killed"]


def test_playpause_callback_toggles(settings, proc_table, monkeypatch):
    signals = []
    monkeypatch.setattr(mc, "_signal_pid", lambda pid, sig: signals.append((pid, sig)) or True)
    state_path = settings.openclaw_music_player_state_path
    pid = mc._start_song({"name": "A", "path": "/m/A.flac"}, state_path, mode="single")
    cb = mb.build_music_callback_handler(settings)

    import signal as _sig
    assert cb("playpause", "", "c")[0] == "⏸ 已暫停播放。"  # first press pauses
    assert (pid, _sig.SIGSTOP) in signals
    assert mc._read_json(state_path)["paused"] is True

    assert cb("playpause", "", "c")[0] == "▶️ 已繼續播放。"  # second press resumes
    assert (pid, _sig.SIGCONT) in signals
    assert mc._read_json(state_path)["paused"] is False


def test_playpause_callback_when_nothing_playing(settings, proc_table):
    cb = mb.build_music_callback_handler(settings)
    assert cb("playpause", "", "c")[0] == "目前沒有播放中的音樂。"
