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

    def test_question_type_drives_knowledge_retrieval(self, tmp_path):
        # Regression: the retrieval query must include the question_type, so a
        # type-specific rule (e.g. the 内容理解 "don't copy the answer verbatim"
        # lesson) is injected even when its keywords don't match the song name.
        # Crowd the KB with higher-confidence generic rules so the type rule
        # only makes the top-k cut when the question_type token boosts it.
        seen_prompts = []

        def call(**kw):
            prompt = kw.get("prompt", "")
            seen_prompts.append(prompt)
            if "解題者" in prompt or "獨立作答" in prompt:
                return _grader_payload(2)
            return _author_payload(2)

        db, gen = _make_gen(tmp_path, call)
        for i in range(6):
            db.upsert_authoring_knowledge(
                category="level_calibration",
                title=f"無關規則{i}",
                technique="一般規則。",
                keywords=("無關",),
                origin="seed",
                confidence=0.99,
            )
        db.upsert_authoring_knowledge(
            category="reading",
            title="内容理解は逐字コピー禁止",
            technique="内容理解の正解を本文から逐字コピーしてはいけない。",
            keywords=("内容理解", "逐字コピー"),
            origin="seed",
            confidence=0.5,  # lower than the generic rules
        )
        gen.generate_one_question(
            level="JLPT N1", theme="fake", provider=FakeProvider(), question_type="内容理解"
        )
        author_prompts = [p for p in seen_prompts if "出題老師" in p]
        assert author_prompts, "author prompt was never issued"
        assert "内容理解は逐字コピー禁止" in author_prompts[0]

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


class ReadingProvider:
    """Provider whose excerpt is multi-sentence 本文 (essay/評論), so reading-type
    discrimination guards can be exercised end-to-end."""

    theme = "fake"

    def __init__(self, excerpt):
        self._excerpt = excerpt

    def fetch_candidates(self, limit=10):
        return [
            QuizSource(
                source_type="essay",
                name="テスト評論",
                text_url="https://example.com/text",
                media_url=None,
                excerpt=self._excerpt,
            )
        ]


def _reading_author_payload(*, exam_point, stem, options, answer_index, explanation="解説"):
    return json.dumps(
        {
            "exam_point": exam_point,
            "stem": stem,
            "options": options,
            "answer_index": answer_index,
            "explanation": explanation,
        }
    )


def _is_leak_probe(prompt):
    return "看不到本文" in prompt


def _is_grader(prompt):
    return ("解題者" in prompt or "獨立作答" in prompt) and not _is_leak_probe(prompt)


class TestReadingDiscriminationGuards:
    EXCERPT_VERBATIM = "猫は窓辺で静かに眠っていた。外では冷たい雨が降り続いていた。"

    def test_verbatim_copy_correct_option_is_rejected(self, tmp_path):
        # 内容理解 where the correct option is a 本文 sentence copied verbatim. The
        # correctness grader agrees (it can see 本文), but the question is just
        # 'spot the copied line' → the verbatim guard must discard it.
        author = _reading_author_payload(
            exam_point="内容理解",
            stem="本文の内容に合うものはどれか。",
            options=[
                "猫は窓辺で静かに眠っていた",
                "犬が広い庭を駆け回っていた",
                "小鳥が高い空を飛んでいた",
                "魚が清い川を泳いでいた",
            ],
            answer_index=0,
        )

        def call(**kw):
            prompt = kw.get("prompt", "")
            if _is_leak_probe(prompt):
                return _grader_payload(-1)
            if _is_grader(prompt):
                return _grader_payload(0)  # grader agrees with author
            return author

        db, gen = _make_gen(tmp_path, call)
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake",
            provider=ReadingProvider(self.EXCERPT_VERBATIM), question_type="内容理解",
        )
        assert q is None
        assert db.count_verified(level="JLPT N1") == 0

    def test_stem_leak_rejected_by_inverted_grader(self, tmp_path):
        # The answer is leaked into the stem itself (not a verbatim 本文 copy, so the
        # copy guard passes). The inverted grader, denied 本文, still lands on it →
        # the question needs no 本文 → reject.
        author = _reading_author_payload(
            exam_point="内容理解",
            stem="本文によれば、主人公が最後に選んだのは『海』だった。主人公が最後に選んだものはどれか。",
            options=["海", "山", "空", "森"],
            answer_index=0,
        )

        def call(**kw):
            prompt = kw.get("prompt", "")
            if _is_leak_probe(prompt):
                return _grader_payload(0)  # determinable WITHOUT 本文 → leak
            if _is_grader(prompt):
                return _grader_payload(0)
            return author

        db, gen = _make_gen(tmp_path, call)
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake",
            provider=ReadingProvider("主人公は長い旅の末、ある決断を下した。"),
            question_type="内容理解",
        )
        assert q is None
        assert db.count_verified(level="JLPT N1") == 0

    def test_clean_reading_question_passes_both_guards(self, tmp_path):
        # Paraphrased correct option (not verbatim) + answer needs 本文 → both guards
        # pass and the question is stored.
        excerpt = (
            "筆者は、技術の進歩が必ずしも幸福をもたらすとは限らないと述べている。"
            "便利さの裏で人間関係が希薄になることを懸念している。"
        )
        author = _reading_author_payload(
            exam_point="主張",
            stem="本文における筆者の主張に最も近いものはどれか。",
            options=[
                "技術の進歩は人間関係を損なう恐れがある",
                "技術の進歩は常に幸福をもたらす",
                "技術の進歩は不要である",
                "技術の進歩は人間関係を必ず深める",
            ],
            answer_index=0,
        )

        def call(**kw):
            prompt = kw.get("prompt", "")
            if _is_leak_probe(prompt):
                return _grader_payload(-1)  # cannot determine without 本文 → clean
            if _is_grader(prompt):
                return _grader_payload(0)
            return author

        db, gen = _make_gen(tmp_path, call)
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake",
            provider=ReadingProvider(excerpt), question_type="主張",
        )
        assert q is not None
        assert q.verified is True
        assert q.answer_index == 0
        assert db.count_verified(level="JLPT N1") == 1


class TestVerbatimCopyHelper:
    def test_detects_verbatim_lift(self):
        from openclaw_adapter.quiz_db import correct_option_is_verbatim_copy

        excerpt = "猫は窓辺で静かに眠っていた。外では雨が降り続いていた。"
        assert correct_option_is_verbatim_copy(
            options=("猫は窓辺で静かに眠っていた", "犬が庭を走っていた", "鳥が飛んでいた", "魚が泳いでいた"),
            answer_index=0,
            source_excerpt=excerpt,
        )

    def test_paraphrase_is_not_flagged(self):
        from openclaw_adapter.quiz_db import correct_option_is_verbatim_copy

        excerpt = "筆者は技術の進歩が幸福をもたらすとは限らないと述べている。"
        assert not correct_option_is_verbatim_copy(
            options=("技術の進歩は必ずしも幸福に繋がらない", "技術は常に幸福を生む", "技術は無意味だ", "技術は害だ"),
            answer_index=0,
            source_excerpt=excerpt,
        )

    def test_short_options_never_flagged(self):
        from openclaw_adapter.quiz_db import correct_option_is_verbatim_copy

        assert not correct_option_is_verbatim_copy(
            options=("海", "山", "空", "森"),
            answer_index=0,
            source_excerpt="主人公は海を選んだ。",
        )


class TestStructuralGuards:
    """Universal (all-type) guards that fire BEFORE the grader, so a degenerate
    item never even costs a grader call."""

    def test_duplicate_options_rejected(self, tmp_path):
        author = _reading_author_payload(
            exam_point="文の組み立て",
            stem="＿＿　＿＿　＿★＿　＿＿。",
            options=["同じ片", "同じ片", "別片い", "別片ろ"],  # two identical → no discrimination
            answer_index=0,
        )
        db, gen = _make_gen(tmp_path, lambda **kw: author)
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake",
            provider=ReadingProvider("同じ片別片い別片ろ"), question_type="文の組み立て",
        )
        assert q is None
        assert db.count_verified(level="JLPT N1") == 0

    def test_answer_leaked_in_stem_rejected(self, tmp_path):
        author = _reading_author_payload(
            exam_point="文脈規定",
            stem="正解はこれは重要な情報だから選べ。",  # correct option sits in the stem
            options=["これは重要な情報", "無関係な選択ろ", "無関係な選択は", "無関係な選択に"],
            answer_index=0,
        )
        db, gen = _make_gen(tmp_path, lambda **kw: author)
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake",
            provider=ReadingProvider("これは重要な情報を伝える本文。"), question_type="文脈規定",
        )
        assert q is None
        assert db.count_verified(level="JLPT N1") == 0

    def test_clean_kumitate_passes(self, tmp_path):
        line = "君のことをいつまでも忘れないと誓う"
        author = _reading_author_payload(
            exam_point="文の組み立て",
            stem="＿＿　＿＿　＿★＿　＿＿。",
            options=["と誓う", "君のことを", "忘れない", "いつまでも"],
            answer_index=2,
        )

        def call(**kw):
            prompt = kw.get("prompt", "")
            # Detect the author prompt first: _KUMITATE_RULES itself contains the
            # word 解題者, which would otherwise fool a grader-keyword check.
            if "出題老師" in prompt:
                return author
            return _grader_payload(2)

        db, gen = _make_gen(tmp_path, call)
        q = gen.generate_one_question(
            level="JLPT N1", theme="fake",
            provider=ReadingProvider(line), question_type="文の組み立て",
        )
        assert q is not None and q.answer_index == 2
        assert q.exam_point == "文の組み立て"


class TestVocabSubtypeSteer:
    def test_subtype_pins_vocab_rules_and_steer(self):
        from openclaw_adapter.quiz_generator import _select_type_block, _VOCAB_RULES

        block = _select_type_block("漢字読み")
        assert _VOCAB_RULES in block            # full 単語 ruleset is used
        assert "固定只出「漢字読み」" in block      # plus the pin to that subtype

    def test_plain_tango_has_no_pin(self):
        from openclaw_adapter.quiz_generator import _select_type_block, _VOCAB_RULES

        assert _select_type_block("単語") == _VOCAB_RULES


class TestStructuralHelpers:
    def test_options_have_duplicates(self):
        from openclaw_adapter.quiz_db import options_have_duplicates

        assert options_have_duplicates(("A", "A", "B", "C"))
        assert options_have_duplicates(("海。", "海", "山", "空"))  # noise-stripped equal
        assert not options_have_duplicates(("海", "山", "空", "森"))

    def test_answer_leaks_into_stem(self):
        from openclaw_adapter.quiz_db import answer_leaks_into_stem

        assert answer_leaks_into_stem(
            stem="主人公が選んだのは海辺の町だった。",
            options=("海辺の町", "山間の村", "都会の街", "森の小屋"),
            answer_index=0,
        )
        assert not answer_leaks_into_stem(
            stem="主人公が最後に選んだものはどれか。",
            options=("海辺の町", "山間の村", "都会の街", "森の小屋"),
            answer_index=0,
        )
        # too-short correct option never trips the guard (incidental kana)
        assert not answer_leaks_into_stem(
            stem="海を見た。", options=("海", "山", "空", "森"), answer_index=0
        )


class TestKumitateGrounding:
    def test_real_fragments_are_grounded(self):
        from openclaw_adapter.quiz_db import is_grounded

        assert is_grounded(
            exam_point="文の組み立て",
            stem="＿＿　＿＿　＿★＿　＿＿。",
            options=("と誓う", "君のことを", "忘れない", "いつまでも"),
            answer_index=2,
            source_excerpt="君のことを いつまでも 忘れないと誓う、夜空の下で。",
        )

    def test_fabricated_fragment_not_grounded(self):
        from openclaw_adapter.quiz_db import is_grounded

        assert not is_grounded(
            exam_point="文の組み立て",
            stem="＿＿　＿＿　＿★＿　＿＿。",
            options=("と誓う", "君のことを", "捏造された詞", "いつまでも"),
            answer_index=0,
            source_excerpt="君のことを いつまでも 忘れないと誓う、夜空の下で。",
        )


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
