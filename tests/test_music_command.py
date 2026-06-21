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
    return state


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
def test_music_random_starts_playback(settings, proc_table):
    handler = mc.build_music_handler(settings)
    reply = handler("random", "chat-1")
    assert reply.startswith("正在播放：")
    assert len(proc_table["spawned"]) == 1
    state = json.loads(Path(settings.openclaw_music_player_state_path).read_text("utf-8"))
    assert state["pid"] == proc_table["spawned"][0][0]


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
    handler("random", "chat-1")
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
    handler("random", "chat-1")
    assert handler("stop", "chat-1") == "已停止目前由龍蝦播放的音樂。"
    assert handler("stop", "chat-1") == "目前沒有由龍蝦播放中的音樂。"


def test_stale_pid_is_cleaned_and_reported_not_playing(settings, proc_table):
    handler = mc.build_music_handler(settings)
    handler("random", "chat-1")
    pid = proc_table["spawned"][0][0]
    proc_table["alive"].discard(pid)  # process died on its own (song ended)
    reply = handler("stop", "chat-1")
    assert reply == "目前沒有由龍蝦播放中的音樂。"
    assert pid not in proc_table["killed"]  # we did not kill a dead/reused pid
    assert not Path(settings.openclaw_music_player_state_path).exists()


def test_stop_does_not_kill_unrelated_reused_pid(settings, proc_table, monkeypatch):
    handler = mc.build_music_handler(settings)
    handler("random", "chat-1")
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
    handler("random", "chat-1")
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
    reply = mc.build_music_handler(settings)("random", "chat-1")
    assert "播放失敗" in reply


def test_empty_arg_returns_usage(settings, proc_table):
    reply = mc.build_music_handler(settings)("", "chat-1")
    assert "/music random" in reply
    assert "/music stop" in reply
