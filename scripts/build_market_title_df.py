#!/usr/bin/env python3
"""Build the historical title DF/IDF table for /research comp filtering (PR2).

Reads marketplace titles that have ALREADY been cached locally (no new external
queries — Rule G / no IP risk) and writes a document-frequency table that
`research_command.load_title_idf_stats` consumes to down-weight generic family
tokens against rare, high-information attributes (BOX / シュリンク付き / …).

Default source is the market title corpus (data/market_title_corpus.sqlite3),
which /research and /opportunity fill passively from searches they already run.
Falls back to the `source_offers.title` price-tracking table in monitor.sqlite3,
or a plain newline-delimited titles file.

Titles are de-duplicated first: the same listing is seen on many scans, and
counting every sighting would wrongly inflate a product's tokens into looking
"generic". One distinct title == one document.

Usage:
    .venv/bin/python scripts/build_market_title_df.py                 # corpus sink
    .venv/bin/python scripts/build_market_title_df.py --source source_offers
    .venv/bin/python scripts/build_market_title_df.py --titles-file titles.txt
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from openclaw_adapter.market_title_corpus import iter_titles  # noqa: E402
from openclaw_adapter.research_command import build_title_df_from_titles  # noqa: E402


def _titles_from_sqlite(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT title FROM source_offers WHERE title IS NOT NULL AND title != ''"
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def _titles_from_file(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", choices=("corpus", "source_offers"), default="corpus",
        help="corpus = passive title sink (default); source_offers = price tracker",
    )
    parser.add_argument(
        "--corpus", type=Path, default=REPO_ROOT / "data" / "market_title_corpus.sqlite3"
    )
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "monitor.sqlite3")
    parser.add_argument("--titles-file", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "market_title_df.json")
    args = parser.parse_args()

    if args.titles_file is not None:
        titles = _titles_from_file(args.titles_file)
        source_desc = f"titles file {args.titles_file}"
    elif args.source == "source_offers":
        titles = _titles_from_sqlite(args.db)
        source_desc = f"{args.db} source_offers (DISTINCT title)"
    else:
        titles = iter_titles(args.corpus)
        source_desc = f"{args.corpus} title corpus"

    payload = build_title_df_from_titles(titles)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"source: {source_desc}")
    print(f"input titles: {len(titles)}")
    print(
        f"documents: {payload['total_docs']} | "
        f"token vocab: {len(payload['token_df'])} | "
        f"bigram vocab: {len(payload['bigram_df'])}"
    )
    print(f"wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
