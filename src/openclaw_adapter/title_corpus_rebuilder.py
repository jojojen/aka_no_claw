"""Weekly rebuild of the comp-filter IDF table from the passive title corpus.

The bot (龍蝦) starts this daemon at launch. Once a week it re-distills every
title the corpus sink has passively harvested from /research and /opportunity
into ``data/market_title_df.json``, then evaluates the activation gate
(``research_command.describe_title_idf_activation``: enough docs **and** the
behavioural canary). It writes the table unconditionally — the gate is enforced
at *read* time on every /research, so a thin or off-domain table simply stays on
cold-start (plain Jaccard / PR1). Each run posts a Telegram notice saying it ran,
how big the corpus is, and whether the IDF weighting is now active.

Rebuilding reads only titles already cached locally — **zero** new external
queries (Rule C7).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .market_title_corpus import _CORPUS_PATH, corpus_size, iter_titles
from .research_command import (
    _TITLE_DF_PATH,
    build_title_df_from_titles,
    describe_title_idf_activation,
    load_title_idf_stats,
)

logger = logging.getLogger(__name__)

_WEEKLY_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class RebuildReport:
    corpus_titles: int
    total_docs: int
    token_vocab: int
    bigram_vocab: int
    activated: bool
    reason: str
    min_docs: int
    canary_pass: bool


def rebuild_title_df(
    *,
    corpus_path: Path | None = None,
    out_path: Path | None = None,
) -> RebuildReport:
    """Distil the corpus into the DF table and report the activation decision.

    Pure of threads/Telegram so it is directly unit-testable.
    """
    corpus = corpus_path if corpus_path is not None else _CORPUS_PATH
    out = out_path if out_path is not None else _TITLE_DF_PATH

    titles = iter_titles(corpus)
    payload = build_title_df_from_titles(titles)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = load_title_idf_stats(out)
    decision = describe_title_idf_activation(stats)

    token_df = payload.get("token_df") or {}
    bigram_df = payload.get("bigram_df") or {}
    return RebuildReport(
        corpus_titles=corpus_size(corpus),
        total_docs=int(payload.get("total_docs") or 0),
        token_vocab=len(token_df),
        bigram_vocab=len(bigram_df),
        activated=bool(decision["activated"]),
        reason=str(decision["reason"]),
        min_docs=int(decision["min_docs"]),
        canary_pass=bool(decision["canary_pass"]),
    )


def format_rebuild_notice(report: RebuildReport) -> str:
    """Human-readable Telegram message for one weekly rebuild."""
    if report.activated:
        head = "✅ Comp 比對 IDF 權重已啟用"
        tail = "高資訊量屬性(BOX/シュリンク付き/未開封…)現在會壓過泛用詞。"
    elif report.reason == "too_thin":
        head = "⏳ Comp 比對 IDF 表仍在養厚 — 維持純 Jaccard"
        tail = f"文件數 {report.total_docs} < 門檻 {report.min_docs},需要更多搜尋累積。"
    elif report.reason == "canary_failed":
        head = "⚠️ Comp 比對 IDF 表未通過金絲雀 — 維持純 Jaccard"
        tail = "語料夠厚但行為檢查不過(可能偏離核心商品域),暫不啟用以策安全。"
    else:
        head = "⏳ Comp 比對 IDF 表尚未建立 — 維持純 Jaccard"
        tail = "尚無可用表,維持 PR1 行為。"
    return (
        f"{head}\n"
        f"語料標題: {report.corpus_titles} | 文件: {report.total_docs} | "
        f"token詞彙: {report.token_vocab} | bigram詞彙: {report.bigram_vocab}\n"
        f"{tail}"
    )


class TitleCorpusRebuilder:
    """Daemon that rebuilds the IDF table weekly and reports on Telegram."""

    def __init__(
        self,
        *,
        notify_fn,
        corpus_path: Path | None = None,
        out_path: Path | None = None,
        interval_seconds: float = _WEEKLY_SECONDS,
        initial_delay_seconds: float = 600,
    ) -> None:
        self._notify_fn = notify_fn
        self._corpus_path = corpus_path
        self._out_path = out_path
        self._interval = interval_seconds
        self._initial_delay = initial_delay_seconds
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="title-corpus-rebuilder", daemon=True
        )
        self._thread.start()
        logger.info(
            "TitleCorpusRebuilder started — first run in %.0f min, then every %.1f days",
            self._initial_delay / 60,
            self._interval / 86400,
        )

    def _loop(self) -> None:
        time.sleep(self._initial_delay)
        while True:
            self.run_once()
            time.sleep(self._interval)

    def run_once(self) -> RebuildReport | None:
        try:
            report = rebuild_title_df(
                corpus_path=self._corpus_path, out_path=self._out_path
            )
        except Exception:
            logger.exception("TitleCorpusRebuilder: rebuild failed")
            return None
        logger.info(
            "TitleCorpusRebuilder: docs=%d activated=%s reason=%s",
            report.total_docs,
            report.activated,
            report.reason,
        )
        try:
            self._notify_fn(format_rebuild_notice(report))
        except Exception:
            logger.exception("TitleCorpusRebuilder: Telegram notify failed")
        return report
