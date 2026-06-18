from __future__ import annotations

from openclaw_adapter import market_title_corpus as mtc


def test_record_titles_dedupes_and_counts_new(tmp_path) -> None:
    db = tmp_path / "corpus.sqlite3"
    added = mtc.record_titles(["ポケモンカード 黒炎の支配者", "BOX 未開封"], source="research", path=db)
    assert added == 2
    # Re-recording an existing title plus one new one → only the new one counts.
    added2 = mtc.record_titles(["ポケモンカード 黒炎の支配者", "シュリンク付き"], source="opportunity", path=db)
    assert added2 == 1
    assert mtc.corpus_size(db) == 3
    assert set(mtc.iter_titles(db)) == {
        "ポケモンカード 黒炎の支配者",
        "BOX 未開封",
        "シュリンク付き",
    }


def test_record_titles_skips_blank_and_whitespace(tmp_path) -> None:
    db = tmp_path / "corpus.sqlite3"
    added = mtc.record_titles(["", "   ", None, "本物"], source="research", path=db)  # type: ignore[list-item]
    assert added == 1
    assert mtc.iter_titles(db) == ["本物"]


def test_corpus_size_and_iter_absent_store_are_empty(tmp_path) -> None:
    db = tmp_path / "missing.sqlite3"
    assert mtc.corpus_size(db) == 0
    assert mtc.iter_titles(db) == []


def test_record_titles_is_fail_safe(monkeypatch, tmp_path) -> None:
    # A storage failure must never propagate into the /research or /opportunity run.
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mtc, "_connect", boom)
    assert mtc.record_titles(["x"], source="research", path=tmp_path / "c.sqlite3") == 0
