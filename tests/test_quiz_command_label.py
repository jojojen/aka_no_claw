from types import SimpleNamespace

from openclaw_adapter.quiz_command import (
    _grade_view,
    _is_commentary_url,
    _parse_serve_args,
    _render_stats,
    _render_type_menu,
    _serve_question,
    _wants_random,
)


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


# ── type-selection feature ────────────────────────────────────────────────────


def test_wants_random_detects_flag():
    assert _wants_random("JLPTN1 miku random")
    assert _wants_random("random")
    assert not _wants_random("JLPTN1 miku")
    assert not _wants_random("")


def test_parse_serve_args_ignores_random_token():
    # 'random' is a serve-mode flag, not a theme — must not become the theme.
    level, theme = _parse_serve_args("JLPTN1 miku random")
    assert level == "JLPT N1"
    assert theme == "miku"


def test_parse_serve_args_random_only_keeps_defaults():
    level, theme = _parse_serve_args("random")
    assert level == "JLPT N1"
    assert theme == "miku"


class _FakeDB:
    def __init__(self, counts):
        self._counts = counts

    def exam_point_counts(self, *, level=None, verified_only=True):
        return list(self._counts)


def test_render_type_menu_one_button_per_type_plus_random():
    db = _FakeDB([("漢字読み", 29), ("文脈規定", 15), ("用法", 6)])
    text, markup = _render_type_menu(db, "JLPT N1", "miku")
    rows = markup["inline_keyboard"]
    flat = [b for row in rows for b in row]
    cbs = [b["callback_data"] for b in flat]
    # one button per type + a random-all button
    assert "quiz:t:JLPT N1:漢字読み" in cbs
    assert "quiz:t:JLPT N1:用法" in cbs
    assert "quiz:t:JLPT N1:*" in cbs
    assert "quiz:t:JLPT N1:!" in cbs  # 錯題本 button next to random
    assert any(b["text"] == "📭 錯題本" for b in flat)
    # every callback fits Telegram's 64-byte limit
    assert all(len(c.encode("utf-8")) <= 64 for c in cbs)
    assert "選擇題型" in text


def test_render_type_menu_empty_pool_message():
    db = _FakeDB([])
    text, markup = _render_type_menu(db, "JLPT N1", "miku")
    assert markup["inline_keyboard"] == []
    assert "題庫是空的" in text


# ── 錯題本 + 混淆分析 ──────────────────────────────────────────────────────────


class _NoWrongDB:
    def weighted_question(self, **kw):
        assert kw.get("wrong_only") is True
        return None


def test_serve_question_wrong_only_empty_message():
    out = _serve_question(None, _NoWrongDB(), "JLPT N1", "miku", None, "u1", wrong_only=True)
    assert isinstance(out, str)
    assert "沒有錯題" in out


class _StatsDB:
    def __init__(self, stats, pairs):
        self._stats = stats
        self._pairs = pairs

    def mastery_stats(self, *, chat_id=None):
        return self._stats

    def confusion_pairs(self, *, chat_id=None, limit=8):
        return list(self._pairs)


def test_render_stats_shows_confusion_pairs():
    stats = {
        "total": 5,
        "by_type": [{"key": "文法形式の判断", "accuracy": 0.4, "corrects": 2, "attempts": 5}],
        "by_point": [],
    }
    pairs = [{"exam_point": "文法形式の判断", "correct": "にあって", "chosen": "にして", "count": 3}]
    text = _render_stats(_StatsDB(stats, pairs), "u1")
    assert "最常混淆的選項" in text
    assert "「にあって」← 你選了「にして」" in text
    assert "×3" in text


def test_render_stats_no_history():
    text = _render_stats(_StatsDB({"total": 0, "by_type": [], "by_point": []}, []), "u1")
    assert "還沒作答" in text


def test_callback_wrong_marker_routes_to_wrong_only(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler

    settings = SimpleNamespace(quiz_db_path=tmp_path / "quiz.sqlite3")
    handler = build_quiz_callback_handler(settings)
    # empty DB → wrong-notebook is empty → the 錯題本 marker yields the empty message
    toast, new_text, markup = handler("t:JLPT N1:!", "menu text", "u1")
    assert new_text is not None and "沒有錯題" in new_text
