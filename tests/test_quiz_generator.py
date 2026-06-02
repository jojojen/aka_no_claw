"""Unit tests for QuizGenerator — fake SourceProvider + mocked LLM, no network."""
from __future__ import annotations

import json

from openclaw_adapter.quiz_db import QuizDatabase
from openclaw_adapter.quiz_generator import QuizGenerator
from openclaw_adapter.quiz_sources import QuizSource


class FakeProvider:
    """Source-agnostic provider — the generator must work via this interface
    alone, proving it never reaches for song-specific knowledge."""

    theme = "fake"

    def __init__(self, source_type="essay"):
        self._source_type = source_type
        self.calls = 0

    def fetch_candidates(self, limit=10):
        self.calls += 1
        return [
            QuizSource(
                source_type=self._source_type,
                name="テスト素材",
                text_url="https://example.com/text",
                media_url=None,
                excerpt="これはテスト用の本文です。",
            )
        ]


def _author_payload(answer_index=2):
    # Stem is a cloze on the FakeProvider excerpt ("これはテスト用の本文です。") so it
    # passes the source-grounding gate — the gate is exercised end-to-end here.
    return json.dumps(
        {
            "exam_point": "文法",
            "stem": "これはテスト用の___です。",
            "options": ["A案", "B案", "C案", "D案"],
            "answer_index": answer_index,
            "explanation": "C が正しい。",
        }
    )


def _grader_payload(answer_index):
    return json.dumps({"answer_index": answer_index, "reason": "ok"})


def _make_gen(tmp_path, json_call_fn):
    db = QuizDatabase(tmp_path / "quiz.sqlite3")
    gen = QuizGenerator(
        db=db,
        endpoint="http://127.0.0.1:11434",
        model="qwen3:14b",
        json_call_fn=json_call_fn,
        max_retries=3,
    )
    return db, gen


class TestDualVerification:
    def test_grader_agreement_inserts_verified(self, tmp_path):
        # author says 2, grader independently says 2 → accepted.
        replies = iter([_author_payload(2), _grader_payload(2)])
        db, gen = _make_gen(tmp_path, lambda **kw: next(replies))
        q = gen.generate_one_question(level="JLPT N1", theme="fake", provider=FakeProvider())
        assert q is not None
        assert q.verified is True
        assert q.answer_index == 2
        assert db.count_verified(level="JLPT N1") == 1

    def test_grader_disagreement_discards_and_retries(self, tmp_path):
        # Every attempt: author=2 but grader=0 → always rejected, nothing stored.
        def call(**kw):
            prompt = kw.get("prompt", "")
            # The grader prompt is the one that asks the solver to answer.
            if "解題者" in prompt or "獨立作答" in prompt:
                return _grader_payload(0)
            return _author_payload(2)

        db, gen = _make_gen(tmp_path, call)
        q = gen.generate_one_question(level="JLPT N1", theme="fake", provider=FakeProvider())
        assert q is None
        assert db.count_verified(level="JLPT N1") == 0

    def test_eventual_agreement_within_retry_budget(self, tmp_path):
        # First round grader disagrees; second round agrees.
        seq = iter(
            [
                _author_payload(2),
                _grader_payload(0),  # reject
                _author_payload(1),
                _grader_payload(1),  # accept
            ]
        )
        db, gen = _make_gen(tmp_path, lambda **kw: next(seq))
        q = gen.generate_one_question(level="JLPT N1", theme="fake", provider=FakeProvider())
        assert q is not None and q.answer_index == 1


class TestAuthoringKnowledgeInjection:
    def test_retrieved_rule_is_injected_into_author_prompt(self, tmp_path):
        seen_prompts = []

        def call(**kw):
            prompt = kw.get("prompt", "")
            seen_prompts.append(prompt)
            if "解題者" in prompt or "獨立作答" in prompt:
                return _grader_payload(2)
            return _author_payload(2)

        db, gen = _make_gen(tmp_path, call)
        db.upsert_authoring_knowledge(
            category="distractor_design",
            title="干擾項同詞性",
            technique="干擾選項要與正解同詞性。",
            keywords=("fake", "テスト素材"),
            origin="seed",
            confidence=0.9,
        )
        gen.generate_one_question(level="JLPT N1", theme="fake", provider=FakeProvider())
        author_prompts = [p for p in seen_prompts if "出題老師" in p]
        assert author_prompts, "author prompt was never issued"
        assert "干擾項同詞性" in author_prompts[0]

    def test_applied_count_increments(self, tmp_path):
        seq = iter([_author_payload(2), _grader_payload(2)])
        db, gen = _make_gen(tmp_path, lambda **kw: next(seq))
        entry = db.upsert_authoring_knowledge(
            category="grammar",
            title="規則A",
            technique="…",
            keywords=("fake", "テスト素材"),
            origin="seed",
            confidence=0.9,
        )
        gen.generate_one_question(level="JLPT N1", theme="fake", provider=FakeProvider())
        refreshed = db.all_authoring_knowledge()[0]
        assert refreshed.knowledge_id == entry.knowledge_id
        assert refreshed.times_applied >= 1


class TestSourceAgnostic:
    def test_works_for_non_song_source_type(self, tmp_path):
        # Swapping the provider to an essay source needs zero generator changes;
        # the stored question simply carries source_type="essay".
        seq = iter([_author_payload(0), _grader_payload(0)])
        db, gen = _make_gen(tmp_path, lambda **kw: next(seq))
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake", provider=FakeProvider(source_type="essay")
        )
        assert q is not None and q.source_type == "essay"
