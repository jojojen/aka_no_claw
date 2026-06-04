from __future__ import annotations

from openclaw_adapter.quiz_favorite_songs import (
    extract_youtube_video_id,
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
