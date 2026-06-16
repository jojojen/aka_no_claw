"""Unit tests for the independent quiz DB — no network, no Ollama."""
from __future__ import annotations

import sqlite3

import pytest

from openclaw_adapter.quiz_db import (
    QuizDatabase,
    build_grammar_card_summary,
    build_question_id,
    reading_question_targets_source_title,
    format_authoring_knowledge_block,
    infer_source_excerpt_type,
    is_grounded,
    youhou_uses_generic_template_stem,
    source_excerpt_vocab_example,
    source_excerpt_type_conflicts_with_exam_point,
    synonym_answer_restates_headword,
    vocab_example_is_low_value,
    youhou_target_word_presence_leaks,
)


def _db(tmp_path) -> QuizDatabase:
    return QuizDatabase(tmp_path / "quiz.sqlite3")


def _insert_sample(db, *, stem="次の___に入る語を選べ。", source_name="メルト", answer_index=1):
    return db.insert_question(
        level="JLPT N1",
        exam_point="文法",
        stem=stem,
        options=("だから", "ものの", "とはいえ", "ながら"),
        answer_index=answer_index,
        explanation="逆接の「ものの」が正しい。",
        source_type="vocaloid_song",
        source_name=source_name,
        source_text_url="https://example.com/lyrics",
        source_media_url="https://youtube.com/watch?v=x",
        source_excerpt="朝、目が覚めて…",
        verified=True,
        allow_ungrounded=True,
    )


class TestSchemaAndPragmas:
    def test_wal_journal_mode_enabled(self, tmp_path):
        db = _db(tmp_path)
        with db.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_tables_created(self, tmp_path):
        db = _db(tmp_path)
        with db.connect() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {
            "quiz_questions",
            "quiz_authoring_knowledge",
            "quiz_vocab_cards",
            "quiz_grammar_cards",
            "quiz_songs",
            "lyrics",
            "sentences",
            "vocabulary_tokens",
        } <= names

    def test_source_excerpt_type_column_exists(self, tmp_path):
        db = _db(tmp_path)
        with db.connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_questions)")}
        assert "source_excerpt_type" in cols

    def test_bootstrap_backfills_source_excerpt_type_for_legacy_rows(self, tmp_path):
        path = tmp_path / "quiz.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE quiz_questions (
                question_id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                exam_point TEXT NOT NULL,
                stem TEXT NOT NULL,
                options_json TEXT NOT NULL DEFAULT '[]',
                answer_index INTEGER NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'other',
                source_name TEXT NOT NULL DEFAULT '',
                source_text_url TEXT,
                source_media_url TEXT,
                source_excerpt TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                served_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Claude',
                tested_point TEXT
            );
            INSERT INTO quiz_questions (
                question_id, level, exam_point, stem, options_json, answer_index,
                explanation, source_type, source_name, source_text_url,
                source_media_url, source_excerpt, verified, served_count,
                created_at, updated_at, author, tested_point
            ) VALUES (
                'legacy-q',
                'JLPT N1',
                '漢字読み',
                '「ロストワンの号哭」の「号哭」の読み方として最も適切なものはどれか。',
                '["ごうこく","ごうきゅう","こうこく","こうきゅう"]',
                0,
                '',
                'vocaloid_song',
                'ロストワンの号哭',
                'https://www.uta-net.com/song/145551/',
                NULL,
                'ロストワンの号哭',
                1,
                0,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00',
                'codex',
                '号哭'
            );
            """
        )
        conn.commit()
        conn.close()

        db = QuizDatabase(path)
        row = db.get_question("legacy-q")
        assert row is not None
        assert row.source_excerpt_type == "title"

    def test_bootstrap_reinfers_existing_other_source_excerpt_type(self, tmp_path):
        path = tmp_path / "quiz.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE quiz_questions (
                question_id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                exam_point TEXT NOT NULL,
                stem TEXT NOT NULL,
                options_json TEXT NOT NULL DEFAULT '[]',
                answer_index INTEGER NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'other',
                source_name TEXT NOT NULL DEFAULT '',
                source_text_url TEXT,
                source_media_url TEXT,
                source_excerpt TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                served_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Claude',
                tested_point TEXT,
                source_excerpt_type TEXT NOT NULL DEFAULT 'other'
            );
            INSERT INTO quiz_questions (
                question_id, level, exam_point, stem, options_json, answer_index,
                explanation, source_type, source_name, source_text_url,
                source_media_url, source_excerpt, verified, served_count,
                created_at, updated_at, author, tested_point, source_excerpt_type
            ) VALUES (
                'legacy-q-other',
                'JLPT N1',
                '内容理解（短文）',
                '本文の内容として最も適切なものはどれか。',
                '["a","b","c","d"]',
                0,
                '',
                'vocaloid_song',
                '夜もすがら君想ふ',
                'https://utaten.com/specialArticle/index/5017',
                NULL,
                '仄暗い話題多い世の中',
                1,
                0,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00',
                'codex',
                NULL,
                'other'
            );
            """
        )
        conn.commit()
        conn.close()

        db = QuizDatabase(path)
        row = db.get_question("legacy-q-other")
        assert row is not None
        assert row.source_excerpt_type == "article"


class TestQuestions:
    def test_insert_and_get_roundtrip(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_sample(db)
        again = db.get_question(q.question_id)
        assert again is not None
        assert again.stem == q.stem
        assert again.options == ("だから", "ものの", "とはいえ", "ながら")
        assert again.answer_index == 1
        assert again.verified is True
        assert again.source_media_url == "https://youtube.com/watch?v=x"
        assert again.source_excerpt_type == "other"

    def test_question_id_is_deterministic(self):
        a = build_question_id(level="JLPT N1", source_name="メルト", stem="X")
        b = build_question_id(level="JLPT N1", source_name="メルト", stem="X")
        assert a == b

    def test_insert_rejects_too_few_options(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError):
            db.insert_question(
                level="JLPT N1",
                exam_point="文法",
                stem="bad",
                options=("only-one",),
                answer_index=0,
            )

    def test_insert_rejects_answer_index_out_of_range(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError):
            db.insert_question(
                level="JLPT N1",
                exam_point="文法",
                stem="bad",
                options=("a", "b"),
                answer_index=5,
            )

    def test_random_question_filters_by_level(self, tmp_path):
        db = _db(tmp_path)
        _insert_sample(db, stem="N1 q", source_name="songA")
        db.insert_question(
            level="JLPT N5",
            exam_point="単語",
            stem="N5 q",
            options=("a", "b", "c", "d"),
            answer_index=0,
            source_name="songB",
            allow_ungrounded=True,
        )
        picked = db.random_question(level="JLPT N1")
        assert picked is not None and picked.level == "JLPT N1"

    def test_random_question_prefers_unserved(self, tmp_path):
        db = _db(tmp_path)
        served = _insert_sample(db, stem="served", source_name="A")
        unserved = _insert_sample(db, stem="unserved", source_name="B")
        db.mark_served(served.question_id)
        # With one served and one not, prefer_unserved must surface the unserved.
        picked = db.random_question(level="JLPT N1", prefer_unserved=True)
        assert picked is not None and picked.question_id == unserved.question_id

    def test_count_and_delete(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_sample(db)
        assert db.count_verified(level="JLPT N1") == 1
        assert db.delete_question(q.question_id) is True
        assert db.count_verified(level="JLPT N1") == 0


class TestFavoriteSongs:
    def test_upsert_and_replace_analysis_roundtrip(self, tmp_path):
        from types import SimpleNamespace

        db = _db(tmp_path)
        song_id = db.upsert_favorite_song(
            title="勇者",
            artist="YOASOBI",
            youtube_url="https://www.youtube.com/watch?v=OIBODIPC_8Y",
            youtube_short_url="https://youtu.be/OIBODIPC_8Y",
            status="fetching",
            youtube_title_raw="YOASOBI「勇者」 Official Music Video",
            video_id="OIBODIPC_8Y",
        )
        db.replace_favorite_song_analysis(
            song_id=song_id,
            lyrics_url="https://www.uta-net.com/song/344130/",
            lyrics_text="まるで御伽の話\n終わり迎えた証",
            sentences=["まるで御伽の話", "終わり迎えた証"],
            tokens=[
                SimpleNamespace(
                    sentence_index=0,
                    surface="御伽",
                    dictionary_form="御伽",
                    reading="おとぎ",
                    pos="名詞,普通名詞,一般",
                    jlpt_level="N1",
                ),
                SimpleNamespace(
                    sentence_index=1,
                    surface="証",
                    dictionary_form="証",
                    reading="あかし",
                    pos="名詞,普通名詞,一般",
                    jlpt_level=None,
                ),
            ],
            status="ready",
        )
        row = db.get_favorite_song_by_youtube_short_url("https://youtu.be/OIBODIPC_8Y")
        assert row is not None
        assert row["status"] == "ready"
        assert row["lyrics_url"] == "https://www.uta-net.com/song/344130/"
        counts = db.favorite_song_analysis_counts(song_id)
        assert counts == {"sentences": 2, "tokens": 2, "n1_tokens": 1}
        picked = db.pick_favorite_song_token(jlpt_level="N1")
        assert picked is not None
        assert picked.song_title == "勇者"
        assert picked.dictionary_form == "御伽"
        assert picked.jlpt_level == "N1"

    def test_mark_favorite_token_used(self, tmp_path):
        from types import SimpleNamespace

        db = _db(tmp_path)
        song_id = db.upsert_favorite_song(
            title="勇者",
            artist="YOASOBI",
            youtube_url="https://www.youtube.com/watch?v=OIBODIPC_8Y",
            youtube_short_url="https://youtu.be/OIBODIPC_8Y",
            status="fetching",
        )
        db.replace_favorite_song_analysis(
            song_id=song_id,
            lyrics_url="https://www.uta-net.com/song/344130/",
            lyrics_text="まるで御伽の話",
            sentences=["まるで御伽の話"],
            tokens=[
                SimpleNamespace(
                    sentence_index=0,
                    surface="御伽",
                    dictionary_form="御伽",
                    reading="おとぎ",
                    pos="名詞,普通名詞,一般",
                    jlpt_level="N1",
                ),
            ],
            status="ready",
        )
        picked = db.pick_favorite_song_token(jlpt_level="N1")
        assert picked is not None
        assert db.mark_favorite_token_used(token_id=picked.token_id, usage="quiz") is True
        picked_after = db.pick_favorite_song_token(jlpt_level="N1", unused_only=False)
        assert picked_after is not None
        assert picked_after.used_quiz_count == 1


class TestAuthorColumn:
    def test_fresh_db_defaults_author_to_claude(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_sample(db)
        assert db.get_question(q.question_id).author == "Claude"

    def test_bootstrap_is_idempotent(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_sample(db)
        # Re-running bootstrap (as happens on every startup) must not raise nor
        # disturb existing rows — the PRAGMA guard skips a second ADD COLUMN.
        db.bootstrap()
        db.bootstrap()
        assert db.count_verified(level="JLPT N1") == 1
        assert db.get_question(q.question_id).author == "Claude"

    def test_non_default_author_roundtrips(self, tmp_path):
        db = _db(tmp_path)
        q = db.insert_question(
            level="JLPT N1",
            exam_point="文法",
            stem="codex authored",
            options=("a", "b", "c", "d"),
            answer_index=0,
            source_name="codexsong",
            author="codex",
            allow_ungrounded=True,
        )
        assert db.get_question(q.question_id).author == "codex"


class TestVocabCards:
    def test_title_source_excerpt_does_not_backfill_vocab_card(self, tmp_path):
        db = _db(tmp_path)
        db.upsert_vocab_seed("25時の情熱", "にごじのじょうねつ", "歌曲名稱")
        with pytest.raises(ValueError, match="reading item targets source title"):
            db.insert_question(
                level="JLPT N1",
                exam_point="漢字読み",
                stem="次の一節「25時の情熱」にある〈25時の情熱〉の読み方として最も適切なものはどれか。",
                options=("にごじのじょうねつ", "にじゅうごじのじょうねつ", "にごときのねつ", "ごじのじょうねつ"),
                answer_index=0,
                explanation="〈25時の情熱〉は「にごじのじょうねつ」と読む。",
                source_type="vocaloid_song",
                source_name="25時の情熱",
                source_text_url="https://vocadb.net/Search?searchType=Song&filter=Songs&query=25%E6%99%82%E3%81%AE%E6%83%85%E7%86%B1",
                source_media_url="https://youtu.be/RifILHUOV_w",
                source_excerpt="25時の情熱",
                source_excerpt_type="title",
                tested_point="25時の情熱",
                author="codex",
                verified=True,
                allow_ungrounded=True,
            )
        assert db.get_vocab_card(headword="25時の情熱", level="JLPT N1") is None

    def test_vocab_example_low_value_detection(self):
        assert vocab_example_is_low_value("退路", "「退路」の意味を調べた。")
        assert not vocab_example_is_low_value("退路", "退路を断たれた。")

    def test_source_excerpt_example_requires_real_non_title_sentence(self):
        assert source_excerpt_vocab_example(
            headword="範疇",
            source_excerpt="プログラムの範疇さ",
            source_excerpt_type="lyric",
        ) == "プログラムの範疇さ"
        assert source_excerpt_vocab_example(
            headword="威風堂々",
            source_excerpt="威風堂々",
            source_excerpt_type="title",
        ) is None
        assert source_excerpt_vocab_example(
            headword="周年",
            source_excerpt="プロセカ1周年曲『",
            source_excerpt_type="article",
        ) is None

    def test_source_excerpt_example_rejects_whole_lyric_blob(self):
        # A lyric excerpt with no sentence delimiters collapses into one giant
        # blob; that is not a usable single-line card example.
        blob = "あ" * 40 + "矛盾" + "い" * 40
        assert source_excerpt_vocab_example(
            headword="矛盾",
            source_excerpt=blob,
            source_excerpt_type="lyric",
        ) is None
        assert source_excerpt_vocab_example(
            headword="矛盾",
            source_excerpt="矛盾を抱えて生きてくなんて怒られてしまう。",
            source_excerpt_type="lyric",
        ) == "矛盾を抱えて生きてくなんて怒られてしまう。"

    def test_source_excerpt_example_splits_japanese_sentences_without_spaces(self):
        excerpt = (
            "何度も辞めたい、逃げたいと思うけれど、心のどこかでは明日と自分に期待している。"
            "今は葛藤ばかりの主人公ですが、昔は大きな夢があった様子。"
            "その時の思いや夢を大切にしまっているからこそ、人生を簡単に諦めることはできない。"
        )
        assert source_excerpt_vocab_example(
            headword="葛藤",
            source_excerpt=excerpt,
            source_excerpt_type="article",
        ) == "今は葛藤ばかりの主人公ですが、昔は大きな夢があった様子。"

    def test_source_excerpt_example_extracts_short_window_from_lyric_blob(self):
        excerpt = (
            "【文章A】きっかけは自分だったのです 傷を付けてしまったのです 放っておけば自然消滅 "
            "でも 痛みがいちいち主張してくるよ 【文章B】別の歌詞が続く"
        )
        got = source_excerpt_vocab_example(
            headword="自然消滅",
            source_excerpt=excerpt,
            source_excerpt_type="lyric",
        )
        assert got is not None
        assert "自然消滅" in got
        assert len(got) <= 70

    def test_source_excerpt_example_caps_selected_example_to_three_sentences(self):
        excerpt = "葛藤だ。葛藤だよ。葛藤なんだ。葛藤かもしれない。"
        got = source_excerpt_vocab_example(
            headword="葛藤",
            source_excerpt=excerpt,
            source_excerpt_type="article",
        )
        assert got in {"葛藤だ。", "葛藤だよ。", "葛藤なんだ。", "葛藤かもしれない。"}

    def test_backfill_skips_no_example_question_and_keeps_example_author(self, tmp_path):
        # An article question (yields no card example) must not sink the card
        # when a lyric question for the same headword yields a real example.
        # The card's author badge follows the example actually shown.
        db = _db(tmp_path)
        db.upsert_vocab_seed("矛盾", "むじゅん", "矛盾、自相矛盾")
        db.insert_question(
            level="JLPT N1",
            exam_point="漢字読み",
            stem="解説中の〈矛盾〉の読み方として最も適切なものはどれか。",
            options=("むじゅん", "ぼうじゅん", "ほこじゅん", "むとん"),
            answer_index=0,
            explanation="〈矛盾〉は「むじゅん」と読む。",
            source_type="vocaloid_song",
            source_name="命に嫌われている。",
            source_text_url="https://example.com/article",
            source_excerpt="この曲の前半部分の矛盾や見えない敵の存在を合わせて考えると主題が見える。",
            source_excerpt_type="article",
            tested_point="矛盾",
            author="codex",
            verified=True,
            allow_ungrounded=True,
        )
        db.insert_question(
            level="JLPT N1",
            exam_point="漢字読み",
            stem="「矛盾を抱えて生きてくなんて怒られてしまう。」の〈矛盾〉の読み方はどれか。",
            options=("むじゅん", "ぼうじゅん", "ほこじゅん", "むとん"),
            answer_index=0,
            explanation="〈矛盾〉は「むじゅん」と読む。",
            source_type="vocaloid_song",
            source_name="命に嫌われている。",
            source_text_url="https://example.com/lyric",
            source_excerpt="矛盾を抱えて生きてくなんて怒られてしまう。",
            source_excerpt_type="lyric",
            tested_point="矛盾",
            author="Claude",
            verified=True,
            allow_ungrounded=True,
        )
        card = db.get_vocab_card(headword="矛盾", level="JLPT N1")
        assert card is not None
        assert card.example_ja == "矛盾を抱えて生きてくなんて怒られてしまう。"
        assert card.author == "Claude"

    def test_insert_codex_lexical_question_backfills_vocab_card(self, tmp_path):
        db = _db(tmp_path)
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
        card = db.get_vocab_card(headword="範疇", level="JLPT N1")
        assert card is not None
        assert card.reading_hiragana == "はんちゅう"
        assert card.zh_gloss_short == "範圍、類別"
        assert card.example_ja == "プログラムの範疇さ"
        assert card.example_source_kind == "source_excerpt"
        assert card.source_name == "ダンスロボットダンス"
        assert card.source_media_url == "https://www.youtube.com/watch?v=g7dvpD_zlIM"
        assert card.exam_points == ("漢字読み",)

    def test_vocab_cards_do_not_share_same_example_sentence(self, tmp_path):
        db = _db(tmp_path)
        db.upsert_vocab_seed("範疇A", "はんちゅうえー", "範疇A")
        db.upsert_vocab_seed("範疇B", "はんちゅうびー", "範疇B")
        for term, answer in (("範疇A", 0), ("範疇B", 1)):
            db.insert_question(
                level="JLPT N1",
                exam_point="漢字読み",
                stem=f"次の一節「範疇Aと範疇Bを比べる」にある〈{term}〉の読み方として最も適切なものはどれか。",
                options=("はんちゅうえー", "はんちゅうびー", "ばんちゅうえー", "ばんちゅうびー"),
                answer_index=answer,
                explanation=f"〈{term}〉の読みを問う。",
                source_type="vocaloid_song",
                source_name="テスト曲",
                source_text_url="https://example.com/article",
                source_media_url="https://youtu.be/example",
                source_excerpt="範疇Aと範疇Bを比べる",
                source_excerpt_type="article",
                tested_point=term,
                author="codex",
                verified=True,
                allow_ungrounded=True,
            )

        cards = [
            db.get_vocab_card(headword="範疇A", level="JLPT N1"),
            db.get_vocab_card(headword="範疇B", level="JLPT N1"),
        ]
        assert sum(card is not None for card in cards) == 1

    def test_vocab_card_prefers_youhou_as_primary_when_same_headword_repeats(self, tmp_path):
        db = _db(tmp_path)
        db.upsert_vocab_seed("範疇", "はんちゅう", "範圍、類別")
        read_q = db.insert_question(
            level="JLPT N1",
            exam_point="漢字読み",
            stem="「プログラムの範疇さ」の〈範疇〉の読み方として最も適切なものはどれか。",
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
        youhou_q = db.insert_question(
            level="JLPT N1",
            exam_point="用法",
            stem="業務の範囲を説明する文脈で、〈範疇〉の使い方として最も適切なものはどれか。",
            options=("彼の態度は範疇だ。", "これは業務の範疇だ。", "範疇が速く走る。", "範疇を飲んだ。"),
            answer_index=1,
            explanation="「範疇」は物事の範囲・カテゴリーを表す。",
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
        assert card.primary_question_id == youhou_q.question_id
        assert card.support_question_ids == (youhou_q.question_id, read_q.question_id)
        assert card.exam_points == ("用法", "漢字読み")


class TestGrounding:
    """The hard invariant: a question's user-visible real text must be a verbatim
    substring of the real source_excerpt. Fabricated stems must be rejected."""

    LYRIC = "悲しみの海に沈んだ私　このままどこまでも堕ちて行き"

    def test_cloze_on_real_line_is_grounded(self):
        # Stem IS the real lyric line with a grammatical element blanked → grounded.
        assert is_grounded(
            exam_point="文法形式の判断",
            stem="悲しみの海に沈んだ私、このまま（　　）堕ちて行く。",
            options=("どこまでも", "あえて", "しぶしぶ", "むやみに"),
            answer_index=0,
            source_excerpt=self.LYRIC,
        )

    def test_fabricated_stem_is_rejected(self):
        # The 深海少女 bug: stem is a made-up sentence, not the real lyric.
        assert not is_grounded(
            exam_point="文法形式の判断",
            stem="黒い海に行く手を阻まれ、少女は深く沈むこと（　　）。",
            options=("には当たらない", "に堪えなかった", "を余儀なくされた", "をものともしなかった"),
            answer_index=2,
            source_excerpt=self.LYRIC,
        )

    def test_single_line_cloze_against_multiline_excerpt_is_grounded(self):
        # Regression: a single blanked lyric line must ground even when the excerpt
        # spans many lines. The old check required the carrier to cover 60% of the
        # WHOLE excerpt, so multi-line excerpts rejected every genuine 文脈規定 cloze.
        multiline = (
            "悲しみの海に沈んだ私　このままどこまでも堕ちて行き／"
            "夜空に光る星を数えて　明日が来るのを待ち続ける／"
            "君の声が遠くで響いて　眠れない夜を越えていく"
        )
        assert is_grounded(
            exam_point="文脈規定",
            stem="夜空に光る星を（　　）、明日が来るのを待ち続ける。",
            options=("数えて", "忘れて", "壊して", "投げて"),
            answer_index=0,
            source_excerpt=multiline,
        )

    def test_fabricated_cloze_against_multiline_excerpt_is_rejected(self):
        # The fix must NOT open a fabrication hole: a made-up carrier that only shares
        # a short fragment with a multi-line excerpt is still rejected.
        multiline = (
            "悲しみの海に沈んだ私　このままどこまでも堕ちて行き／"
            "夜空に光る星を数えて　明日が来るのを待ち続ける"
        )
        assert not is_grounded(
            exam_point="文脈規定",
            stem="未来への希望を胸に抱いて、彼は新たな一歩を（　　）。",
            options=("踏み出した", "諦めた", "見失った", "忘れた"),
            answer_index=0,
            source_excerpt=multiline,
        )

    def test_iikae_quotes_real_line_is_grounded(self):
        # 言い換え: the real line sits inside the stem (excerpt ⊆ stem).
        assert is_grounded(
            exam_point="言い換え類義",
            stem="「〈大胆不敵〉にハイカラ革命」の〈大胆不敵〉に最も近い意味はどれか。",
            options=("恐れを知らず大胆な", "用心深く慎重な", "礼儀正しい", "おとなしい"),
            answer_index=0,
            source_excerpt="大胆不敵にハイカラ革命",
        )

    def test_youhou_correct_option_must_be_real_line(self):
        # 用法: correct option = the real lyric line → grounded; otherwise rejected.
        assert is_grounded(
            exam_point="用法",
            stem="「蔑む」の使い方として最も適切なものはどれか。",
            options=(
                "吐き出す様な暴力と蔑んだ目の毎日に",
                "彼は努力を蔑んで合格した",
                "蔑む音楽が好きだ",
                "蔑んだ料理を食べた",
            ),
            answer_index=0,
            source_excerpt="吐き出す様な暴力と　蔑んだ目の毎日に",
        )
        assert not is_grounded(
            exam_point="用法",
            stem="「蔑む」の使い方として最も適切なものはどれか。",
            options=("彼を蔑む", "蔑む朝", "蔑む色", "蔑む音"),
            answer_index=0,
            source_excerpt="吐き出す様な暴力と　蔑んだ目の毎日に",
        )

    def test_reading_type_grounded_by_excerpt_presence(self):
        # 内容理解: the 本文 (== excerpt) is rendered verbatim → grounded.
        assert is_grounded(
            exam_point="内容理解（短文）",
            stem="次の文章を読み、語り手の心情として最も適切なものはどれか。",
            options=("a", "b", "c", "d"),
            answer_index=0,
            source_excerpt="こんな僕が生きてるだけで　何万人のひとが悲しんで",
        )

    def test_explanation_citing_equivalent_line_is_grounded(self):
        # Tier C: a constructed grammar stem is acceptable IF the explanation quotes
        # the equivalent real lyric line verbatim (等価於哪句原歌詞).
        assert is_grounded(
            exam_point="文法形式の判断",
            stem="本当の気持ちを素直に言え（　　）だった。",
            options=("がてら", "なり", "そばから", "ずじまい"),
            answer_index=3,
            source_excerpt="まだ素直に言葉に出来ない僕は天性の弱虫さ",
            explanation="原歌詞「まだ素直に言葉に出来ない僕は天性の弱虫さ」に対応。「ずじまい」が正解。",
        )

    def test_fabricated_without_citation_is_rejected(self):
        assert not is_grounded(
            exam_point="文法形式の判断",
            stem="本当の気持ちを素直に言え（　　）だった。",
            options=("がてら", "なり", "そばから", "ずじまい"),
            answer_index=3,
            source_excerpt="まだ素直に言葉に出来ない僕は天性の弱虫さ",
            explanation="「ずじまい」は結局～しなかったの意。",
        )

    def test_empty_excerpt_is_rejected(self):
        assert not is_grounded(
            exam_point="文法形式の判断",
            stem="何か（　　）。",
            options=("a", "b", "c", "d"),
            answer_index=0,
            source_excerpt=None,
        )

    def test_insert_rejects_ungrounded(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError):
            db.insert_question(
                level="JLPT N1",
                exam_point="文法形式の判断",
                stem="黒い海に行く手を阻まれ、少女は深く沈むこと（　　）。",
                options=("には当たらない", "に堪えなかった", "を余儀なくされた", "をものともしなかった"),
                answer_index=2,
                source_name="深海少女",
                source_excerpt="悲しみの海に沈んだ私　このままどこまでも堕ちて行き",
            )


class TestSourceExcerptType:
    def test_title_is_inferred_from_exact_source_name_match(self):
        assert infer_source_excerpt_type(
            source_text_url="https://example.com/anything",
            source_excerpt="ロストワンの号哭",
            source_name="ロストワンの号哭",
        ) == "title"

    def test_lyric_and_article_urls_are_inferred(self):
        assert infer_source_excerpt_type(
            source_text_url="https://utaten.com/lyric/iz16122110/",
            source_excerpt="いがみ合ってきりがないな",
            source_name="シャルル",
        ) == "lyric"
        assert infer_source_excerpt_type(
            source_text_url="https://utaten.com/specialArticle/index/7343",
            source_excerpt="ビビバスへの書き下ろし楽曲です。",
            source_name="Flyer!",
        ) == "article"

    def test_conflict_rules_are_conservative(self):
        assert not source_excerpt_type_conflicts_with_exam_point(
            exam_point="言い換え類義", source_excerpt_type="article"
        )
        assert not source_excerpt_type_conflicts_with_exam_point(
            exam_point="用法", source_excerpt_type="commentary"
        )
        assert source_excerpt_type_conflicts_with_exam_point(
            exam_point="文脈規定", source_excerpt_type="title"
        )
        assert not source_excerpt_type_conflicts_with_exam_point(
            exam_point="漢字読み", source_excerpt_type="title"
        )
        assert not source_excerpt_type_conflicts_with_exam_point(
            exam_point="内容理解（短文）", source_excerpt_type="article"
        )

    def test_insert_accepts_verbatim_article_grounding_for_vocabulary(self, tmp_path):
        db = _db(tmp_path)
        q = db.insert_question(
            level="JLPT N1",
            exam_point="言い換え類義",
            stem="次の一節「疾走感溢れる四つ打ちのタイトなリズム」にある〈疾走感〉の意味として最も近いものはどれか。",
            options=("速く走るような勢い", "遅い感じ", "静けさ", "懐かしさ"),
            answer_index=0,
            source_name="命に嫌われている",
            source_text_url="https://utaten.com/specialArticle/index/9999",
            source_excerpt="疾走感溢れる四つ打ちのタイトなリズム",
            tested_point="疾走感",
            author="codex",
        )
        assert q.source_excerpt_type == "article"

    def test_insert_still_rejects_fabricated_article_grounding_for_vocabulary(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="question not grounded"):
            db.insert_question(
                level="JLPT N1",
                exam_point="言い換え類義",
                stem="次の一節「疾走するような勢いがある曲」にある〈疾走感〉の意味として最も近いものはどれか。",
                options=("速く走るような勢い", "遅い感じ", "静けさ", "懐かしさ"),
                answer_index=0,
                source_name="命に嫌われている",
                source_text_url="https://utaten.com/specialArticle/index/9999",
                source_excerpt="疾走感溢れる四つ打ちのタイトなリズム",
                tested_point="疾走感",
                author="codex",
            )


class TestYouhouLeakGuard:
    def test_detects_generic_youhou_template_stem(self):
        assert youhou_uses_generic_template_stem(
            exam_point="用法",
            stem="次のうち、語句「魅了する」の使い方として最も適切な文はどれか。",
        )
        assert youhou_uses_generic_template_stem(
            exam_point="用法",
            stem="「浪費」の使い方として最も適切なものはどれか。",
        )
        assert not youhou_uses_generic_template_stem(
            exam_point="用法",
            stem="困難な課題に直面する意の「壁にぶち当たる」の使い方として最も適切なものはどれか。",
        )

    def test_detects_target_word_presence_leak(self):
        assert youhou_target_word_presence_leaks(
            exam_point="用法",
            stem="次のうち、語句「魅了する」の使い方として最も適切な文はどれか。",
            options=(
                "重なる波形に魅了されていく",
                "甘い香りに魅惑されて、しばらくその場を離れられなかった",
                "観客は演奏に陶酔して、終演後もしばらく席を立てなかった",
                "不思議な光に惹きつけられて、彼は思わず足を止めた",
            ),
            answer_index=0,
        )

    def test_clean_youhou_not_flagged(self):
        assert not youhou_target_word_presence_leaks(
            exam_point="用法",
            stem="次のうち、語句「魅了する」の使い方として最も適切な文はどれか。",
            options=(
                "重なる波形に魅了されていく",
                "観客は演奏を魅了されて、終演後もしばらく席を立てなかった",
                "甘い香りが彼に魅了して、店先で足を止めさせた",
                "その演説は聴衆に魅了され、多くの支持を集めた",
            ),
            answer_index=0,
        )

    def test_insert_rejects_youhou_presence_leak(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="youhou item leaks"):
            db.insert_question(
                level="JLPT N1",
                exam_point="用法",
                stem="観客が強く引きつけられる場面で、語句「魅了する」の使い方として最も適切な文はどれか。",
                options=(
                    "重なる波形に魅了されていく",
                    "甘い香りに魅惑されて、しばらくその場を離れられなかった",
                    "観客は演奏に陶酔して、終演後もしばらく席を立てなかった",
                    "不思議な光に惹きつけられて、彼は思わず足を止めた",
                ),
                answer_index=0,
                source_name="ヒビカセ",
                source_text_url="http://vgperson.com/lyrics.php?song=hibikase",
                source_excerpt="重なる波形に魅了されていく",
            )

    def test_insert_rejects_generic_youhou_template_stem(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="generic template stem"):
            db.insert_question(
                level="JLPT N1",
                exam_point="用法",
                stem="次のうち、語句「魅了する」の使い方として最も適切な文はどれか。",
                options=(
                    "重なる波形に魅了されていく",
                    "観客は演奏を魅了されて、終演後もしばらく席を立てなかった",
                    "甘い香りが彼に魅了して、店先で足を止めさせた",
                    "その演説は聴衆に魅了され、多くの支持を集めた",
                ),
                answer_index=0,
                source_name="ヒビカセ",
                source_text_url="http://vgperson.com/lyrics.php?song=hibikase",
                source_excerpt="重なる波形に魅了されていく",
            )


class TestAuthoringKnowledge:
    def test_upsert_retrieve_and_block(self, tmp_path):
        db = _db(tmp_path)
        db.upsert_authoring_knowledge(
            category="distractor_design",
            title="干擾項要同詞性",
            technique="干擾選項應與正解同詞性、長度相近，避免一眼排除。",
            keywords=("distractor", "干擾", "詞性"),
            origin="seed",
            confidence=0.7,
        )
        rows = db.retrieve_authoring_knowledge("distractor 干擾 設計", k=6)
        assert rows and rows[0].title == "干擾項要同詞性"
        block = format_authoring_knowledge_block(rows)
        assert "干擾項要同詞性" in block

    def test_higher_confidence_wins(self, tmp_path):
        db = _db(tmp_path)
        db.upsert_authoring_knowledge(
            category="grammar", title="T", technique="low", confidence=0.3, origin="seed"
        )
        db.upsert_authoring_knowledge(
            category="grammar", title="T", technique="high", confidence=0.9, origin="distilled"
        )
        rows = db.all_authoring_knowledge()
        assert len(rows) == 1 and rows[0].technique == "high"
        # A lower-confidence write must not clobber the stronger rule.
        db.upsert_authoring_knowledge(
            category="grammar", title="T", technique="lower", confidence=0.1, origin="seed"
        )
        rows = db.all_authoring_knowledge()
        assert rows[0].technique == "high"

    def test_delete_authoring(self, tmp_path):
        db = _db(tmp_path)
        entry = db.upsert_authoring_knowledge(
            category="reading", title="R", technique="t", confidence=0.5, origin="seed"
        )
        assert db.delete_authoring_knowledge(entry.knowledge_id) is True
        assert db.all_authoring_knowledge() == []


def _insert_q(db, *, stem, exam_point, tested_point=None, source_name="S", answer_index=1):
    return db.insert_question(
        level="JLPT N1",
        exam_point=exam_point,
        stem=stem,
        options=("a", "b", "c", "d"),
        answer_index=answer_index,
        explanation="x",
        source_type="vocaloid_song",
        source_name=source_name,
        source_excerpt="朝、目が覚めて…",
        tested_point=tested_point,
        verified=True,
        allow_ungrounded=True,
    )


class TestAdaptiveSelection:
    def test_tested_point_roundtrips(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_q(db, stem="q1", exam_point="用法", tested_point="操る")
        got = db.get_question(q.question_id)
        assert got.tested_point == "操る"

    def test_record_attempt_and_mastery_stats(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_q(db, stem="q1", exam_point="用法", tested_point="操る")
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=0, correct=False)
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=1, correct=True)
        s = db.mastery_stats(chat_id="u1")
        assert s["total"] == 2
        ep = {r["key"]: r for r in s["by_type"]}["用法"]
        assert ep["attempts"] == 2 and ep["corrects"] == 1
        tp = {r["key"]: r for r in s["by_point"]}["操る"]
        assert tp["attempts"] == 2 and tp["corrects"] == 1

    def test_mastery_stats_isolated_per_chat(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_q(db, stem="q1", exam_point="用法", tested_point="操る")
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=0, correct=False)
        assert db.mastery_stats(chat_id="u2")["total"] == 0

    def test_weighted_question_biases_toward_weak_exam_point(self, tmp_path):
        import random
        db = _db(tmp_path)
        strong = _insert_q(db, stem="strong", exam_point="漢字読み",
                           tested_point="A", source_name="S1")
        weak = _insert_q(db, stem="weak", exam_point="文法形式の判断",
                         tested_point="B", source_name="S2")
        # Build history: always right on 漢字読み, always wrong on 文法.
        for _ in range(6):
            db.record_attempt(question_id=strong.question_id, exam_point="漢字読み",
                              tested_point="A", level="JLPT N1", chat_id="u1",
                              chosen_index=1, correct=True)
            db.record_attempt(question_id=weak.question_id, exam_point="文法形式の判断",
                              tested_point="B", level="JLPT N1", chat_id="u1",
                              chosen_index=0, correct=False)
        rng = random.Random(42)
        picks = [db.weighted_question(level="JLPT N1", chat_id="u1", rng=rng).question_id
                 for _ in range(200)]
        weak_n = picks.count(weak.question_id)
        assert weak_n > picks.count(strong.question_id)

    def test_weighted_question_biases_toward_weak_tested_point(self, tmp_path):
        import random
        db = _db(tmp_path)
        # Same 題型, two specific 考点 — only one is weak.
        easy = _insert_q(db, stem="easy", exam_point="用法",
                         tested_point="操る", source_name="S1")
        hard = _insert_q(db, stem="hard", exam_point="用法",
                         tested_point="贖う", source_name="S2")
        for _ in range(6):
            db.record_attempt(question_id=easy.question_id, exam_point="用法",
                              tested_point="操る", level="JLPT N1", chat_id="u1",
                              chosen_index=1, correct=True)
            db.record_attempt(question_id=hard.question_id, exam_point="用法",
                              tested_point="贖う", level="JLPT N1", chat_id="u1",
                              chosen_index=0, correct=False)
        rng = random.Random(7)
        picks = [db.weighted_question(level="JLPT N1", chat_id="u1", rng=rng).question_id
                 for _ in range(200)]
        assert picks.count(hard.question_id) > picks.count(easy.question_id)

    def test_weighted_question_cold_start_returns_something(self, tmp_path):
        import random
        db = _db(tmp_path)
        _insert_q(db, stem="q1", exam_point="用法", tested_point="操る")
        got = db.weighted_question(level="JLPT N1", chat_id="u1", rng=random.Random(1))
        assert got is not None

    def test_weighted_question_exclude_id_skips_when_alternatives_exist(self, tmp_path):
        import random
        db = _db(tmp_path)
        a = _insert_q(db, stem="qa", exam_point="用法", tested_point="操る", source_name="S1")
        b = _insert_q(db, stem="qb", exam_point="用法", tested_point="贖う", source_name="S2")
        for _ in range(20):
            got = db.weighted_question(level="JLPT N1", chat_id="u1",
                                       exclude_id=a.question_id, rng=random.Random(_))
            assert got.question_id == b.question_id

    def test_weighted_question_restricts_to_exam_point(self, tmp_path):
        import random
        db = _db(tmp_path)
        _insert_q(db, stem="kanji", exam_point="漢字読み", tested_point="A", source_name="S1")
        target = _insert_q(db, stem="bunpou", exam_point="文法形式の判断",
                           tested_point="B", source_name="S2")
        for _ in range(30):
            got = db.weighted_question(level="JLPT N1", chat_id="u1",
                                       exam_point="文法形式の判断", rng=random.Random(_))
            assert got.question_id == target.question_id

    def test_weighted_question_exam_point_no_match_returns_none(self, tmp_path):
        import random
        db = _db(tmp_path)
        _insert_q(db, stem="kanji", exam_point="漢字読み", tested_point="A")
        got = db.weighted_question(level="JLPT N1", chat_id="u1",
                                   exam_point="情報検索", rng=random.Random(1))
        assert got is None

    def test_exam_point_counts_groups_and_orders(self, tmp_path):
        db = _db(tmp_path)
        _insert_q(db, stem="k1", exam_point="漢字読み", tested_point="A", source_name="S1")
        _insert_q(db, stem="k2", exam_point="漢字読み", tested_point="B", source_name="S2")
        _insert_q(db, stem="g1", exam_point="文法形式の判断", tested_point="C", source_name="S3")
        counts = db.exam_point_counts(level="JLPT N1")
        assert counts[0] == ("漢字読み", 2)  # most-populous first
        assert ("文法形式の判断", 1) in counts

    def test_exam_point_counts_excludes_unverified(self, tmp_path):
        db = _db(tmp_path)
        _insert_q(db, stem="k1", exam_point="漢字読み", tested_point="A")
        db.insert_question(
            level="JLPT N1", exam_point="用法", stem="u1",
            options=("a", "b", "c", "d"), answer_index=0, explanation="x",
            source_type="vocaloid_song", source_name="S9",
            source_excerpt="朝、目が覚めて…", tested_point="Z",
            verified=False, allow_ungrounded=True,
        )
        eps = dict(db.exam_point_counts(level="JLPT N1"))
        assert "漢字読み" in eps and "用法" not in eps


class TestWrongNotebook:
    def test_wrong_only_serves_last_wrong_question(self, tmp_path):
        import random
        db = _db(tmp_path)
        wrong = _insert_q(db, stem="qw", exam_point="用法", tested_point="操る", source_name="S1")
        right = _insert_q(db, stem="qr", exam_point="用法", tested_point="贖う", source_name="S2")
        db.record_attempt(question_id=wrong.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=0, correct=False)
        db.record_attempt(question_id=right.question_id, exam_point="用法",
                          tested_point="贖う", level="JLPT N1", chat_id="u1",
                          chosen_index=1, correct=True)
        for _ in range(10):
            got = db.weighted_question(level="JLPT N1", chat_id="u1",
                                       wrong_only=True, rng=random.Random(_))
            assert got is not None and got.question_id == wrong.question_id

    def test_wrong_only_drops_after_correction(self, tmp_path):
        import random
        db = _db(tmp_path)
        q = _insert_q(db, stem="qw", exam_point="用法", tested_point="操る")
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=0, correct=False)
        assert db.weighted_question(level="JLPT N1", chat_id="u1",
                                    wrong_only=True, rng=random.Random(1)) is not None
        # re-answer correctly → most recent attempt is right → drops out
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=1, correct=True)
        assert db.weighted_question(level="JLPT N1", chat_id="u1",
                                    wrong_only=True, rng=random.Random(1)) is None

    def test_wrong_only_isolated_per_chat(self, tmp_path):
        import random
        db = _db(tmp_path)
        q = _insert_q(db, stem="qw", exam_point="用法", tested_point="操る")
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="操る", level="JLPT N1", chat_id="u1",
                          chosen_index=0, correct=False)
        assert db.weighted_question(level="JLPT N1", chat_id="u2",
                                    wrong_only=True, rng=random.Random(1)) is None


class TestConfusionPairs:
    def _q4(self, db, *, exam_point, opts, answer_index, source_name):
        return db.insert_question(
            level="JLPT N1", exam_point=exam_point, stem="s",
            options=opts, answer_index=answer_index, explanation="x",
            source_type="vocaloid_song", source_name=source_name,
            source_excerpt="朝、目が覚めて…", tested_point="tp",
            verified=True, allow_ungrounded=True,
        )

    def test_confusion_pair_counts_recurring_mistake(self, tmp_path):
        db = _db(tmp_path)
        q = self._q4(db, exam_point="文法形式の判断",
                     opts=("にあって", "にして", "にいたって", "において"),
                     answer_index=0, source_name="S1")
        for _ in range(3):
            db.record_attempt(question_id=q.question_id, exam_point="文法形式の判断",
                              tested_point="tp", level="JLPT N1", chat_id="u1",
                              chosen_index=1, correct=False)
        pairs = db.confusion_pairs(chat_id="u1")
        assert pairs and pairs[0]["correct"] == "にあって"
        assert pairs[0]["chosen"] == "にして" and pairs[0]["count"] == 3

    def test_confusion_skips_reading_types(self, tmp_path):
        db = _db(tmp_path)
        q = self._q4(db, exam_point="内容理解（短文）",
                     opts=("長い文A", "長い文B", "長い文C", "長い文D"),
                     answer_index=0, source_name="S1")
        db.record_attempt(question_id=q.question_id, exam_point="内容理解（短文）",
                          tested_point="tp", level="JLPT N1", chat_id="u1",
                          chosen_index=1, correct=False)
        assert db.confusion_pairs(chat_id="u1") == []

    def test_confusion_ignores_correct_answers(self, tmp_path):
        db = _db(tmp_path)
        q = self._q4(db, exam_point="用法", opts=("a", "b", "c", "d"),
                     answer_index=2, source_name="S1")
        db.record_attempt(question_id=q.question_id, exam_point="用法",
                          tested_point="tp", level="JLPT N1", chat_id="u1",
                          chosen_index=2, correct=True)
        assert db.confusion_pairs(chat_id="u1") == []


class TestDeriveTestedPoint:
    def test_kanji_reading_word(self):
        from openclaw_adapter.quiz_db import derive_tested_point
        s = "「その〈断頭台〉で見下ろして」の〈断頭台〉の読み方として正しいものはどれか。"
        assert derive_tested_point(exam_point="漢字読み", stem=s, options=[], answer_index=0) == "断頭台"

    def test_kanji_reading_single_bracket_variant(self):
        from openclaw_adapter.quiz_db import derive_tested_point
        s = "「〈古今未曾有〉」の読み方として正しいものはどれか。"
        assert derive_tested_point(exam_point="漢字読み", stem=s, options=[], answer_index=0) == "古今未曾有"

    def test_iikae_meaning_variant_phrasings(self):
        from openclaw_adapter.quiz_db import derive_tested_point
        for s, want in [
            ("「〈みっともない〉暮らしにもうバイバイ」の〈みっともない〉に最も近い意味はどれか。", "みっともない"),
            ("「怒涛の時代を生きた日本人」の「怒涛」の意味に最も近いものはどれか。", "怒涛"),
            ("「うまく周囲に溶け込めず悩んでいる」の「溶け込めず」に意味が最も近いものはどれか。", "溶け込めず"),
        ]:
            assert derive_tested_point(exam_point="言い換え類義", stem=s, options=[], answer_index=0) == want

    def test_youhou_leading_word(self):
        from openclaw_adapter.quiz_db import derive_tested_point
        s = "「操る」の使い方として最も適切なものはどれか。"
        assert derive_tested_point(exam_point="用法", stem=s, options=[], answer_index=0) == "操る"

    def test_grammar_and_cloze_use_correct_option(self):
        from openclaw_adapter.quiz_db import derive_tested_point
        opts = ["だけあって", "がてら", "ばかりに", "とあって"]
        assert derive_tested_point(exam_point="文法形式の判断", stem="…（　　）…",
                                   options=opts, answer_index=2) == "ばかりに"
        opts2 = ["わざとらしい", "初々しい", "たどたどしい", "さりげない"]
        assert derive_tested_point(exam_point="文脈規定", stem="…（　　）…",
                                   options=opts2, answer_index=0) == "わざとらしい"

    def test_reading_and_kumitate_return_none(self):
        from openclaw_adapter.quiz_db import derive_tested_point
        assert derive_tested_point(exam_point="内容理解（短文）", stem="本文…",
                                   options=["a", "b"], answer_index=0) is None
        assert derive_tested_point(exam_point="文の組み立て", stem="並べ替え…",
                                   options=["a", "b"], answer_index=0) is None


class TestReadingTitleGuard:
    def test_detects_explicit_song_title_reading_prompt(self):
        assert reading_question_targets_source_title(
            exam_point="漢字読み",
            stem="次の曲名「ロストワンの号哭」にある〈号哭〉の読み方として最も適切なものはどれか。",
            source_excerpt_type="lyric",
        )

    def test_detects_title_only_grounding_even_without_marker(self):
        assert reading_question_targets_source_title(
            exam_point="漢字読み",
            stem="「〈号哭〉」の読み方として正しいものはどれか。",
            source_excerpt_type="title",
        )

    def test_allows_lexical_reading_grounded_by_real_lyric(self):
        assert not reading_question_targets_source_title(
            exam_point="漢字読み",
            stem="次の歌詞の「号哭」の読み方として正しいものはどれか。",
            source_excerpt_type="lyric",
        )

    def test_insert_rejects_song_title_reading_prompt(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="reading item targets source title"):
            db.insert_question(
                level="JLPT N1",
                exam_point="漢字読み",
                stem="次の曲名「ロストワンの号哭」にある〈号哭〉の読み方として最も適切なものはどれか。",
                options=("ごうこく", "ごうきゅう", "こうこく", "ごうなき"),
                answer_index=0,
                source_name="ロストワンの号哭",
                source_excerpt="ロストワンの号哭",
                source_excerpt_type="title",
                tested_point="号哭",
                author="codex",
            )


class TestLexicalCommentaryWrapperGuard:
    def test_detects_commentary_wrapped_lexical_stem(self):
        from openclaw_adapter.quiz_db import lexical_stem_uses_commentary_wrapper
        assert lexical_stem_uses_commentary_wrapper(
            exam_point="漢字読み",
            stem="次の一節『「ブリキノダンス」の解説によれば、...と筆者は読み解いている。』にある〈壮大〉の読み方として最も適切なものはどれか。",
        )
        assert lexical_stem_uses_commentary_wrapper(
            exam_point="漢字読み",
            stem="次の一節『「夜が明ける」というフレーズは、戦争が終盤を迎えていることや』にある〈終盤〉の読み方として最も適切なものはどれか。",
        )
        assert lexical_stem_uses_commentary_wrapper(
            exam_point="漢字読み",
            stem="次の一節『同日に公開したMVには、作詞作曲を担当したn-bunaが原案、監督、アニメーターとして初めて映像の制作に携わっています。』にある〈原案〉の読み方として最も適切なものはどれか。",
        )
        assert lexical_stem_uses_commentary_wrapper(
            exam_point="漢字読み",
            stem="歌詞『フロムトーキョー』では、MVで使用されているイラストが『シンデレラ』と近いテイストで描かれており、2つの歌詞の一人称が両方とも「あたし」であることから、にある〈一人称〉の読み方として最も適切なものはどれか。",
        )
        assert lexical_stem_uses_commentary_wrapper(
            exam_point="漢字読み",
            stem="歌詞「フロムトーキョー 歌詞 夏代孝明,渡辺拓也,cillia feat. 初音ミク ふりがな付。」にある〈渡辺拓也〉の読み方として最も適切なものはどれか。",
        )
        assert lexical_stem_uses_commentary_wrapper(
            exam_point="漢字読み",
            stem="次の一節『表現者としての才能はあっても『人を愛する才能』はありません。結果的に仲間を失ってしまい』にある〈表現者〉の読み方として最も適切なものはどれか。",
        )

    def test_allows_real_lyric_sentence(self):
        from openclaw_adapter.quiz_db import lexical_stem_uses_commentary_wrapper
        assert not lexical_stem_uses_commentary_wrapper(
            exam_point="言い換え類義",
            stem="次の一節「理想で作った道を現実が塗り替えていくよ」にある〈塗り替える〉に最も近い意味はどれか。",
        )

    def test_insert_rejects_commentary_wrapped_lexical_stem(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="lexical stem uses commentary wrapper"):
            db.insert_question(
                level="JLPT N1",
                exam_point="漢字読み",
                stem="次の一節『「ブリキノダンス」の解説によれば、...と筆者は読み解いている。』にある〈壮大〉の読み方として最も適切なものはどれか。",
                options=("そうだい", "そうたい", "そだい", "そうてい"),
                answer_index=0,
                source_name="ブリキノダンス",
                source_excerpt="「ブリキノダンス」の解説によれば、...と筆者は読み解いている。",
                source_excerpt_type="commentary",
                tested_point="壮大",
                author="codex",
                allow_ungrounded=True,
            )


class TestLowValueSynonymTargetGuard:
    def test_detects_colloquial_whole_utterance(self):
        from openclaw_adapter.quiz_db import synonym_target_is_low_value_fragment
        assert synonym_target_is_low_value_fragment(
            exam_point="言い換え類義",
            tested_point="どうだっていいや",
        )

    def test_detects_inflection_fragment(self):
        from openclaw_adapter.quiz_db import synonym_target_is_low_value_fragment
        assert synonym_target_is_low_value_fragment(
            exam_point="言い換え類義",
            tested_point="いがみ合って",
        )
        assert synonym_target_is_low_value_fragment(
            exam_point="言い換え類義",
            tested_point="占って",
        )

    def test_allows_fixed_nominal_phrase(self):
        from openclaw_adapter.quiz_db import synonym_target_is_low_value_fragment
        assert not synonym_target_is_low_value_fragment(
            exam_point="言い換え類義",
            tested_point="挙句の果て",
        )

    def test_insert_rejects_low_value_synonym_target(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="synonym target is a low-value fragment"):
            db.insert_question(
                level="JLPT N1",
                exam_point="言い換え類義",
                stem="次の一節「もうどうだっていいや」にある〈どうだっていいや〉の意味として最も近いものはどれか。",
                options=("何よりも大切だ", "もう関心が持てない", "細かく調べたい", "正しく直したい"),
                answer_index=1,
                source_name="ロストワンの号哭",
                source_excerpt="もうどうだっていいや",
                source_excerpt_type="lyric",
                tested_point="どうだっていいや",
                author="codex",
                allow_ungrounded=True,
            )


class TestLowValueReadingTargetGuard:
    def test_detects_inflected_reading_fragment(self):
        from openclaw_adapter.quiz_db import reading_target_is_low_value_fragment
        assert reading_target_is_low_value_fragment(
            exam_point="漢字読み",
            tested_point="募って",
        )
        assert reading_target_is_low_value_fragment(
            exam_point="漢字読み",
            tested_point="推し量らない",
        )
        assert reading_target_is_low_value_fragment(
            exam_point="漢字読み",
            tested_point="踠いたって",
        )

    def test_allows_clean_nominal_lexeme(self):
        from openclaw_adapter.quiz_db import reading_target_is_low_value_fragment
        assert not reading_target_is_low_value_fragment(
            exam_point="漢字読み",
            tested_point="切り抜け",
        )
        assert not reading_target_is_low_value_fragment(
            exam_point="漢字読み",
            tested_point="腑抜け",
        )

    def test_insert_rejects_low_value_reading_target(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="reading target is a low-value fragment"):
            db.insert_question(
                level="JLPT N1",
                exam_point="漢字読み",
                stem="「脅迫的に〈縛っちゃって〉」の〈縛っちゃって〉の読み方は？",
                options=("しばっちゃって", "しばちゃって", "ばくちゃって", "しばりちゃって"),
                answer_index=0,
                source_name="裏表ラバーズ",
                source_excerpt="脅迫的に縛っちゃって",
                source_excerpt_type="lyric",
                tested_point="縛っちゃって",
                author="codex",
                allow_ungrounded=True,
            )


class TestKanjiReadingDistractorAudit:
    def test_flags_zero_overlap_distractors(self):
        from openclaw_adapter.quiz_db import audit_kanji_reading_distractors
        # 【創造】そうぞう with けいばつ / やきめ — the systematic codex failure mode
        opts = ("だみんをおうかする", "やきめ", "そうぞう", "けいばつ")
        flagged = {o for _, o, _ in audit_kanji_reading_distractors(options=opts, answer_index=2)}
        assert "けいばつ" in flagged
        assert "やきめ" in flagged

    def test_flags_phrase_length_reading(self):
        from openclaw_adapter.quiz_db import audit_kanji_reading_distractors
        opts = ("そうぞう", "むせかえるようなにおい", "そうさく", "さくぞう")
        reasons = [r for _, _, r in audit_kanji_reading_distractors(options=opts, answer_index=0)]
        assert any("long" in r for r in reasons)

    def test_plausible_misreadings_pass(self):
        from openclaw_adapter.quiz_db import audit_kanji_reading_distractors
        # All distractors share sound with the answer — no obvious garbage
        good = ("そうぞう", "そうさく", "さくぞう", "そうきょう")
        assert audit_kanji_reading_distractors(options=good, answer_index=0) == []

    def test_katakana_folds_to_hiragana(self):
        from openclaw_adapter.quiz_db import _to_hiragana
        assert _to_hiragana("ソウゾウ") == "そうぞう"
        assert _to_hiragana("創造そうぞうabc123") == "そうぞう"

    def test_out_of_range_answer_index_is_safe(self):
        from openclaw_adapter.quiz_db import audit_kanji_reading_distractors
        assert audit_kanji_reading_distractors(options=("a", "b"), answer_index=9) == []

    def test_insert_rejects_implausible_reading_distractors(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="kanji reading distractors are implausible"):
            db.insert_question(
                level="JLPT N1",
                exam_point="漢字読み",
                stem="「創造」の読み方として正しいものはどれか。",
                options=("そうぞう", "けいばつ", "やきめ", "そうさく"),
                answer_index=0,
                source_name="sample",
                source_excerpt="創造",
                source_excerpt_type="lyric",
                tested_point="創造",
                author="codex",
                allow_ungrounded=True,
            )


class TestQuestionDedup:
    def test_identical_stem_same_point_is_duplicate(self):
        from openclaw_adapter.quiz_db import questions_are_near_duplicate
        assert questions_are_near_duplicate(
            a_stem="次の文の「創造」の読みとして正しいものはどれか。",
            b_stem="次の文の「創造」の読みとして正しいものはどれか。",
            a_tested_point="創造", b_tested_point="創造",
        )

    def test_different_point_and_stem_not_duplicate(self):
        from openclaw_adapter.quiz_db import questions_are_near_duplicate
        assert not questions_are_near_duplicate(
            a_stem="「創造」の読みは何か。",
            b_stem="まったく別の文脈規定の長い問題文でありこれは本当に違う内容だ。",
            a_tested_point="創造", b_tested_point="批判",
        )

    def test_similarity_bounds(self):
        from openclaw_adapter.quiz_db import question_similarity
        assert question_similarity("あいうえお", "あいうえお") == pytest.approx(1.0)
        assert question_similarity("", "x") == 0.0

    def test_find_duplicate_questions_db(self, tmp_path):
        db = _db(tmp_path)
        stem = "次の文の「矜持」の読みとして正しいものはどれか。"
        db.insert_question(
            level="JLPT N1", exam_point="漢字読み", stem=stem,
            options=("きょうじ", "きんじ", "けいじ", "きょうじゃく"),
            answer_index=0, source_type="vocaloid_song", source_name="テスト曲",
            source_excerpt="矜持を胸に進む", tested_point="矜持",
            verified=True, allow_ungrounded=True,
        )
        dups = db.find_duplicate_questions(
            stem=stem, tested_point="矜持", exam_point="漢字読み",
        )
        assert len(dups) == 1
        # An unrelated stem finds nothing
        assert db.find_duplicate_questions(
            stem="完全に無関係なべつの問題文である", tested_point="他",
            exam_point="漢字読み",
        ) == []


class TestSongCandidatePack:
    def _seed_song(self, db):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO quiz_songs (title, artist, youtube_url, youtube_short_url, "
                "lyrics_url, youtube_title_raw, video_id, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("テスト曲", "テスト歌手", "https://youtu.be/abc", "https://youtu.be/abc",
                 "https://example.com/l", "raw", "abc", "ready", "t", "t"),
            )
            song_id = conn.execute("SELECT id FROM quiz_songs WHERE video_id='abc'").fetchone()["id"]
            conn.execute(
                "INSERT INTO sentences (song_id, sentence_text, sentence_index, created_at) "
                "VALUES (?,?,?,?)", (song_id, "矜持を胸に進む夜", 0, "t"),
            )
            sid = conn.execute("SELECT id FROM sentences WHERE song_id=?", (song_id,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO vocabulary_tokens (song_id, sentence_id, surface, dictionary_form, "
                "reading, pos, jlpt_level, used_quiz_count, used_flashcard_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (song_id, sid, "矜持", "矜持", "きょうじ", "名詞", "N1", 0, 0, "t"),
            )
            conn.execute(
                "INSERT INTO vocabulary_tokens (song_id, sentence_id, surface, dictionary_form, "
                "reading, pos, jlpt_level, used_quiz_count, used_flashcard_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (song_id, sid, "胸", "胸", "むね", "名詞", "N3", 0, 0, "t"),
            )
        return song_id

    def test_build_pack_contains_only_unused_n1_tokens(self, tmp_path):
        db = _db(tmp_path)
        song_id = self._seed_song(db)
        pack = db.build_song_candidate_pack(song_id)
        assert pack["title"] == "テスト曲"
        assert pack["candidate_token_count"] == 1  # only the N1 token, not the N3 one
        cand = pack["sentences"][0]["candidates"][0]
        assert cand["surface"] == "矜持" and cand["reading"] == "きょうじ"
        assert pack["sentences"][0]["sentence_text"] == "矜持を胸に進む夜"

    def test_pack_is_cached_to_disk_and_loaded(self, tmp_path):
        db = _db(tmp_path)
        song_id = self._seed_song(db)
        db.build_song_candidate_pack(song_id)
        assert db._song_pack_path(song_id).exists()
        loaded = db.load_song_candidate_pack(song_id)
        assert loaded["song_id"] == song_id

    def test_missing_song_raises(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError):
            db.build_song_candidate_pack(999999)


class TestTestedJlptLevel:
    def test_question_stores_and_returns_tested_jlpt_level(self, tmp_path):
        db = _db(tmp_path)
        q = db.insert_question(
            level="JLPT N1",
            exam_point="漢字読み",
            stem="〈乙女〉の読みは？",
            options=("おとめ", "おつじょ", "いつめ", "おとな"),
            answer_index=0,
            explanation="〈乙女〉は「おとめ」。",
            source_type="vocaloid_song",
            source_name="テスト曲",
            source_excerpt="愛らしい乙女なんだが",
            tested_point="乙女",
            tested_jlpt_level="N2",
            author="codex",
            verified=True,
            allow_ungrounded=True,
        )
        assert q.tested_jlpt_level == "N2"
        assert q.level == "JLPT N1"  # stays in the N1 pool
        assert db.get_question(q.question_id).tested_jlpt_level == "N2"

    def test_tested_jlpt_level_defaults_null_treated_as_n1(self, tmp_path):
        db = _db(tmp_path)
        q = _insert_sample(db)
        assert q.tested_jlpt_level is None

    def test_vocab_card_inherits_tested_jlpt_level_from_primary(self, tmp_path):
        db = _db(tmp_path)
        db.upsert_vocab_seed("乙女", "おとめ", "少女")
        db.insert_question(
            level="JLPT N1",
            exam_point="漢字読み",
            stem="次の一節「愛らしい乙女なんだが」の〈乙女〉の読みは？",
            options=("おとめ", "おつじょ", "いつめ", "おとな"),
            answer_index=0,
            explanation="〈乙女〉は「おとめ」。",
            source_type="vocaloid_song",
            source_name="テスト曲",
            source_excerpt="愛らしい乙女なんだが",
            tested_point="乙女",
            tested_jlpt_level="N2",
            author="codex",
            verified=True,
            allow_ungrounded=True,
        )
        card = db.get_vocab_card(headword="乙女", level="JLPT N1")
        assert card is not None
        assert card.tested_jlpt_level == "N2"

    def test_migration_adds_columns_to_legacy_db(self, tmp_path):
        import sqlite3
        path = tmp_path / "legacy.sqlite3"
        # Minimal legacy tables WITHOUT the new column.
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE quiz_questions (
                question_id TEXT PRIMARY KEY, level TEXT NOT NULL,
                exam_point TEXT NOT NULL, stem TEXT NOT NULL,
                options_json TEXT NOT NULL DEFAULT '[]', answer_index INTEGER NOT NULL,
                explanation TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT 'other',
                source_name TEXT NOT NULL DEFAULT '', source_text_url TEXT,
                source_media_url TEXT, source_excerpt TEXT,
                source_excerpt_type TEXT NOT NULL DEFAULT 'other', tested_point TEXT,
                verified INTEGER NOT NULL DEFAULT 0, served_count INTEGER NOT NULL DEFAULT 0,
                author TEXT NOT NULL DEFAULT 'Claude',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE quiz_vocab_cards (
                vocab_id TEXT PRIMARY KEY, level TEXT NOT NULL, headword TEXT NOT NULL,
                reading_hiragana TEXT NOT NULL, zh_gloss_short TEXT NOT NULL,
                example_ja TEXT NOT NULL, example_source_kind TEXT NOT NULL DEFAULT 'adapted',
                source_name TEXT NOT NULL DEFAULT '', source_text_url TEXT,
                primary_question_id TEXT NOT NULL,
                support_question_ids_json TEXT NOT NULL DEFAULT '[]',
                exam_points_json TEXT NOT NULL DEFAULT '[]',
                author TEXT NOT NULL DEFAULT 'codex',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()
        # Opening via QuizDatabase must additively migrate without error.
        db = QuizDatabase(path)
        with db.connect() as conn:
            qcols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_questions)")}
            vcols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_vocab_cards)")}
        assert "tested_jlpt_level" in qcols
        assert "tested_jlpt_level" in vcols


class TestQuizSongsRenameAndFavorite:
    def test_fresh_db_has_quiz_songs_with_favorite(self, tmp_path):
        db = _db(tmp_path)
        with db.connect() as conn:
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_songs)")}
        assert "quiz_songs" in tables
        assert "favorite_songs" not in tables
        assert "favorite" in cols

    def test_upsert_defaults_favorite_and_is_sticky(self, tmp_path):
        db = _db(tmp_path)
        sid = db.upsert_favorite_song(
            title="t", artist="a", youtube_url="https://youtu.be/x",
            youtube_short_url="https://youtu.be/x", status="ready",
        )
        with db.connect() as conn:
            fav = conn.execute(
                "SELECT favorite FROM quiz_songs WHERE id = ?", (sid,)
            ).fetchone()["favorite"]
        assert fav == 1
        # A later quiz_source re-ingest must NOT un-favorite a hearted song.
        db.upsert_favorite_song(
            title="t", artist="a", youtube_url="https://youtu.be/x",
            youtube_short_url="https://youtu.be/x", status="ready",
            favorite=False,
        )
        with db.connect() as conn:
            fav2 = conn.execute(
                "SELECT favorite FROM quiz_songs WHERE id = ?", (sid,)
            ).fetchone()["favorite"]
        assert fav2 == 1

    def test_quiz_source_stored_as_not_favorite(self, tmp_path):
        db = _db(tmp_path)
        sid = db.upsert_favorite_song(
            title="t", artist="a", youtube_url="https://youtu.be/y",
            youtube_short_url="https://youtu.be/y", status="ready",
            favorite=False,
        )
        with db.connect() as conn:
            fav = conn.execute(
                "SELECT favorite FROM quiz_songs WHERE id = ?", (sid,)
            ).fetchone()["favorite"]
        assert fav == 0

    def test_legacy_favorite_songs_renamed_preserving_data(self, tmp_path):
        import sqlite3
        path = tmp_path / "legacy_songs.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE favorite_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '', youtube_url TEXT NOT NULL,
                youtube_short_url TEXT NOT NULL UNIQUE, lyrics_url TEXT,
                youtube_title_raw TEXT, video_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending', last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO favorite_songs
                (title, artist, youtube_url, youtube_short_url, status, created_at, updated_at)
                VALUES ('旧曲', '歌手', 'https://youtu.be/z', 'https://youtu.be/z', 'ready', 't', 't');
            """
        )
        conn.commit()
        conn.close()
        db = QuizDatabase(path)
        with db.connect() as conn:
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            row = conn.execute(
                "SELECT title, favorite FROM quiz_songs WHERE youtube_short_url='https://youtu.be/z'"
            ).fetchone()
        assert "quiz_songs" in tables
        assert "favorite_songs" not in tables
        assert row["title"] == "旧曲"
        # Pre-existing rows are genuine user favorites → default 1.
        assert row["favorite"] == 1


class TestVocabCardDifficultyBadge:
    def test_render_always_shows_difficulty_badge(self, tmp_path):
        from types import SimpleNamespace
        from openclaw_adapter.quiz_command import _render_vocab_card

        def _card(level_tag):
            return SimpleNamespace(
                level="JLPT N1", headword="乙女", reading_hiragana="おとめ",
                zh_gloss_short="少女", example_ja="愛らしい乙女なんだが",
                exam_points=("漢字読み",), source_name="テスト曲",
                source_media_url=None, source_text_url=None, vocab_id="v1",
                tested_jlpt_level=level_tag,
            )

        text_n2, _ = _render_vocab_card(_card("N2"), mode="all", index=0, total=3)
        assert "難度 N2" in text_n2.splitlines()[0]
        # NULL/blank tag is treated as N1 and still shows a badge.
        text_n1, _ = _render_vocab_card(_card(None), mode="all", index=0, total=3)
        assert "難度 N1" in text_n1.splitlines()[0]


class TestGrammarCards:
    def test_insert_grammar_question_backfills_grammar_card(self, tmp_path):
        db = _db(tmp_path)
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
            source_media_url="https://example.com/song",
            source_excerpt="逃げるまいと心に決めた。",
            tested_point="〜まい",
            tested_jlpt_level="N1",
            author="codex",
            verified=True,
            allow_ungrounded=True,
        )
        card = db.get_grammar_card(headword="〜まい", level="JLPT N1")
        assert card is not None
        assert card.primary_question_id == q.question_id
        assert card.example_ja == "彼は最後まで逃げるまいと踏みとどまった。"
        assert card.example_source_kind == "adapted"
        assert "否定意志" in card.explanation_zh
        assert card.exam_points == ("文章の文法",)

    def test_grammar_card_lookup_and_wrong_mode_use_tested_point_progress(self, tmp_path):
        db = _db(tmp_path)
        db.insert_question(
            level="JLPT N1",
            exam_point="文法形式の判断",
            stem="事情を知っている（　　）、黙っているわけにはいかない。",
            options=("以上", "ほど", "だけ", "まで"),
            answer_index=0,
            explanation="「〜以上」は前件を受けて当然そうなるという含みを持つ。",
            source_type="vocaloid_song",
            source_name="テスト曲",
            source_text_url="https://example.com/text",
            source_excerpt="事情を知っている以上、黙ってはいられない。",
            tested_point="〜以上",
            author="codex",
            verified=True,
            allow_ungrounded=True,
        )
        card = db.get_grammar_card(headword="〜以上", level="JLPT N1")
        assert card is not None
        db.record_attempt(
            question_id=card.primary_question_id,
            exam_point="文法形式の判断",
            tested_point="〜以上",
            level="JLPT N1",
            chat_id="u1",
            chosen_index=1,
            correct=False,
        )
        wrong_cards = db.list_grammar_cards(level="JLPT N1", chat_id="u1", mode="wrong")
        assert [c.headword for c in wrong_cards] == ["〜以上"]

    def test_grammar_card_summary_strips_answer_specific_noise(self, tmp_path):
        db = _db(tmp_path)
        db.insert_question(
            level="JLPT N1",
            exam_point="文の組み立て",
            stem="＿＿　＿＿　★＿＿　＿＿",
            options=("身動きも", "取れず", "眺めてる", "世界は不自由だなって"),
            answer_index=1,
            explanation=(
                "正しい並びは『身動きも→取れず→眺めてる→世界は不自由だなって』で、"
                "『身動きも取れず眺めてる世界は不自由だなって、ため息をつく。』となる。"
                "慣用句「身動きも取れない」の文語的な中止形「取れず」は「身動きも」の直後に来て"
                "『〜できないまま』を表す。"
                "「眺めてる」は連体修飾として名詞「世界」に掛かり、"
                "「世界は不自由だなって」が感想の引用として「ため息をつく」につながる。"
                "★印の空欄（＿＿②）に入るのは「取れず」。"
                "出典の対応箇所は「身動きも取れず眺めてる世界は不自由だなって」。"
                "【読み】身動（みうご）き・取（と）れず"
            ),
            source_type="vocaloid_song",
            source_name="反逆者の僕ら",
            source_text_url="https://example.com/text",
            source_excerpt="身動きも取れず眺めてる世界は不自由だなって",
            tested_point="文語否定「ず」の中止法",
            author="Claude",
            verified=True,
            allow_ungrounded=True,
        )
        card = db.get_grammar_card(headword="文語否定「ず」の中止法", level="JLPT N1")
        assert card is not None
        assert "正しい並び" not in card.explanation_zh
        assert "★印" not in card.explanation_zh
        assert "【読み】" not in card.explanation_zh
        assert "動詞連用形後" in card.explanation_zh
        assert len(card.explanation_zh) <= 120


def test_build_grammar_card_summary_prefers_prelearning_sentences():
    summary = build_grammar_card_summary(
        headword="文語否定「ず」の中止法",
        exam_point="文の組み立て",
        explanation=(
            "正しい並びは『身動きも→取れず→眺めてる→世界は不自由だなって』で、"
            "『身動きも取れず眺めてる世界は不自由だなって、ため息をつく。』となる。"
            "慣用句「身動きも取れない」の文語的な中止形「取れず」は「身動きも」の直後に来て"
            "『〜できないまま』を表す。"
            "★印の空欄（＿＿②）に入るのは「取れず」。"
            "【読み】身動（みうご）き・取（と）れず"
        ),
    )
    assert "正しい並び" not in summary
    assert "★印" not in summary
    assert "【読み】" not in summary
    assert "〜できないまま" in summary


def test_grammar_card_manual_override_wins_over_generated_summary(tmp_path):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    db.insert_question(
        level="JLPT N1",
        exam_point="文法形式の判断",
        stem="その全てを肯定し（　　）前に進めないかい。",
        options=("ないと", "まいと", "がてら", "かたがた"),
        answer_index=0,
        explanation=(
            "動詞ます形語幹『受け入れ』に接続し、かつ"
            "『受け入れなければ踏み出せない』という必要条件を表すのは「ないと」のみ。"
        ),
        source_type="vocaloid_song",
        source_name="ドラマツルギー",
        source_text_url="https://example.com/text",
        source_excerpt="その全てを肯定しないと前に進めないかい",
        tested_point="〜ないと（必要条件）",
        author="Claude",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(headword="〜ないと（必要条件）", level="JLPT N1")
    assert card is not None
    assert card.explanation_zh.startswith("表示「不……就不行」")
    assert "受け入れ" not in card.explanation_zh


def test_grammar_card_manual_override_for_bungo_zu_is_chinese(tmp_path):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    db.insert_question(
        level="JLPT N1",
        exam_point="文の組み立て",
        stem="身動きも（　　）眺めてる世界は不自由だなって。",
        options=("取れず", "取れて", "取ると", "取れば"),
        answer_index=0,
        explanation=(
            "正しい並びは『身動きも→取れず→眺めてる→世界は不自由だなって』で、"
            "慣用句「身動きも取れない」の文語的な中止形「取れず」は"
            "「身動きも」の直後に来て『〜できないまま』を表す。"
        ),
        source_type="vocaloid_song",
        source_name="反逆者の僕ら",
        source_text_url="https://example.com/text",
        source_excerpt="身動きも取れず眺めてる世界は不自由だなって",
        tested_point="文語否定「ず」の中止法",
        author="Claude",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(headword="文語否定「ず」の中止法", level="JLPT N1")
    assert card is not None
    assert "動詞連用形後" in card.explanation_zh
    assert "文語的な中止形" not in card.explanation_zh


def test_grammar_card_manual_override_can_replace_bad_source_example(tmp_path):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    db.insert_question(
        level="JLPT N1",
        exam_point="文法形式の判断",
        stem="重大な決断を前に、彼は誰に相談（　　）、たった一人で全てを決めてしまった。",
        options=("せずに", "するなり", "するそばから", "しようものなら"),
        answer_index=0,
        explanation=(
            "「〜ずに」は『〜しないで（次の動作をする）』の意で、"
            "『する』は不規則に『せずに』となる。"
        ),
        source_type="vocaloid_song",
        source_name="花を唄う",
        source_text_url="https://example.com/text",
        source_excerpt="どうしても 大人に成れずに",
        tested_point="〜ずに（〜しないで）",
        author="Claude",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(headword="〜ずに（〜しないで）", level="JLPT N1")
    assert card is not None
    assert card.example_ja == "彼は誰にも相談せずに、たった一人で全てを決めてしまった。"
    assert card.source_name == "改寫例句"
    assert card.source_text_url is None
    assert "ない形去掉「ない」" in card.explanation_zh
    assert "大人に成れずに" not in card.example_ja


def test_grammar_card_uses_filled_stem_as_adapted_example(tmp_path):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    db.insert_question(
        level="JLPT N1",
        exam_point="文法形式の判断",
        stem="明日の試合の結果（　　）、方針が変わるだろう。",
        options=("いかんで", "ながらに", "をよそに", "だに"),
        answer_index=0,
        explanation="「〜いかんで」は結果次第で後件が決まることを表す。",
        source_type="vocaloid_song",
        source_name="テスト曲",
        source_text_url="https://example.com/text",
        source_excerpt="どこを行けばどこに着くか？",
        tested_point="〜いかんで（〜がどうであるかによって）",
        author="Claude",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(
        headword="〜いかんで（〜がどうであるかによって）", level="JLPT N1"
    )
    assert card is not None
    assert card.example_ja == "明日の試合の結果いかんで、方針が変わるだろう。"
    assert card.example_source_kind == "adapted"
    assert card.source_name == "改寫例句"
    assert card.source_text_url is None
    assert card.explanation_zh.startswith("表示後項取決於前項")


def test_grammar_card_fills_all_blanks_in_adapted_example(tmp_path):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    db.insert_question(
        level="JLPT N1",
        exam_point="文法形式の判断",
        stem="幸福（　　）不幸（　　）、朝は誰にでもやって来る。",
        options=("であろうと", "につれて", "をおいて", "がてら"),
        answer_index=0,
        explanation="「〜であろうと」は条件に左右されない譲歩を表す。",
        source_type="vocaloid_song",
        source_name="ハロ／ハワユ",
        source_text_url="https://example.com/text",
        source_excerpt="幸せだろうと 不幸せだろうと 平等に残酷に 朝日は昇る",
        tested_point="〜であろうと",
        author="Claude",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(headword="〜であろうと", level="JLPT N1")
    assert card is not None
    assert card.example_ja == "幸福であろうと不幸であろうと、朝は誰にでもやって来る。"
    assert "（" not in card.example_ja
    assert card.source_name == "改寫例句"


def test_grammar_card_rejects_commentary_source_excerpt(tmp_path):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    db.insert_question(
        level="JLPT N1",
        exam_point="文の組み立て",
        stem="次の語句を並べ替えて、『＿＿①　＿＿②★　＿＿③　＿＿④』という文を完成させる。",
        options=("敵と見なしていることが", "主人公は", "読み取れる", "自分自身を恐れ、"),
        answer_index=3,
        explanation="目的語を含む並列述語の語順を問う。",
        source_type="vocaloid_song",
        source_name="ECHO",
        source_text_url="https://example.com/text",
        source_excerpt=(
            "「ECHO」の解説によれば、前半部分の矛盾や「見えない敵」の存在を合わせて考えると、"
            "主人公は自分自身を恐れ、敵と見なしていることが読み取れるという。"
            "二面性のある心がせめぎ合って主人公を苦しめているのである。"
        ),
        tested_point="目的語を含む並列述語の語順",
        author="codex",
        verified=True,
        allow_ungrounded=True,
    )
    card = db.get_grammar_card(headword="目的語を含む並列述語の語順", level="JLPT N1")
    assert card is None


def test_synonym_answer_restates_headword_flags_kana_restatement():
    # answer contains the headword reading in kana → restatement
    assert synonym_answer_restates_headword(
        headword="建前", reading="たてまえ", option="表向きの方針やたてまえ"
    )
    # inflected verb: reading minus okurigana still matches (わめく → わめ in わめき)
    assert synonym_answer_restates_headword(
        headword="喚く", reading="わめく", option="大声でわめき叫ぶ"
    )
    # cognate kana run (かたまり → かたま in かたまった)
    assert synonym_answer_restates_headword(
        headword="塊", reading="かたまり", option="一つにかたまったもの"
    )


def test_synonym_answer_restates_headword_flags_kanji_restatement():
    assert synonym_answer_restates_headword(
        headword="建前", reading="たてまえ", option="建前ばかりの方針"
    )


def test_synonym_answer_restates_headword_allows_clean_paraphrase():
    # a real paraphrase with different words is fine
    assert not synonym_answer_restates_headword(
        headword="建前", reading="たてまえ", option="表向きに示す名目や方針"
    )
    # incidental single shared kanji (移) in an otherwise-different paraphrase is OK
    assert not synonym_answer_restates_headword(
        headword="転移", reading="てんい", option="病巣などが他の場所へ移り広がること"
    )
    # short-reading word not present in the option
    assert not synonym_answer_restates_headword(
        headword="慈悲", reading="じひ", option="いつくしみ哀れむ思いやりの心"
    )
