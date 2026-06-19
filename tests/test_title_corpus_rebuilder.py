from __future__ import annotations

from openclaw_adapter import market_title_corpus as mtc
from openclaw_adapter import research_command as rc
from openclaw_adapter import title_corpus_rebuilder as tcr


_CANARY_SINGLES = (
    "ピカチュウ", "リザードン ex", "ミュウ", "イーブイ", "ナンジャモ sar", "単品",
    "プロモ", "ピカチュウ ar", "リザードン sar", "ミライドン", "コライドン",
    "パオジアン ex", "セグレイブ", "サーフゴー ex", "まとめ売り", "美品", "傷あり",
    "ペリペリ付き", "未使用", "コレクション", "おまけ付き", "値下げ", "即購入可",
)


def _canary_passing_titles() -> list[str]:
    titles = [f"ポケモンカード 黒炎の支配者 {s}" for s in _CANARY_SINGLES]
    titles += [
        f"ポケモンカード {s}"
        for s in ("151 リザードン", "クレイバースト", "スノーハザード", "vstar ユニバース")
    ]
    titles += ["ポケモンカード 黒炎の支配者 box シュリンク付き 未開封"]
    titles += ["ポケモンカード 黒炎の支配者 box 未開封"]
    return titles


def test_rebuild_writes_table_and_reports_thin(tmp_path) -> None:
    corpus = tmp_path / "corpus.sqlite3"
    out = tmp_path / "df.json"
    mtc.record_titles(_canary_passing_titles(), source="research", path=corpus)

    report = tcr.rebuild_title_df(corpus_path=corpus, out_path=out)

    assert out.exists()  # table is always written; the gate decides at read time
    assert report.corpus_titles == report.total_docs > 0
    assert report.token_vocab > 0
    # Default threshold is 3000 → a tiny corpus must NOT activate.
    assert report.activated is False
    assert report.reason == "too_thin"


def test_rebuild_activates_when_thick_and_canary_passes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rc, "_MIN_TITLE_CORPUS_DOCS", 5)
    corpus = tmp_path / "corpus.sqlite3"
    out = tmp_path / "df.json"
    mtc.record_titles(_canary_passing_titles(), source="opportunity", path=corpus)

    report = tcr.rebuild_title_df(corpus_path=corpus, out_path=out)

    assert report.activated is True
    assert report.reason == "activated"
    assert report.canary_pass is True


def test_rebuild_empty_corpus_reports_no_table(tmp_path) -> None:
    report = tcr.rebuild_title_df(
        corpus_path=tmp_path / "absent.sqlite3", out_path=tmp_path / "df.json"
    )
    assert report.total_docs == 0
    assert report.activated is False
    assert report.reason == "no_table"


def test_run_once_notifies_with_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rc, "_MIN_TITLE_CORPUS_DOCS", 5)
    corpus = tmp_path / "corpus.sqlite3"
    mtc.record_titles(_canary_passing_titles(), source="research", path=corpus)
    sent: list[str] = []
    rebuilder = tcr.TitleCorpusRebuilder(
        notify_fn=sent.append, corpus_path=corpus, out_path=tmp_path / "df.json"
    )

    report = rebuilder.run_once()

    assert report is not None and report.activated is True
    assert len(sent) == 1
    assert "已啟用" in sent[0]


def test_run_once_is_fail_safe_when_notify_raises(tmp_path) -> None:
    corpus = tmp_path / "corpus.sqlite3"
    mtc.record_titles(["ポケモンカード 単品"], source="research", path=corpus)

    def boom(_text: str) -> None:
        raise RuntimeError("telegram down")

    rebuilder = tcr.TitleCorpusRebuilder(
        notify_fn=boom, corpus_path=corpus, out_path=tmp_path / "df.json"
    )
    # A notify failure must not propagate out of the weekly job.
    report = rebuilder.run_once()
    assert report is not None


def test_run_once_skips_notify_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rc, "_MIN_TITLE_CORPUS_DOCS", 5)
    corpus = tmp_path / "corpus.sqlite3"
    mtc.record_titles(_canary_passing_titles(), source="research", path=corpus)
    sent: list[str] = []
    rebuilder = tcr.TitleCorpusRebuilder(
        notify_fn=sent.append,
        corpus_path=corpus,
        out_path=tmp_path / "df.json",
        notify_enabled=False,
    )

    report = rebuilder.run_once()

    assert report is not None and report.activated is True
    assert sent == []


def test_format_rebuild_notice_branches() -> None:
    base = dict(corpus_titles=10, total_docs=10, token_vocab=5, bigram_vocab=3, min_docs=3000)
    activated = tcr.format_rebuild_notice(
        tcr.RebuildReport(activated=True, reason="activated", canary_pass=True, **base)
    )
    thin = tcr.format_rebuild_notice(
        tcr.RebuildReport(activated=False, reason="too_thin", canary_pass=True, **base)
    )
    canary = tcr.format_rebuild_notice(
        tcr.RebuildReport(activated=False, reason="canary_failed", canary_pass=False, **base)
    )
    assert "已啟用" in activated
    assert "養厚" in thin
    assert "金絲雀" in canary
