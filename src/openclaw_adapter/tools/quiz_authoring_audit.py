"""Audit quiz authoring quality for one author or the whole DB.

Usage:
    PYTHONPATH=src python -m openclaw_adapter.tools.quiz_authoring_audit
    PYTHONPATH=src python -m openclaw_adapter.tools.quiz_authoring_audit --author codex
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from openclaw_adapter.quiz_db import (  # noqa: E402
    QuizDatabase,
    answer_leaks_into_stem,
    infer_source_excerpt_type,
    is_grounded,
    options_have_duplicates,
    source_excerpt_type_conflicts_with_exam_point,
    youhou_target_word_presence_leaks,
)


def _configure_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit /quiz authoring quality.")
    parser.add_argument("--db-path", default="data/quiz.sqlite3", help="Path to quiz SQLite DB.")
    parser.add_argument("--author", default="codex", help="Filter to one author; empty means all authors.")
    parser.add_argument("--level", default="", help="Optional level filter, e.g. JLPT N1.")
    parser.add_argument("--quota", type=int, default=3, help="Per-source quota to enforce.")
    parser.add_argument("--show-limit", type=int, default=40, help="Max issue rows to print.")
    return parser


def _load_rows(db: QuizDatabase, *, author: str, level: str) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if author:
        clauses.append("author = ?")
        params.append(author)
    if level:
        clauses.append("level = ?")
        params.append(level)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.connect() as conn:
        rows = conn.execute(f"SELECT * FROM quiz_questions{where}", tuple(params)).fetchall()
    return list(rows)


def _question_issues(row: sqlite3.Row) -> list[tuple[str, str]]:
    try:
        options = tuple(str(o) for o in json.loads(row["options_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        options = ()
    issues: list[tuple[str, str]] = []
    excerpt_kind = infer_source_excerpt_type(
        source_text_url=row["source_text_url"],
        source_excerpt=row["source_excerpt"],
        source_name=row["source_name"],
        source_excerpt_type=row["source_excerpt_type"] if "source_excerpt_type" in row.keys() else None,
    )
    if not (row["tested_point"] or "").strip():
        issues.append(("missing_tested_point", "tested_point is empty"))
    if options_have_duplicates(options):
        issues.append(("duplicate_options", "two options normalize to the same text"))
    if answer_leaks_into_stem(
        stem=row["stem"], options=options, answer_index=int(row["answer_index"])
    ):
        issues.append(("answer_leaks_into_stem", "correct option appears verbatim in stem"))
    if not is_grounded(
        exam_point=row["exam_point"],
        stem=row["stem"],
        options=options,
        answer_index=int(row["answer_index"]),
        source_excerpt=row["source_excerpt"],
        explanation=row["explanation"] or "",
    ):
        issues.append(("ungrounded", "stem/options/explanation are not anchored in source_excerpt"))
    if source_excerpt_type_conflicts_with_exam_point(
        exam_point=row["exam_point"], source_excerpt_type=excerpt_kind
    ):
        issues.append(
            (
                "source_excerpt_type_conflict",
                f"exam_point={row['exam_point']} should not use source_excerpt_type={excerpt_kind}",
            )
        )
    if youhou_target_word_presence_leaks(
        exam_point=row["exam_point"],
        stem=row["stem"],
        options=options,
        answer_index=int(row["answer_index"]),
    ):
        issues.append(
            (
                "youhou_target_presence_leak",
                "only the correct option contains the asked target word form",
            )
        )
    return issues


def main(argv: list[str] | None = None) -> int:
    args = _configure_parser().parse_args(argv)
    db = QuizDatabase(args.db_path)
    rows = _load_rows(db, author=args.author.strip(), level=args.level.strip())

    exam_point_counts = Counter((row["exam_point"] or "").strip() for row in rows)
    source_counts = Counter((row["source_name"] or "").strip() for row in rows if (row["source_name"] or "").strip())
    issues: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        for code, detail in _question_issues(row):
            issues.append(
                (
                    row["question_id"],
                    (row["exam_point"] or "").strip(),
                    (row["source_name"] or "").strip(),
                    code,
                    detail,
                )
            )
    over_quota = [(name, n) for name, n in sorted(source_counts.items()) if n > args.quota]

    print(f"rows={len(rows)} author={args.author or '(all)'} level={args.level or '(all)'}")
    print("counts:")
    for exam_point, count in sorted(exam_point_counts.items()):
        print(f"  {exam_point}: {count}")
    print(f"issues={len(issues)} over_quota={len(over_quota)}")
    for name, n in over_quota:
        print(f"OVER_QUOTA\t{name}\t{n}")
    for qid, exam_point, source_name, code, detail in issues[: max(0, int(args.show_limit))]:
        print(f"{code}\t{qid}\t{exam_point}\t{source_name}\t{detail}")
    if len(issues) > int(args.show_limit):
        print(f"... truncated {len(issues) - int(args.show_limit)} more issue rows")
    return 1 if issues or over_quota else 0


if __name__ == "__main__":
    raise SystemExit(main())
