"""Issue #33 — /music local FLAC playback (random / by-name / stop).

Real playback is stubbed: a fake process table records which pids were spawned
and killed, so the tests assert the command's *control* logic — index build,
cache reuse + invalidation, search (incl. Unicode normalization), AppleDouble
exclusion, start/previous-stop/explicit-stop, and stale-pid cleanup — without
ever launching afplay.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openclaw_adapter import music_command as mc


@pytest.fixture
def music_dir(tmp_path):
    root = tmp_path / "Music"
    (root / "ずっと真夜中でいいのに。形藻土").mkdir(parents=True)
    (root / "misc").mkdir(parents=True)
    songs = [
        "ずっと真夜中でいいのに。形藻土/01 ずっと真夜中でいいのに。 - 地球存在しない説.flac",
        "ずっと真夜中でいいのに。形藻土/02 ずっと真夜中でいいのに。 - 間人間.flac",
        "misc/Daft Punk - Get Lucky.flac",
    ]
    for rel in songs:
        (root / rel).write_bytes(b"fake-flac-bytes")
    # AppleDouble sidecar + a non-flac file: both must be ignored.
    (root / "misc" / "._Daft Punk - Get Lucky.flac").write_bytes(b"junk")
    (root / "misc" / "cover.jpg").write_bytes(b"img")
    return root


@pytest.fixture
def settings(tmp_path, music_dir):
    return SimpleNamespace(
        openclaw_music_dir=str(music_dir),
        openclaw_music_index_path=str(tmp_path / ".openclaw_tmp" / "music_index.json"),
        openclaw_music_player_state_path=str(tmp_path / ".openclaw_tmp" / "music_state.json"),
        openclaw_music_best_path=str(tmp_path / ".openclaw_tmp" / "music_best.json"),
        openclaw_music_token_cache_path=str(tmp_path / ".openclaw_tmp" / "music_tokens.json"),
    )


@pytest.fixture
def proc_table(monkeypatch):
    """Stub the module-level process primitives with an in-memory pid table."""
    state = {"next_pid": 1000, "alive": set(), "spawned": [], "killed": [], "start": {}}

    def _spawn(path):
        state["next_pid"] += 1
        pid = state["next_pid"]
        state["alive"].add(pid)
        state["spawned"].append((pid, path))
        state["start"][pid] = f"start-{pid}"  # stable per-process identity token
        return pid

    def _alive(pid):
        return pid in state["alive"]

    def _is_player(pid):
        return pid in state["alive"]  # everything we spawn is "afplay"

    def _start_time(pid):
        return state["start"].get(pid) if pid in state["alive"] else None

    def _terminate(pid):
        state["killed"].append(pid)
        state["alive"].discard(pid)

    monkeypatch.setattr(mc, "_spawn_player", _spawn)
    monkeypatch.setattr(mc, "_pid_alive", _alive)
    monkeypatch.setattr(mc, "_pid_is_player", _is_player)
    monkeypatch.setattr(mc, "_pid_start_time", _start_time)
    monkeypatch.setattr(mc, "_terminate", _terminate)
    # Skip the real spawn-survival wait; a spawned pid is "playing" iff alive.
    monkeypatch.setattr(mc, "_verify_playing", _alive)
    yield state
    mc._PLAYBEST.stop()  # never leak a playbest thread across tests


# --- index build / cache / invalidation -----------------------------------
def test_index_build_excludes_appledouble_and_non_flac(settings):
    idx = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    assert idx.rebuilt is True
    names = {e["name"] for e in idx.entries}
    assert names == {
        "01 ずっと真夜中でいいのに。 - 地球存在しない説",
        "02 ずっと真夜中でいいのに。 - 間人間",
        "Daft Punk - Get Lucky",
    }
    assert not any(Path(e["path"]).name.startswith("._") for e in idx.entries)
    assert Path(settings.openclaw_music_index_path).exists()


def test_index_reused_when_folder_unchanged(settings):
    first = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    assert first.rebuilt is True
    second = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    assert second.rebuilt is False
    assert second.signature == first.signature
    assert second.entries == first.entries


def test_index_rebuilds_when_file_added(settings, music_dir):
    mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    (music_dir / "misc" / "New Song.flac").write_bytes(b"new")
    refreshed = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    assert refreshed.rebuilt is True
    assert any(e["name"] == "New Song" for e in refreshed.entries)


def test_index_rebuilds_when_file_renamed(settings, music_dir):
    mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    old = music_dir / "misc" / "Daft Punk - Get Lucky.flac"
    old.rename(music_dir / "misc" / "Daft Punk - Instant Crush.flac")
    refreshed = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    assert refreshed.rebuilt is True
    names = {e["name"] for e in refreshed.entries}
    assert "Daft Punk - Instant Crush" in names
    assert "Daft Punk - Get Lucky" not in names


# --- search ----------------------------------------------------------------
def test_search_substring_match(settings):
    idx = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    result = mc._search(idx.entries, "Get Lucky")
    assert result.kind == "single"
    assert result.entry["name"] == "Daft Punk - Get Lucky"


def test_search_handles_unicode_normalization(settings):
    idx = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    import unicodedata
    # query in NFD (decomposed) must still match an NFC-stored filename
    query = unicodedata.normalize("NFD", "地球存在しない説")
    result = mc._search(idx.entries, query)
    assert result.kind in ("exact", "single")
    assert "地球存在しない説" in result.entry["name"]


def test_search_no_match_returns_none(settings):
    idx = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    assert mc._search(idx.entries, "zzz-not-a-real-song-xyz").kind == "none"


def test_search_ambiguous_returns_candidates(settings):
    idx = mc.load_or_build_index(settings.openclaw_music_dir, settings.openclaw_music_index_path)
    # broad query (album/artist) matches both ずっと真夜中 tracks → must not guess
    result = mc._search(idx.entries, "ずっと真夜中でいいのに")
    assert result.kind == "ambiguous"
    names = {e["name"] for e in result.candidates}
    assert names == {
        "01 ずっと真夜中でいいのに。 - 地球存在しない説",
        "02 ずっと真夜中でいいのに。 - 間人間",
    }


# --- playback --------------------------------------------------------------
def test_music_random_starts_continuous_playback(settings, proc_table, monkeypatch):
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    handler = mc.build_music_handler(settings)
    reply = handler("random", "chat-1")
    assert "隨機" in reply and "自動接續" in reply
    # the continuous controller spawns the first song on its loop thread
    import time as _t
    for _ in range(200):
        if proc_table["spawned"]:
            break
        _t.sleep(0.01)
    assert proc_table["spawned"], "random should have started a song"
    assert mc._PLAYBEST.is_active()
    handler("stop", "chat-1")
    assert not mc._PLAYBEST.is_active()


def test_music_by_name_starts_matching_song(settings, proc_table):
    handler = mc.build_music_handler(settings)
    reply = handler("間人間", "chat-1")
    assert reply == "正在播放：\n02 ずっと真夜中でいいのに。 - 間人間"
    assert proc_table["spawned"][0][1].endswith("- 間人間.flac")


def test_starting_new_song_stops_previous(settings, proc_table):
    handler = mc.build_music_handler(settings)
    handler("Get Lucky", "chat-1")
    first_pid = proc_table["spawned"][0][0]
    handler("間人間", "chat-1")
    assert first_pid in proc_table["killed"]  # previous track was stopped
    assert len(proc_table["spawned"]) == 2


def test_music_ambiguous_query_returns_list_without_playing(settings, proc_table):
    handler = mc.build_music_handler(settings)
    reply = handler("ずっと真夜中でいいのに", "chat-1")
    assert "請輸入更精確的名稱" in reply
    assert "地球存在しない説" in reply
    assert "間人間" in reply
    assert proc_table["spawned"] == []  # ambiguous => never plays


def test_stop_terminates_current(settings, proc_table):
    handler = mc.build_music_handler(settings)
    handler("間人間", "chat-1")
    pid = proc_table["spawned"][0][0]
    reply = handler("stop", "chat-1")
    assert reply == "已停止目前由龍蝦播放的音樂。"
    assert pid in proc_table["killed"]
    assert not Path(settings.openclaw_music_player_state_path).exists()


def test_stop_is_safe_when_nothing_playing(settings, proc_table):
    handler = mc.build_music_handler(settings)
    reply = handler("stop", "chat-1")
    assert reply == "目前沒有由龍蝦播放中的音樂。"
    assert proc_table["killed"] == []


def test_stop_idempotent(settings, proc_table):
    handler = mc.build_music_handler(settings)
    handler("間人間", "chat-1")
    assert handler("stop", "chat-1") == "已停止目前由龍蝦播放的音樂。"
    assert handler("stop", "chat-1") == "目前沒有由龍蝦播放中的音樂。"


def test_stale_pid_is_cleaned_and_reported_not_playing(settings, proc_table):
    handler = mc.build_music_handler(settings)
    handler("間人間", "chat-1")
    pid = proc_table["spawned"][0][0]
    proc_table["alive"].discard(pid)  # process died on its own (song ended)
    reply = handler("stop", "chat-1")
    assert reply == "目前沒有由龍蝦播放中的音樂。"
    assert pid not in proc_table["killed"]  # we did not kill a dead/reused pid
    assert not Path(settings.openclaw_music_player_state_path).exists()


def test_stop_does_not_kill_unrelated_reused_pid(settings, proc_table, monkeypatch):
    handler = mc.build_music_handler(settings)
    handler("間人間", "chat-1")
    pid = proc_table["spawned"][0][0]
    # pid is alive but now belongs to an unrelated, non-afplay process.
    monkeypatch.setattr(mc, "_pid_is_player", lambda p: False)
    reply = handler("stop", "chat-1")
    assert reply == "目前沒有由龍蝦播放中的音樂。"
    assert pid not in proc_table["killed"]


def test_stop_does_not_kill_reused_pid_that_is_a_different_afplay(settings, proc_table):
    # The recorded pid is alive AND is an afplay — but it's a *different* afplay
    # the OS handed our pid to (e.g. user's own playback). The start-time mismatch
    # must protect it from /music stop.
    handler = mc.build_music_handler(settings)
    handler("間人間", "chat-1")
    pid = proc_table["spawned"][0][0]
    proc_table["start"][pid] = "start-DIFFERENT-PROCESS"  # pid reused, new identity
    reply = handler("stop", "chat-1")
    assert reply == "目前沒有由龍蝦播放中的音樂。"
    assert pid not in proc_table["killed"]
    assert not Path(settings.openclaw_music_player_state_path).exists()


# --- error handling --------------------------------------------------------
def test_missing_folder_message(tmp_path, proc_table):
    settings = SimpleNamespace(
        openclaw_music_dir=str(tmp_path / "does_not_exist"),
        openclaw_music_index_path=str(tmp_path / "idx.json"),
        openclaw_music_player_state_path=str(tmp_path / "state.json"),
        openclaw_music_best_path=str(tmp_path / "best.json"),
        openclaw_music_token_cache_path=str(tmp_path / "tok.json"),
    )
    reply = mc.build_music_handler(settings)("random", "chat-1")
    assert "找不到音樂資料夾" in reply


def test_path_not_a_directory_message(tmp_path, proc_table):
    f = tmp_path / "notadir"
    f.write_text("x")
    settings = SimpleNamespace(
        openclaw_music_dir=str(f),
        openclaw_music_index_path=str(tmp_path / "idx.json"),
        openclaw_music_player_state_path=str(tmp_path / "state.json"),
        openclaw_music_best_path=str(tmp_path / "best.json"),
        openclaw_music_token_cache_path=str(tmp_path / "tok.json"),
    )
    reply = mc.build_music_handler(settings)("random", "chat-1")
    assert "不是資料夾" in reply


def test_empty_folder_message(tmp_path, proc_table):
    empty = tmp_path / "Empty"
    empty.mkdir()
    settings = SimpleNamespace(
        openclaw_music_dir=str(empty),
        openclaw_music_index_path=str(tmp_path / "idx.json"),
        openclaw_music_player_state_path=str(tmp_path / "state.json"),
        openclaw_music_best_path=str(tmp_path / "best.json"),
        openclaw_music_token_cache_path=str(tmp_path / "tok.json"),
    )
    reply = mc.build_music_handler(settings)("random", "chat-1")
    assert "找不到可播放的音檔" in reply


def test_no_match_message(settings, proc_table):
    reply = mc.build_music_handler(settings)("zzz-not-real-xyz", "chat-1")
    assert "找不到符合" in reply
    assert proc_table["spawned"] == []


def test_playback_failure_message(settings, monkeypatch, proc_table):
    def _boom(path):
        raise OSError("afplay missing")

    monkeypatch.setattr(mc, "_spawn_player", _boom)
    reply = mc.build_music_handler(settings)("間人間", "chat-1")
    assert "播放失敗" in reply


def test_play_reports_failure_when_afplay_dies_on_spawn(settings, monkeypatch, proc_table):
    # afplay launches but exits instantly (unreadable file / asleep external
    # drive): we must NOT claim 正在播放, must surface a failure, kill the dead
    # pid, and leave no stale player state behind.
    monkeypatch.setattr(mc, "_verify_playing", lambda pid: False)
    reply = mc.build_music_handler(settings)("間人間", "chat-1")
    assert "正在播放" not in reply
    assert "播放失敗" in reply
    pid = proc_table["spawned"][0][0]
    assert pid in proc_table["killed"]
    assert not Path(settings.openclaw_music_player_state_path).exists()


def test_empty_arg_returns_button_menu(settings, proc_table):
    reply = mc.build_music_handler(settings)("", "chat-1")
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "音樂控制" in text
    cbs = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert {"music:rnd", "music:stop", "music:ls:root:0", "pg:mb:0:r", "music:pb", "music:now"} <= cbs


# --- playbest scheduler (pure round logic) ---------------------------------
def _favs(*names):
    return [{"id": n, "name": n, "path": f"/m/{n}.flac"} for n in names]


def test_scheduler_no_repeat_within_round():
    favs = _favs("a", "b", "c")
    sch = mc.PlaybestScheduler(lambda: favs, exists_fn=lambda p: True, shuffler=lambda x: None)
    first_round = [sch.next()["id"] for _ in range(3)]
    assert sorted(first_round) == ["a", "b", "c"]  # every favorite once, no repeat


def test_scheduler_reshuffles_only_after_full_round():
    favs = _favs("a", "b", "c")
    sch = mc.PlaybestScheduler(lambda: favs, exists_fn=lambda p: True, shuffler=lambda x: None)
    seen = [sch.next()["id"] for _ in range(6)]  # two full rounds
    assert seen[:3].count("a") == 1 and seen[3:].count("a") == 1  # one play per round


def test_scheduler_skips_missing_files():
    favs = _favs("a", "gone", "c")
    exists = lambda p: not p.endswith("gone.flac")
    sch = mc.PlaybestScheduler(lambda: favs, exists_fn=exists, shuffler=lambda x: None)
    got = [sch.next()["id"] for _ in range(4)]
    assert "gone" not in got
    assert set(got) <= {"a", "c"}


def test_scheduler_returns_none_when_empty():
    sch = mc.PlaybestScheduler(lambda: [], exists_fn=lambda p: True)
    assert sch.next() is None


# --- playbest controller + stop integration --------------------------------
def _write_favorites(settings, music_dir):
    """Register two real songs as favorites and return the store."""
    from openclaw_adapter.music_favorites import FavoritesStore

    store = FavoritesStore(settings.openclaw_music_best_path)
    songs = sorted(music_dir.rglob("*.flac"))
    songs = [s for s in songs if not s.name.startswith("._")][:2]
    for s in songs:
        store.add(str(s), s.stem)
    return store, [str(s) for s in songs]


def test_playbest_starts_and_stop_halts_it(settings, music_dir, proc_table, monkeypatch):
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    _write_favorites(settings, music_dir)
    handler = mc.build_music_handler(settings)
    reply = handler("playbest", "chat-1")
    assert "開始連續" in reply
    # wait for the loop to spawn at least one song
    import time as _t
    for _ in range(200):
        if proc_table["spawned"]:
            break
        _t.sleep(0.01)
    assert proc_table["spawned"], "playbest should have started a song"
    assert mc._PLAYBEST.is_active()

    stop_reply = handler("stop", "chat-1")
    assert "已停止" in stop_reply
    assert not mc._PLAYBEST.is_active()  # stop halts playbest, no auto-restart
    # give the loop a moment; it must not spawn anything new after stop
    n = len(proc_table["spawned"])
    _t.sleep(0.05)
    assert len(proc_table["spawned"]) == n


def test_playbest_auto_advances_when_song_ends(settings, music_dir, proc_table, monkeypatch):
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    _write_favorites(settings, music_dir)
    handler = mc.build_music_handler(settings)
    handler("playbest", "chat-1")
    import time as _t
    # song "ends" => drop the live pid; loop should advance and spawn the next
    for _ in range(200):
        if proc_table["spawned"]:
            break
        _t.sleep(0.01)
    first = proc_table["spawned"][0][0]
    proc_table["alive"].discard(first)  # simulate song finishing on its own
    for _ in range(200):
        if len(proc_table["spawned"]) >= 2:
            break
        _t.sleep(0.01)
    assert len(proc_table["spawned"]) >= 2  # auto-advanced to the next favorite
    handler("stop", "chat-1")


def test_playbest_stop_leaves_nothing_playing(settings, music_dir, proc_table, monkeypatch):
    # Regression: an auto-advanced track started in the gap between "song ended"
    # and "next song spawned" must NOT escape /music stop. Drive several natural
    # endings, then stop, and assert nothing is left alive and state is cleared.
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    _write_favorites(settings, music_dir)
    handler = mc.build_music_handler(settings)
    handler("playbest", "chat-1")
    import time as _t
    for _ in range(200):
        if proc_table["spawned"]:
            break
        _t.sleep(0.01)
    for _ in range(3):  # simulate songs finishing → loop keeps auto-advancing
        if proc_table["spawned"]:
            proc_table["alive"].discard(proc_table["spawned"][-1][0])
        _t.sleep(0.03)
    handler("stop", "chat-1")
    _t.sleep(0.05)
    assert not mc._PLAYBEST.is_active()
    assert proc_table["alive"] == set(), "no track may remain playing after stop"
    assert not Path(settings.openclaw_music_player_state_path).exists()


def test_playbest_cross_process_stop_via_session(settings, music_dir, proc_table, monkeypatch):
    # The Telegram poller and the LAN command-bridge are SEPARATE processes, each
    # with its own controller. A /music stop in one must halt the other's loop —
    # which it does by deleting the shared session token. Here a stand-in
    # controller plays; clearing the session (as the other process's stop would)
    # must stop it auto-advancing even though we never touched the controller.
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    store, _ = _write_favorites(settings, music_dir)
    state_path = settings.openclaw_music_player_state_path
    other = mc.PlaybestController()  # stands in for the bridge process's loop
    other.start(
        entries_provider=store.list,
        state_path=state_path,
        is_playable=lambda p: True,
    )
    import time as _t
    for _ in range(200):
        if proc_table["spawned"]:
            break
        _t.sleep(0.01)
    assert proc_table["spawned"]
    n = len(proc_table["spawned"])
    # Simulate the other process's /music stop: clear the shared session token
    # and kill the current track. We never call other.stop() (can't, cross-proc).
    mc._clear_session(state_path)
    proc_table["alive"].clear()
    _t.sleep(0.1)
    assert len(proc_table["spawned"]) == n, "loop must not auto-advance after session cleared"
    assert not other.is_active()
    other.stop()


def test_stop_clears_shared_session(settings, music_dir, proc_table, monkeypatch):
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    _write_favorites(settings, music_dir)
    handler = mc.build_music_handler(settings)
    handler("playbest", "chat-1")
    state_path = settings.openclaw_music_player_state_path
    import time as _t
    for _ in range(200):
        if mc._read_session_token(state_path) is not None:
            break
        _t.sleep(0.01)
    assert mc._read_session_token(state_path) is not None  # session claimed
    handler("stop", "chat-1")
    assert mc._read_session_token(state_path) is None  # /music stop cleared it


def test_playbest_empty_favorites_message(settings, proc_table):
    reply = mc.build_music_handler(settings)("playbest", "chat-1")
    assert "最愛清單是空的" in reply
    assert proc_table["spawned"] == []


# --- playbest path validation (issue #34 codex review) ---------------------
# A favorite is a persisted path that may predate a music-root change or be
# hand-edited; playbest must apply the same root/suffix check as single play and
# NEVER play an existing-but-out-of-root file.
def _favorites_store(settings):
    from openclaw_adapter.music_favorites import FavoritesStore

    return FavoritesStore(settings.openclaw_music_best_path)


def test_scheduler_skips_out_of_root_with_validator(settings, music_dir, tmp_path):
    outside = tmp_path / "outside" / "rogue.flac"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"fake-flac-bytes")
    in_root = sorted(p for p in music_dir.rglob("*.flac") if not p.name.startswith("._"))[0]
    favs = [
        {"id": "ok", "name": "ok", "path": str(in_root)},
        {"id": "rogue", "name": "rogue", "path": str(outside)},
    ]
    validator = lambda p: mc.validate_song_path(p, settings.openclaw_music_dir)
    sch = mc.PlaybestScheduler(lambda: favs, exists_fn=validator, shuffler=lambda x: None)
    got = [sch.next()["id"] for _ in range(4)]
    assert "rogue" not in got
    assert set(got) <= {"ok"}


def test_play_best_rejects_existing_out_of_root_favorite(settings, music_dir, tmp_path, proc_table):
    # An existing .flac that lives OUTSIDE the music root.
    outside = tmp_path / "outside" / "rogue.flac"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"fake-flac-bytes")
    assert outside.exists()  # os.path.exists() alone would have admitted it
    store = _favorites_store(settings)
    store.add(str(outside), "rogue")
    reply = mc.build_music_handler(settings)("playbest", "chat-1")
    assert "沒有可播放" in reply
    assert proc_table["spawned"] == []  # must NOT spawn the out-of-root file
    assert not mc._PLAYBEST.is_active()


def test_play_best_skips_favorite_from_old_root(settings, music_dir, tmp_path, proc_table):
    # A favorite that was valid under a previous music root: file still exists,
    # but is no longer under the current OPENCLAW_MUSIC_DIR → must be skipped.
    old_song = tmp_path / "OldMusic" / "album" / "old.flac"
    old_song.parent.mkdir(parents=True)
    old_song.write_bytes(b"fake-flac-bytes")
    store = _favorites_store(settings)
    store.add(str(old_song), "old")
    reply = mc.build_music_handler(settings)("playbest", "chat-1")
    assert "沒有可播放" in reply
    assert proc_table["spawned"] == []


def test_play_best_never_spawns_out_of_root_when_mixed(settings, music_dir, tmp_path, proc_table, monkeypatch):
    # In-root favorite + out-of-root favorite: the loop may play the valid one,
    # but the out-of-root path must never be spawned, even on auto-advance.
    monkeypatch.setattr(mc, "_PLAYBEST_POLL_SECONDS", 0.01)
    outside = tmp_path / "outside" / "rogue.flac"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"fake-flac-bytes")
    in_root = sorted(p for p in music_dir.rglob("*.flac") if not p.name.startswith("._"))[0]
    store = _favorites_store(settings)
    store.add(str(in_root), in_root.stem)
    store.add(str(outside), "rogue")
    handler = mc.build_music_handler(settings)
    assert "開始連續" in handler("playbest", "chat-1")
    import time as _t

    # Drive several advance cycles by ending whatever is currently playing.
    for _ in range(40):
        for pid in list(proc_table["alive"]):
            proc_table["alive"].discard(pid)
        _t.sleep(0.01)
    handler("stop", "chat-1")
    spawned_paths = {p for _pid, p in proc_table["spawned"]}
    assert str(outside) not in spawned_paths
    assert spawned_paths <= {str(in_root)}


# --- musicnowbest ----------------------------------------------------------
def test_musicnowbest_adds_current_song(settings, proc_table):
    play = mc.build_music_handler(settings)
    play("間人間", "chat-1")
    reply = mc.build_musicnowbest_handler(settings)("", "chat-1")
    assert reply.startswith("已加入最愛")
    from openclaw_adapter.music_favorites import FavoritesStore
    assert len(FavoritesStore(settings.openclaw_music_best_path).list()) == 1


def test_musicnowbest_no_duplicate(settings, proc_table):
    mc.build_music_handler(settings)("間人間", "chat-1")
    now = mc.build_musicnowbest_handler(settings)
    assert now("", "chat-1").startswith("已加入最愛")
    assert "已經在最愛" in now("", "chat-1")  # second add is a no-op


def test_musicnowbest_nothing_playing(settings, proc_table):
    reply = mc.build_musicnowbest_handler(settings)("", "chat-1")
    assert "目前沒有播放中" in reply


# --- now_playing (web#3 生活 mode now-playing strip) ------------------------
def test_now_playing_returns_song_name_while_playing(settings, proc_table):
    mc.build_music_handler(settings)("間人間", "chat-1")
    assert mc.now_playing(settings) == "02 ずっと真夜中でいいのに。 - 間人間"


def test_now_playing_is_none_when_idle(settings, proc_table):
    assert mc.now_playing(settings) is None


def test_now_playing_is_none_after_song_ends(settings, proc_table):
    mc.build_music_handler(settings)("間人間", "chat-1")
    pid = proc_table["spawned"][0][0]
    proc_table["alive"].discard(pid)  # song finished on its own
    assert mc.now_playing(settings) is None


# --- callback path safety --------------------------------------------------
def test_validate_song_path_rejects_outside_root(settings, tmp_path):
    outside = tmp_path / "evil.flac"
    outside.write_bytes(b"x")
    assert mc.validate_song_path(str(outside), settings.openclaw_music_dir) is False


def test_validate_song_path_rejects_appledouble_and_non_flac(settings, music_dir):
    assert mc.validate_song_path(str(music_dir / "misc" / "._Daft Punk - Get Lucky.flac"),
                                 settings.openclaw_music_dir) is False
    assert mc.validate_song_path(str(music_dir / "misc" / "cover.jpg"),
                                 settings.openclaw_music_dir) is False


def test_validate_song_path_accepts_real_song(settings, music_dir):
    song = music_dir / "misc" / "Daft Punk - Get Lucky.flac"
    assert mc.validate_song_path(str(song), settings.openclaw_music_dir) is True
