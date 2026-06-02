from types import SimpleNamespace

from openclaw_adapter.quiz_command import _grade_view, _is_commentary_url


def _q(*, source_type, text_url, exam_point="内容理解（中文）", author="Claude"):
    return SimpleNamespace(
        level="JLPT N1",
        exam_point=exam_point,
        source_name="炉心融解",
        source_type=source_type,
        stem="stem",
        options=["a", "b"],
        answer_index=0,
        explanation="exp",
        source_text_url=text_url,
        source_media_url="https://www.youtube.com/watch?v=dSw8CucthGc",
        source_excerpt="x",
        author=author,
    )


def test_commentary_url_detection():
    assert _is_commentary_url("https://utaten.com/specialArticle/index/7537")
    assert _is_commentary_url("https://dic.nicovideo.jp/v/sm8089993")
    assert _is_commentary_url("https://ameblo.jp/foo/entry-1.html")
    assert not _is_commentary_url("https://utaten.com/lyric/jb50905027/")
    assert not _is_commentary_url("https://www.uta-net.com/movie/122824/")
    assert not _is_commentary_url(None)


def test_lyric_song_labeled_as_lyric():
    q = _q(source_type="vocaloid_song", text_url="https://utaten.com/lyric/jb1/")
    _, text = _grade_view(q, "orig", chosen=0)
    assert "📖 歌詞原文：" in text
    assert "賞析・解説原文" not in text


def test_song_grounded_on_commentary_labeled_as_commentary():
    # A vocaloid song item whose text_url is a 賞析 article must NOT be mislabeled 歌詞.
    q = _q(
        source_type="vocaloid_song",
        text_url="https://utaten.com/specialArticle/index/7537",
    )
    _, text = _grade_view(q, "orig", chosen=0)
    assert "📖 賞析・解説原文：" in text
    assert "歌詞原文" not in text


def test_author_shown_in_reveal():
    q = _q(source_type="vocaloid_song", text_url="https://utaten.com/lyric/jb1/", author="codex")
    _, text = _grade_view(q, "orig", chosen=0)
    assert "🖋️ 出題者：codex" in text
