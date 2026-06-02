"""Unit tests for the independent quiz DB — no network, no Ollama."""
from __future__ import annotations

import pytest

from openclaw_adapter.quiz_db import (
    QuizDatabase,
    build_question_id,
    format_authoring_knowledge_block,
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
