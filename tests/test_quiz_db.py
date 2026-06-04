"""Unit tests for the independent quiz DB — no network, no Ollama."""
from __future__ import annotations

import sqlite3

import pytest

from openclaw_adapter.quiz_db import (
    QuizDatabase,
    build_question_id,
    format_authoring_knowledge_block,
    infer_source_excerpt_type,
    is_grounded,
    source_excerpt_type_conflicts_with_exam_point,
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
        assert {"quiz_questions", "quiz_authoring_knowledge"} <= names

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
        assert source_excerpt_type_conflicts_with_exam_point(
            exam_point="言い換え類義", source_excerpt_type="article"
        )
        assert source_excerpt_type_conflicts_with_exam_point(
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

    def test_insert_rejects_commentary_grounding_for_non_reading(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match="source_excerpt_type conflicts"):
            db.insert_question(
                level="JLPT N1",
                exam_point="言い換え類義",
                stem="次の一節「疾走感溢れる四つ打ちのタイトなリズム」にある〈疾走感〉の意味として最も近いものはどれか。",
                options=("速く走るような勢い", "遅い感じ", "静けさ", "懐かしさ"),
                answer_index=0,
                source_name="命に嫌われている",
                source_text_url="https://utaten.com/specialArticle/index/9999",
                source_excerpt="疾走感溢れる四つ打ちのタイトなリズム",
            )


class TestYouhouLeakGuard:
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
                stem="次のうち、語句「魅了する」の使い方として最も適切な文はどれか。",
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
