from types import SimpleNamespace

from openclaw_adapter.quiz_command import (
    _author_code,
    _author_from_code,
    _grade_view,
    _is_commentary_url,
    _parse_grammar_args,
    _parse_vocab_args,
    _parse_serve_args,
    _render_author_menu,
    _render_grammar_lookup,
    _render_stats,
    _render_type_menu,
    _render_vocab_lookup,
    _vocab_audio_enabled,
    _serve_question,
    _wants_byauthor,
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


def test_parse_vocab_args_modes_and_query():
    assert _parse_vocab_args("") == ("JLPT N1", "weak", "")
    assert _parse_vocab_args("all") == ("JLPT N1", "all", "")
    assert _parse_vocab_args("wrong") == ("JLPT N1", "wrong", "")
    assert _parse_vocab_args("random") == ("JLPT N1", "random", "")
    assert _parse_vocab_args("source 夜もすがら君想ふ") == ("JLPT N1", "source", "夜もすがら君想ふ")
    assert _parse_vocab_args("範疇") == ("JLPT N1", "lookup", "範疇")


def test_parse_grammar_args_modes_and_query():
    assert _parse_grammar_args("") == ("JLPT N1", "weak", "")
    assert _parse_grammar_args("all") == ("JLPT N1", "all", "")
    assert _parse_grammar_args("wrong") == ("JLPT N1", "wrong", "")
    assert _parse_grammar_args("random") == ("JLPT N1", "random", "")
    assert _parse_grammar_args("source ロキ") == ("JLPT N1", "source", "ロキ")
    assert _parse_grammar_args("〜まい") == ("JLPT N1", "lookup", "〜まい")


class _FakeDB:
    def __init__(self, counts, authors=None, by_author=None):
        self._counts = counts
        self._authors = authors or []
        self._by_author = by_author or {}

    def exam_point_counts(self, *, level=None, verified_only=True, author=None):
        if author is not None:
            return list(self._by_author.get(author, []))
        return list(self._counts)

    def author_counts(self, *, level=None, verified_only=True):
        return list(self._authors)


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


# ── byauthor feature ──────────────────────────────────────────────────────────


def test_wants_byauthor_detects_flag():
    assert _wants_byauthor("JLPTN1 miku byauthor")
    assert _wants_byauthor("byauthor")
    assert not _wants_byauthor("JLPTN1 miku")
    assert not _wants_byauthor("JLPTN1 miku random")


def test_parse_serve_args_ignores_byauthor_token():
    # 'byauthor' is a serve-mode flag, not a theme.
    level, theme = _parse_serve_args("JLPTN1 miku byauthor")
    assert level == "JLPT N1"
    assert theme == "miku"


def test_author_code_is_colon_free_and_roundtrips():
    # qwen3:14b carries a colon → must be sanitized so it survives callback split.
    assert ":" not in _author_code("qwen3:14b")
    db = _FakeDB([], authors=[("codex", 283), ("qwen3:14b", 216), ("Claude", 142)])
    for author, _ in db.author_counts():
        assert _author_from_code(db, "JLPT N1", _author_code(author)) == author
    assert _author_from_code(db, "JLPT N1", "nope") is None


def test_render_author_menu_one_button_per_author():
    db = _FakeDB([], authors=[("codex", 283), ("qwen3:14b", 216), ("Claude", 142)])
    text, markup = _render_author_menu(db, "JLPT N1", "miku")
    flat = [b for row in markup["inline_keyboard"] for b in row]
    cbs = [b["callback_data"] for b in flat]
    assert "quiz:au:JLPT N1:codex" in cbs
    assert "quiz:au:JLPT N1:qwen314b" in cbs  # colon stripped
    assert "quiz:au:JLPT N1:Claude" in cbs
    assert any("qwen3:14b" in b["text"] for b in flat)  # label keeps the real name
    assert all(len(c.encode("utf-8")) <= 64 for c in cbs)
    assert "選擇出題者" in text


def test_render_author_menu_empty_pool_message():
    db = _FakeDB([], authors=[])
    text, markup = _render_author_menu(db, "JLPT N1", "miku")
    assert markup["inline_keyboard"] == []
    assert "題庫是空的" in text


def test_author_scoped_type_menu_carries_author_code():
    db = _FakeDB(
        [("内容理解（中文）", 65)],
        authors=[("Claude", 142)],
        by_author={"Claude": [("内容理解（中文）", 34), ("文章の文法", 12)]},
    )
    text, markup = _render_type_menu(db, "JLPT N1", "miku", author="Claude")
    flat = [b for row in markup["inline_keyboard"] for b in row]
    cbs = [b["callback_data"] for b in flat]
    assert "quiz:ta:JLPT N1:内容理解（中文）:Claude" in cbs
    assert "quiz:ta:JLPT N1:文章の文法:Claude" in cbs
    assert "quiz:ta:JLPT N1:*:Claude" in cbs  # random within author scope
    assert "quiz:ta:JLPT N1:!:Claude" in cbs  # 錯題本 within author scope
    assert "出題者：Claude" in text
    assert all(len(c.encode("utf-8")) <= 64 for c in cbs)


def test_author_scoped_type_menu_empty_for_author():
    db = _FakeDB([], authors=[("Claude", 1)], by_author={"Claude": []})
    text, markup = _render_type_menu(db, "JLPT N1", "miku", author="Claude")
    assert markup["inline_keyboard"] == []
    assert "Claude" in text and "沒有題目" in text


def test_callback_au_shows_author_scoped_type_menu(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.insert_question(
        level="JLPT N1", exam_point="内容理解（中文）", stem="本文によれば？",
        options=("a", "b", "c", "d"), answer_index=0, explanation="e",
        source_type="vocaloid_song", source_name="炉心融解",
        source_excerpt="x" * 10, author="Claude", verified=True,
    )
    handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=dbp))
    toast, new_text, markup = handler("au:JLPT N1:Claude", "menu", "u1")
    assert new_text is not None and "出題者：Claude" in new_text
    cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert any(c.startswith("quiz:ta:JLPT N1:内容理解（中文）:Claude") for c in cbs)


def test_callback_ta_serves_author_filtered_question(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.insert_question(
        level="JLPT N1", exam_point="内容理解（中文）", stem="クロードの問題",
        options=("a", "b", "c", "d"), answer_index=0, explanation="e",
        source_type="vocaloid_song", source_name="炉心融解",
        source_excerpt="x" * 10, author="Claude", verified=True,
    )
    db.insert_question(
        level="JLPT N1", exam_point="内容理解（中文）", stem="コーデックスの問題",
        options=("a", "b", "c", "d"), answer_index=0, explanation="e",
        source_type="vocaloid_song", source_name="メルト",
        source_excerpt="y" * 10, author="codex", verified=True,
    )
    handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=dbp))
    # author-scoped serve must only ever return the Claude-authored stem.
    for _ in range(8):
        toast, new_text, markup = handler(
            "ta:JLPT N1:内容理解（中文）:Claude", "menu", "u1"
        )
        assert new_text is not None
        assert "クロードの問題" in new_text
        assert "コーデックスの問題" not in new_text


def test_callback_au_unknown_author_code(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    QuizDatabase(dbp)  # empty pool → no authors → any code is unknown
    handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=dbp))
    toast, new_text, markup = handler("au:JLPT N1:ghost", "menu", "u1")
    assert toast is not None and "找不到該出題者" in toast
    assert new_text is None


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


def test_answer_callback_shows_random_and_same_type_buttons(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    q = db.insert_question(
        level="JLPT N1",
        exam_point="文法形式の判断",
        stem="彼は約束を守る（　　）人だ。",
        options=("べき", "まま", "のみ", "ほど"),
        answer_index=0,
        explanation="文脈上は当然・義務の意味。",
        source_type="vocaloid_song",
        source_name="炉心融解",
        source_excerpt="x" * 12,
        author="Claude",
        verified=True,
        allow_ungrounded=True,
    )
    handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=dbp))
    toast, new_text, markup = handler(f"a:{q.question_id}:0", "orig", "u1")
    assert toast == "✅ 答對了！"
    assert new_text is not None and "✅ 正解！" in new_text
    flat = [b for row in markup["inline_keyboard"] for b in row]
    cbs = [b["callback_data"] for b in flat]
    assert f"quiz:t:{q.level}:*" in cbs
    assert f"quiz:t:{q.level}:{q.exam_point}" in cbs
    assert any(b["text"] == "🧩 同類型下一題" for b in flat)


def test_render_vocab_lookup_multiple_hits_lists_suggestions():
    class _LookupDB:
        def get_vocab_card(self, **kwargs):
            return None

        def find_vocab_cards(self, **kwargs):
            return [
                SimpleNamespace(headword="魅了", reading_hiragana="みりょう", zh_gloss_short="魅惑"),
                SimpleNamespace(headword="魅了する", reading_hiragana="みりょうする", zh_gloss_short="使著迷"),
            ]

    text = _render_vocab_lookup(_LookupDB(), level="JLPT N1", query="魅了")
    assert "找到 2 張相關單字卡" in text
    assert "魅了（みりょう）" in text
    assert "魅了する（みりょうする）" in text


def test_render_grammar_lookup_multiple_hits_lists_suggestions():
    class _LookupDB:
        def get_grammar_card(self, **kwargs):
            return None

        def find_grammar_cards(self, **kwargs):
            return [
                SimpleNamespace(headword="〜まい", source_name="ロキ"),
                SimpleNamespace(headword="〜以上", source_name="テスト曲"),
            ]

    text = _render_grammar_lookup(_LookupDB(), level="JLPT N1", query="〜")
    assert "找到 2 張相關文法卡" in text
    assert "〜まい" in text
    assert "〜以上" in text


def test_quiz_vocab_handler_exact_lookup(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.upsert_vocab_seed("範疇", "はんちゅう", "範圍、類別")
    db.insert_question(
        level="JLPT N1",
        exam_point="漢字読み",
        stem="次の一節「プログラムの範疇さ」にある〈範疇〉の読み方として最も適切なものはどれか。",
        options=("はんじゅう", "はんちゅう", "ばんちゅう", "はんとう"),
        answer_index=1,
        explanation="〈範疇〉は「はんちゅう」と読む。",
        source_type="vocaloid_song",
        source_name="ダンスロボットダンス",
        source_text_url="http://www5.atwiki.jp/hmiku/pages/35673.html",
        source_media_url="https://www.youtube.com/watch?v=g7dvpD_zlIM",
        source_excerpt="プログラムの範疇さ",
        tested_point="範疇",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    handler = build_quiz_handler(SimpleNamespace(quiz_db_path=dbp))
    text, markup = handler("vocab 範疇", "u1")
    assert "範疇（はんちゅう）" in text
    assert "中文：範圍、類別" in text
    assert "歌曲：https://www.youtube.com/watch?v=g7dvpD_zlIM" in text
    assert "原文：http://www5.atwiki.jp/hmiku/pages/35673.html" in text
    assert any(
        b["callback_data"].startswith("quiz:vr:")
        for row in markup["inline_keyboard"]
        for b in row
    )


def test_quiz_grammar_handler_exact_lookup(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.insert_question(
        level="JLPT N1",
        exam_point="文章の文法",
        stem="彼は最後まで逃げる（　　）と踏みとどまった。",
        options=("まい", "べく", "ものを", "ながら"),
        answer_index=0,
        explanation="「〜まい」は強い否定意志を表し、この文では『逃げるつもりはない』という意味になる。",
        source_type="vocaloid_song",
        source_name="ロキ",
        source_text_url="https://example.com/text",
        source_media_url="https://example.com/song",
        source_excerpt="逃げるまいと心に決めた。",
        tested_point="〜まい",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    handler = build_quiz_handler(SimpleNamespace(quiz_db_path=dbp))
    text, markup = handler("grammar 〜まい", "u1")
    assert "句型：〜まい" in text
    assert "說明：" in text
    assert "例句：逃げるまいと心に決めた。" in text
    assert any(
        b["callback_data"].startswith("quiz:gr:")
        for row in markup["inline_keyboard"]
        for b in row
    )


def test_render_vocab_card_shows_audio_button_for_trial_headword():
    from openclaw_adapter.quiz_command import _render_vocab_card

    card = SimpleNamespace(
        level="JLPT N1",
        headword="いがみ合って",
        reading_hiragana="いがみあって",
        zh_gloss_short="互相爭執",
        example_ja="二人はいがみ合っていた。",
        exam_points=("表記",),
        source_name="source",
        source_media_url="https://example.com/song",
        source_text_url="https://example.com/text",
        vocab_id="vid1",
    )
    assert _vocab_audio_enabled(card) is True
    _, markup = _render_vocab_card(card, mode="all", index=0, total=1)
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "quiz:va:vid1" for b in flat)


def test_render_vocab_card_shows_audio_button_for_any_headword_with_example():
    from openclaw_adapter.quiz_command import _render_vocab_card

    card = SimpleNamespace(
        level="JLPT N1",
        headword="範疇",
        reading_hiragana="はんちゅう",
        zh_gloss_short="範圍、類別",
        example_ja="それは議論の範疇だ。",
        exam_points=("漢字読み",),
        source_name="source",
        source_media_url=None,
        source_text_url=None,
        vocab_id="vid2",
    )
    assert _vocab_audio_enabled(card) is True
    _, markup = _render_vocab_card(card, mode="all", index=0, total=1)
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "quiz:va:vid2" for b in flat)


def test_render_grammar_card_shows_audio_button_for_example():
    from openclaw_adapter.quiz_command import _render_grammar_card

    card = SimpleNamespace(
        card_id="gid1",
        level="JLPT N1",
        headword="〜まい",
        explanation_zh="強い否定意志。",
        example_ja="逃げるまいと心に決めた。",
        exam_points=("文章の文法",),
        source_name="ロキ",
        source_media_url=None,
        source_text_url=None,
        tested_jlpt_level="N1",
        author="codex",
    )
    assert _vocab_audio_enabled(card) is True
    _, markup = _render_grammar_card(card, mode="all", index=0, total=1)
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "quiz:ga:gid1" for b in flat)


def test_vocab_related_question_callback_serves_matching_question(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.upsert_vocab_seed("範疇", "はんちゅう", "範圍、類別")
    q = db.insert_question(
        level="JLPT N1",
        exam_point="漢字読み",
        stem="次の一節「プログラムの範疇さ」にある〈範疇〉の読み方として最も適切なものはどれか。",
        options=("はんじゅう", "はんちゅう", "ばんちゅう", "はんとう"),
        answer_index=1,
        explanation="〈範疇〉は「はんちゅう」と読む。",
        source_type="vocaloid_song",
        source_name="ダンスロボットダンス",
        source_text_url="http://www5.atwiki.jp/hmiku/pages/35673.html",
        source_excerpt="プログラムの範疇さ",
        tested_point="範疇",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_vocab_card(headword="範疇", level="JLPT N1")
    assert card is not None
    handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=dbp))
    toast, new_text, markup = handler(f"vr:{card.vocab_id}", "card text", "u1")
    assert toast is None
    assert q.stem in new_text
    assert markup is not None


def test_grammar_card_end_to_end_flow(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_command import build_quiz_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    q = db.insert_question(
        level="JLPT N1",
        exam_point="文章の文法",
        stem="彼は最後まで逃げる（　　）と踏みとどまった。",
        options=("まい", "べく", "ものを", "ながら"),
        answer_index=0,
        explanation="「〜まい」は強い否定意志を表し、この文では『逃げるつもりはない』という意味になる。",
        source_type="vocaloid_song",
        source_name="ロキ",
        source_text_url="https://example.com/text",
        source_excerpt="逃げるまいと心に決めた。",
        tested_point="〜まい",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    command_handler = build_quiz_handler(SimpleNamespace(quiz_db_path=dbp))
    card_text, card_markup = command_handler("grammar 〜まい", "u1")
    serve_payload = next(
        b["callback_data"].removeprefix("quiz:")
        for row in card_markup["inline_keyboard"]
        for b in row
        if b["callback_data"].startswith("quiz:gr:")
    )
    callback_handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=dbp))
    toast, question_text, question_markup = callback_handler(serve_payload, card_text, "u1")
    assert toast is None
    assert q.stem in question_text
    assert question_markup is not None
    toast, graded_text, graded_markup = callback_handler(f"a:{q.question_id}:0", question_text, "u1")
    assert toast == "✅ 答對了！"
    assert "正解：A. まい" in graded_text
    review_payload = next(
        b["callback_data"].removeprefix("quiz:")
        for row in graded_markup["inline_keyboard"]
        for b in row
        if b["callback_data"].startswith("quiz:gc:")
    )
    toast, card_again_text, card_again_markup = callback_handler(review_payload, graded_text, "u1")
    assert toast is None
    assert "句型：〜まい" in card_again_text
    assert card_again_markup is not None


def test_grammar_audio_callback_sends_document_for_grammar_card(tmp_path, monkeypatch):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    class _FakeSynth:
        def synthesize_to_path(self, *, text, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(f"audio:{text}".encode("utf-8"))
            return output_path

        def synthesize_to_cache(self, *, text, cache_dir, vocab_id):
            out = cache_dir / f"{vocab_id}--fake.wav"
            self.synthesize_to_path(text=text, output_path=out)
            return SimpleNamespace(
                output_path=out,
                engine_tag="fake",
                engine_label="Fake Engine",
            )

    sent = {}

    class _FakeClient:
        def __init__(self, token, *, ssl_context=None):
            sent["token"] = token

        def send_document(self, *, chat_id, document_path, caption=None):
            sent["chat_id"] = chat_id
            sent["document_path"] = str(document_path)
            sent["caption"] = caption

    monkeypatch.setattr("openclaw_adapter.quiz_command.build_vocab_synthesizer", lambda settings, params=None: _FakeSynth())
    monkeypatch.setattr("openclaw_adapter.quiz_command.TelegramBotClient", _FakeClient)

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.insert_question(
        level="JLPT N1",
        exam_point="文章の文法",
        stem="彼は最後まで逃げる（　　）と踏みとどまった。",
        options=("まい", "べく", "ものを", "ながら"),
        answer_index=0,
        explanation="「〜まい」は強い否定意志を表し、この文では『逃げるつもりはない』という意味になる。",
        source_type="vocaloid_song",
        source_name="ロキ",
        source_text_url="https://example.com/text",
        source_media_url="https://example.com/song",
        source_excerpt="逃げるまいと心に決めた。",
        tested_point="〜まい",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(headword="〜まい", level="JLPT N1")
    assert card is not None
    settings = SimpleNamespace(
        quiz_db_path=dbp,
        openclaw_telegram_bot_token="token123",
        openclaw_local_tts_endpoint="http://127.0.0.1:10101",
        openclaw_local_tts_timeout_seconds=20,
        openclaw_local_tts_speaker_id=None,
        openclaw_tls_insecure_skip_verify=False,
        openclaw_ca_bundle_path=None,
    )
    handler = build_quiz_callback_handler(settings)
    toast, new_text, markup = handler(f"ga:{card.card_id}", "card text", "u1")
    assert toast == "已送出例句音檔"
    assert new_text is None
    assert markup is None
    assert sent["chat_id"] == "u1"
    assert "Fake Engine" in sent["caption"]
    assert "〜まい" in sent["caption"]
    assert sent["document_path"].endswith(f"{card.card_id}--fake.wav")


def test_vocab_audio_callback_sends_document_for_vocab_card(tmp_path, monkeypatch):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_db import QuizDatabase

    class _FakeSynth:
        def synthesize_to_path(self, *, text, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(f"audio:{text}".encode("utf-8"))
            return output_path

        def synthesize_to_cache(self, *, text, cache_dir, vocab_id):
            out = cache_dir / f"{vocab_id}--fake.wav"
            self.synthesize_to_path(text=text, output_path=out)
            return SimpleNamespace(
                output_path=out,
                engine_tag="fake",
                engine_label="Fake Engine",
            )

    sent = {}

    class _FakeClient:
        def __init__(self, token, *, ssl_context=None):
            sent["token"] = token

        def send_document(self, *, chat_id, document_path, caption=None):
            sent["chat_id"] = chat_id
            sent["document_path"] = str(document_path)
            sent["caption"] = caption

    monkeypatch.setattr("openclaw_adapter.quiz_command.build_vocab_synthesizer", lambda settings, params=None: _FakeSynth())
    monkeypatch.setattr("openclaw_adapter.quiz_command.TelegramBotClient", _FakeClient)

    dbp = tmp_path / "quiz.sqlite3"
    db = QuizDatabase(dbp)
    db.upsert_vocab_seed("いがみ合って", "いがみあって", "互相爭執")
    db.insert_question(
        level="JLPT N1",
        exam_point="用法",
        stem="「いがみ合って」の使い方として最も適切なものはどれか。",
        options=("二人はいがみ合っていた。", "春はいがみ合って咲く。"),
        answer_index=0,
        explanation="人どうしが反目して争う文脈で使う。",
        source_type="vocaloid_song",
        source_name="Just Be Friends",
        source_text_url="https://example.com/text",
        source_media_url="https://example.com/song",
        source_excerpt="二人はいがみ合っていた。",
        tested_point="いがみ合って",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_vocab_card(headword="いがみ合って", level="JLPT N1")
    assert card is not None
    settings = SimpleNamespace(
        quiz_db_path=dbp,
        openclaw_telegram_bot_token="token123",
        openclaw_local_tts_endpoint="http://127.0.0.1:10101",
        openclaw_local_tts_timeout_seconds=20,
        openclaw_local_tts_speaker_id=None,
        openclaw_tls_insecure_skip_verify=False,
        openclaw_ca_bundle_path=None,
    )
    handler = build_quiz_callback_handler(settings)
    toast, new_text, markup = handler(f"va:{card.vocab_id}", "card text", "u1")
    assert toast == "已送出例句音檔"
    assert new_text is None
    assert markup is None
    assert sent["chat_id"] == "u1"
    assert "Fake Engine" in sent["caption"]
    assert "いがみ合って" in sent["caption"]
    assert sent["document_path"].endswith(f"{card.vocab_id}--fake.wav")


def test_vocab_audio_callback_rejects_missing_example(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler

    class _FakeDB:
        def get_vocab_card(self, *, vocab_id=None, level=None):
            return SimpleNamespace(
                vocab_id="vid-missing-example",
                level="JLPT N1",
                headword="範疇",
                reading_hiragana="はんちゅう",
                zh_gloss_short="範圍、類別",
                example_ja="",
                source_name="ダンスロボットダンス",
                source_media_url=None,
                source_text_url=None,
                author="codex",
            )

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr("openclaw_adapter.quiz_command._open_db", lambda settings: _FakeDB())
    settings = SimpleNamespace(
        quiz_db_path=tmp_path / "quiz.sqlite3",
        openclaw_telegram_bot_token="token123",
        openclaw_local_tts_endpoint="http://127.0.0.1:10101",
        openclaw_local_tts_timeout_seconds=20,
        openclaw_local_tts_speaker_id=None,
        openclaw_tls_insecure_skip_verify=False,
        openclaw_ca_bundle_path=None,
    )
    try:
        handler = build_quiz_callback_handler(settings)
        toast, new_text, markup = handler("va:vid-missing-example", "card text", "u1")
        assert toast == "這張單字卡目前沒有開放例句音檔"
        assert new_text is None
        assert markup is None
    finally:
        monkeypatch.undo()


def test_quiz_like_song_usage(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_handler

    settings = SimpleNamespace(quiz_db_path=tmp_path / "quiz.sqlite3")
    handler = build_quiz_handler(settings)
    got = handler("like", "u1")
    assert got == "用法：/quizlikesong <youtube_url>"


def test_quiz_like_song_success(tmp_path, monkeypatch):
    from openclaw_adapter.quiz_command import build_quiz_handler
    from openclaw_adapter.quiz_favorite_songs import FavoriteSongIngestResult

    def _fake_ingest(self, youtube_url):
        assert youtube_url == "https://youtu.be/OIBODIPC_8Y"
        return FavoriteSongIngestResult(
            song_id=1,
            title="勇者",
            artist="YOASOBI",
            youtube_short_url="https://youtu.be/OIBODIPC_8Y",
            lyrics_url="https://www.uta-net.com/song/344130/",
            status="ready",
            sentence_count=12,
            token_count=88,
            n1_token_count=4,
        )

    monkeypatch.setattr(
        "openclaw_adapter.quiz_favorite_songs.FavoriteSongIngestor.ingest_youtube_song",
        _fake_ingest,
    )
    settings = SimpleNamespace(
        quiz_db_path=tmp_path / "quiz.sqlite3",
        openclaw_tls_insecure_skip_verify=False,
        openclaw_ca_bundle_path=None,
    )
    handler = build_quiz_handler(settings)
    got = handler("like song https://youtu.be/OIBODIPC_8Y", "u1")
    assert isinstance(got, str)
    assert "已加入最愛曲目" in got
    assert "歌曲：勇者" in got
    assert "N1 詞元：4" in got


def test_build_like_song_confirmation_success(monkeypatch):
    from openclaw_adapter.quiz_command import build_like_song_confirmation
    from openclaw_adapter.quiz_favorite_songs import YoutubeSongMetadata

    monkeypatch.setattr(
        "openclaw_adapter.quiz_favorite_songs.fetch_youtube_song_metadata",
        lambda **kwargs: YoutubeSongMetadata(
            video_id="OIBODIPC_8Y",
            youtube_url="https://www.youtube.com/watch?v=OIBODIPC_8Y",
            youtube_short_url="https://youtu.be/OIBODIPC_8Y",
            title="勇者",
            artist="YOASOBI",
            raw_title="YOASOBI「勇者」 Official Music Video",
        ),
    )

    rendered = build_like_song_confirmation(SimpleNamespace(), "https://youtu.be/OIBODIPC_8Y")
    assert rendered is not None
    text, markup = rendered
    assert "歌曲：勇者" in text
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "quiz:ls:OIBODIPC_8Y" for b in flat)
    assert any(b["callback_data"] == "quiz:lx:OIBODIPC_8Y" for b in flat)


def test_like_song_confirm_callback_runs_ingest(tmp_path, monkeypatch):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler
    from openclaw_adapter.quiz_favorite_songs import FavoriteSongIngestResult

    monkeypatch.setattr(
        "openclaw_adapter.quiz_favorite_songs.FavoriteSongIngestor.ingest_youtube_song",
        lambda self, youtube_url: FavoriteSongIngestResult(
            song_id=1,
            title="勇者",
            artist="YOASOBI",
            youtube_short_url="https://youtu.be/OIBODIPC_8Y",
            lyrics_url="https://www.uta-net.com/song/344130/",
            status="ready",
            sentence_count=63,
            token_count=337,
            n1_token_count=3,
        ),
    )
    settings = SimpleNamespace(
        quiz_db_path=tmp_path / "quiz.sqlite3",
        openclaw_tls_insecure_skip_verify=False,
        openclaw_ca_bundle_path=None,
    )
    handler = build_quiz_callback_handler(settings)
    toast, new_text, markup = handler(
        "ls:OIBODIPC_8Y",
        "🎵 偵測到 YouTube 歌曲連結",
        "u1",
    )
    assert toast == "已加入最愛"
    assert "歌曲：勇者" in new_text
    assert markup is None


def test_like_song_cancel_callback_clears_keyboard(tmp_path):
    from openclaw_adapter.quiz_command import build_quiz_callback_handler

    handler = build_quiz_callback_handler(SimpleNamespace(quiz_db_path=tmp_path / "quiz.sqlite3"))
    toast, new_text, markup = handler("lx:OIBODIPC_8Y", "proposal", "u1")
    assert toast == "已取消"
    assert "已取消加入最愛" in new_text
    assert markup is None
