from __future__ import annotations

import requests
from types import SimpleNamespace

from openclaw_adapter.quiz_favorite_songs import (
    FavoriteSongIngestor,
    LyricsMatch,
    _extract_utaten_song_title,
    _guess_song_title,
    _metadata_looks_suspicious,
    extract_youtube_video_id,
    YoutubeSongMetadata,
    split_lyrics_sentences,
)


def test_extract_youtube_video_id_from_watch_url():
    assert (
        extract_youtube_video_id("https://www.youtube.com/watch?v=OIBODIPC_8Y&si=abc")
        == "OIBODIPC_8Y"
    )


def test_extract_youtube_video_id_from_short_url():
    assert extract_youtube_video_id("https://youtu.be/OIBODIPC_8Y?si=abc") == "OIBODIPC_8Y"


def test_split_lyrics_sentences_preserves_lines_and_punctuation():
    got = split_lyrics_sentences("まるで御伽の話\n終わり迎えた証。次の旅へ！\n\n")
    assert got == ["まるで御伽の話", "終わり迎えた証。", "次の旅へ！"]


def test_ingestor_exposes_utaten_fallback():
    assert hasattr(FavoriteSongIngestor, "_find_utaten_lyrics")


def test_guess_song_title_extracts_single_quoted_title():
    got = _guess_song_title("ILLIT (아일릿) 'Sunday Morning’ Official MV", "HYBE LABELS")
    assert got == "Sunday Morning"


def test_guess_song_title_strips_leading_mv_label():
    got = _guess_song_title("【オリジナルMV】窓を開けて / CIEL #01", "CIEL")
    assert got == "窓を開けて"


def test_guess_song_title_strips_feat_suffix():
    got = _guess_song_title("DECO*27 - モニタリング feat. 初音ミク", "DECO*27")
    assert got == "モニタリング"


def test_guess_song_title_prefers_right_side_when_left_matches_artist():
    got = _guess_song_title(
        "ヨルシカ - ただ君に晴れ (MUSIC VIDEO)",
        "ヨルシカ / n-buna Official",
    )
    assert got == "ただ君に晴れ"


def test_metadata_looks_suspicious_for_official_artist_channel():
    assert _metadata_looks_suspicious(
        title="ヨルシカ",
        artist="ヨルシカ / n-buna Official",
        raw_title="ヨルシカ - ただ君に晴れ (MUSIC VIDEO)",
    )


def test_extract_utaten_song_title_removes_yomi_prefix():
    assert _extract_utaten_song_title("よみ:まどをひらけて 窓を開けて 歌詞") == "窓を開けて"


def test_find_lyrics_continues_after_single_site_http_error():
    ingestor = object.__new__(FavoriteSongIngestor)
    calls: list[str] = []

    # Finder order is utanet → utaten → vocadb. Put the HTTP error on the
    # first-tried finder so this still exercises "continue past a single-site error".
    def boom(**kwargs):
        calls.append("utanet")
        raise requests.HTTPError("404")

    def nope(**kwargs):
        calls.append("utaten")
        return None

    def ok(**kwargs):
        calls.append("vocadb")
        return LyricsMatch(
            title="Sunday Morning",
            artist="ILLIT",
            lyrics_url="https://example.com/lyrics",
            lyrics_text="hello world",
            source_kind="vocadb",
        )

    ingestor._find_utanet_lyrics = boom
    ingestor._find_utaten_lyrics = nope
    ingestor._find_vocadb_lyrics = ok

    got = ingestor._find_lyrics(title="Sunday Morning", artist="ILLIT", video_id="vid")

    assert got.source_kind == "vocadb"
    assert calls == ["utanet", "utaten", "vocadb"]


class _FakeFavoriteSongDb:
    def __init__(self) -> None:
        self.analysis_kwargs = None

    def get_favorite_song_by_youtube_short_url(self, youtube_short_url: str):
        return None

    def upsert_favorite_song(self, **kwargs):
        return 1

    def replace_favorite_song_analysis(self, **kwargs):
        self.analysis_kwargs = kwargs

    def mark_favorite_song_status(self, **kwargs):
        raise AssertionError("should not mark failed")


def test_ingest_prefers_lyrics_metadata_over_youtube_guess():
    db = _FakeFavoriteSongDb()
    ingestor = object.__new__(FavoriteSongIngestor)
    ingestor._settings = SimpleNamespace(
        openclaw_local_text_backend=None,
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_model=None,
        openclaw_local_text_timeout_seconds=45,
    )
    ingestor._db = db
    ingestor._analyzer = SimpleNamespace(analyze=lambda sentences: [])
    ingestor._fetch_youtube_metadata = lambda url: YoutubeSongMetadata(
        video_id="-VKIqrvVOpo",
        youtube_url="https://www.youtube.com/watch?v=-VKIqrvVOpo",
        youtube_short_url="https://youtu.be/-VKIqrvVOpo",
        title="ヨルシカ",
        artist="ヨルシカ / n-buna Official",
        raw_title="ヨルシカ - ただ君に晴れ (MUSIC VIDEO)",
    )
    ingestor._find_lyrics_with_metadata_fallback = lambda metadata: (
        LyricsMatch(
            title="ただ君に晴れ",
            artist="ヨルシカ",
            lyrics_url="https://www.uta-net.com/song/275012/",
            lyrics_text="あの夏に咲け",
            source_kind="uta-net",
        ),
        metadata,
    )

    got = ingestor.ingest_youtube_song("https://youtu.be/-VKIqrvVOpo")

    assert got.title == "ただ君に晴れ"
    assert got.artist == "ヨルシカ"
    assert db.analysis_kwargs is not None
    assert db.analysis_kwargs["title"] == "ただ君に晴れ"
    assert db.analysis_kwargs["artist"] == "ヨルシカ"
