"""Unit tests for 音檔/PV extraction, exact-name resolution and DB backfill — all
offline (pure selectors + fake resolver), no VocaDB calls."""
from __future__ import annotations

from openclaw_adapter.miku_ranking import (
    _extract_pv_url,
    _select_song_pv,
    backfill_song_media,
)
from openclaw_adapter.quiz_db import QuizDatabase
from openclaw_adapter.quiz_generator import QuizDailyScheduler


class TestExtractPVUrl:
    def test_prefers_youtube_over_niconico(self):
        pvs = [
            {"service": "NicoNicoDouga", "pvType": "Original", "url": "nico"},
            {"service": "Youtube", "pvType": "Original", "url": "yt"},
        ]
        assert _extract_pv_url(pvs) == "yt"

    def test_falls_back_to_niconico_when_no_youtube(self):
        pvs = [{"service": "NicoNicoDouga", "pvType": "Original", "url": "nico"}]
        assert _extract_pv_url(pvs) == "nico"

    def test_prefers_original_within_service(self):
        pvs = [
            {"service": "Youtube", "pvType": "Reprint", "url": "yt-reprint"},
            {"service": "Youtube", "pvType": "Original", "url": "yt-original"},
        ]
        assert _extract_pv_url(pvs) == "yt-original"

    def test_none_when_no_pv_has_url(self):
        assert _extract_pv_url([{"service": "Youtube"}]) is None
        assert _extract_pv_url([]) is None


class TestSelectSongPV:
    def test_requires_exact_name_not_substring(self):
        # Guards the real bug: 『ロキ』 must NOT match 『ロキソプロフェン…』.
        items = [
            {"name": "ロキソプロフェンNaテープ", "pvs": [{"service": "Youtube", "url": "wrong"}]},
            {"name": "ロキ", "pvs": [{"service": "Youtube", "pvType": "Original", "url": "right"}]},
        ]
        assert _select_song_pv(items, "ロキ") == "right"

    def test_matches_via_alias(self):
        items = [{"name": "Roki", "names": [{"value": "ロキ"}],
                  "pvs": [{"service": "Youtube", "url": "u"}]}]
        assert _select_song_pv(items, "ロキ") == "u"

    def test_skips_exact_match_without_pv(self):
        items = [
            {"name": "ロキ", "pvs": []},
            {"name": "ロキ", "pvs": [{"service": "Youtube", "url": "second"}]},
        ]
        assert _select_song_pv(items, "ロキ") == "second"

    def test_no_exact_match_returns_none(self):
        items = [{"name": "別の曲", "pvs": [{"service": "Youtube", "url": "x"}]}]
        assert _select_song_pv(items, "ロキ") is None

    def test_normalizes_sharp_and_space_variants(self):
        # Our DB stores 『心拍数 #0822』 (# + space); VocaDB stores 『心拍数♯0822』 (♯).
        # Auto-heal must bridge that without a manual fix.
        items = [{"name": "心拍数♯0822",
                  "pvs": [{"service": "Youtube", "url": "ok"}]}]
        assert _select_song_pv(items, "心拍数 #0822") == "ok"


def _insert_song(db, name, *, media=None, source_type="vocaloid_song"):
    return db.insert_question(
        level="JLPT N1",
        exam_point="内容理解",
        stem=f"{name}に関する内容理解問題の本文。",
        options=("選択肢A", "選択肢B", "選択肢C", "選択肢D"),
        answer_index=0,
        source_type=source_type,
        source_name=name,
        source_media_url=media,
        source_excerpt="ダミー本文",
        allow_ungrounded=True,
    )


class TestBackfillSongMedia:
    def test_fills_missing_and_skips_present_and_non_song(self, tmp_path):
        db = QuizDatabase(tmp_path / "q.sqlite3")
        a = _insert_song(db, "ロキ")
        _insert_song(db, "千本桜", media="https://youtu.be/have")
        _insert_song(db, "エッセイ", source_type="essay")  # non-song → ignored

        resolved = {"ロキ": "https://youtu.be/roki"}
        filled, missing = backfill_song_media(db, resolver=lambda n: resolved.get(n))

        assert (filled, missing) == (1, 1)  # only ロキ was missing & resolvable
        assert db.get_question(a.question_id).source_media_url == "https://youtu.be/roki"

    def test_unresolvable_name_left_untouched(self, tmp_path):
        db = QuizDatabase(tmp_path / "q.sqlite3")
        a = _insert_song(db, "無名曲")
        filled, missing = backfill_song_media(db, resolver=lambda n: None)
        assert (filled, missing) == (0, 1)
        assert db.get_question(a.question_id).source_media_url is None

    def test_resolver_called_once_per_unique_name(self, tmp_path):
        db = QuizDatabase(tmp_path / "q.sqlite3")
        # Two questions on the SAME song must cost only one lookup.
        _insert_song(db, "ロキ")
        db.insert_question(
            level="JLPT N1", exam_point="主張", stem="ロキに関する主張問題の本文。",
            options=("A", "B", "C", "D"), answer_index=0,
            source_type="vocaloid_song", source_name="ロキ",
            source_media_url=None, source_excerpt="別の本文", allow_ungrounded=True,
        )
        calls = []

        def resolver(name):
            calls.append(name)
            return "https://youtu.be/roki"

        filled, missing = backfill_song_media(db, resolver=resolver)
        assert (filled, missing) == (2, 2)
        assert calls == ["ロキ"]  # cached: one lookup, two rows filled


class TestSchedulerBackfillHook:
    def test_backfill_runs_after_batch(self):
        class _FakeGen:
            def generate_one_question(self, **kw):
                return None

        ran = []
        scheduler = QuizDailyScheduler(
            generator=_FakeGen(), per_day=1, media_backfill_fn=lambda: ran.append(1),
        )
        scheduler.generate_batch()
        assert ran == [1]

    def test_no_hook_is_safe(self):
        class _FakeGen:
            def generate_one_question(self, **kw):
                return None

        scheduler = QuizDailyScheduler(generator=_FakeGen(), per_day=1)
        scheduler.generate_batch()  # must not raise
