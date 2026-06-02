"""Independent SQLite knowledge base for the /quiz feature.

Separate file from the SNS/codegen ``knowledge_db.py`` on purpose — quiz data
has nothing to do with market entities, so it lives in its own DB.

Two tables:
  - ``quiz_questions``: one verified multiple-choice question per row, built on a
    GENERIC source model (not hard-coded to songs). ``source_type`` distinguishes
    ``vocaloid_song`` / ``jpop_song`` / ``essay`` / … so new source kinds need no
    schema change. ``exam_point`` is free-text so new question types (文法 / 單字 /
    閱讀測驗 / …) need no schema change either — anything a multiple-choice question
    can express is allowed.
  - ``quiz_authoring_knowledge``: abstract, transferable rules about HOW to write a
    correct JLPT question — mirrors ``codegen_knowledge``. The generator retrieves
    these before each generation and injects them into the prompt, so reviewer
    corrections accumulate and the model improves over time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS quiz_questions (
    question_id      TEXT PRIMARY KEY,
    level            TEXT NOT NULL,
    exam_point       TEXT NOT NULL,
    stem             TEXT NOT NULL,
    options_json     TEXT NOT NULL DEFAULT '[]',
    answer_index     INTEGER NOT NULL,
    explanation      TEXT NOT NULL DEFAULT '',
    source_type      TEXT NOT NULL DEFAULT 'other',
    source_name      TEXT NOT NULL DEFAULT '',
    source_text_url  TEXT,
    source_media_url TEXT,
    source_excerpt   TEXT,
    verified         INTEGER NOT NULL DEFAULT 0,
    served_count     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quiz_level ON quiz_questions(level);
CREATE INDEX IF NOT EXISTS idx_quiz_source_type ON quiz_questions(source_type);
CREATE INDEX IF NOT EXISTS idx_quiz_verified ON quiz_questions(verified);

CREATE TABLE IF NOT EXISTS quiz_authoring_knowledge (
    knowledge_id  TEXT PRIMARY KEY,
    category      TEXT NOT NULL,
    title         TEXT NOT NULL,
    technique     TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    origin        TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.5,
    times_applied INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quiz_authoring_category ON quiz_authoring_knowledge(category);
"""


# Open sets — free-text values are accepted (writer side may invent new ones as the
# feature grows); these are just the canonical starting points.
AUTHORING_CATEGORIES: tuple[str, ...] = (
    "grammar", "vocabulary", "reading", "distractor_design",
    "level_calibration", "source_grounding",
)
AUTHORING_ORIGINS: tuple[str, ...] = ("seed", "distilled")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_question_id(*, level: str, source_name: str, stem: str) -> str:
    key = f"{(level or '').strip()}|{(source_name or '').strip()}|{(stem or '').strip()}"
    return sha1(key.encode("utf-8")).hexdigest()


def build_authoring_knowledge_id(*, category: str, title: str) -> str:
    return sha1(f"{category}|{title}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QuizQuestion:
    question_id: str
    level: str
    exam_point: str
    stem: str
    options: tuple[str, ...]
    answer_index: int
    explanation: str = ""
    source_type: str = "other"
    source_name: str = ""
    source_text_url: str | None = None
    source_media_url: str | None = None
    source_excerpt: str | None = None
    verified: bool = False
    served_count: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AuthoringKnowledge:
    knowledge_id: str
    category: str
    title: str
    technique: str
    keywords: tuple[str, ...] = ()
    origin: str = "seed"
    confidence: float = 0.5
    times_applied: int = 0
    created_at: str = ""
    updated_at: str = ""


class QuizDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Questions ─────────────────────────────────────────────────────────────

    def insert_question(
        self,
        *,
        level: str,
        exam_point: str,
        stem: str,
        options: tuple[str, ...],
        answer_index: int,
        explanation: str = "",
        source_type: str = "other",
        source_name: str = "",
        source_text_url: str | None = None,
        source_media_url: str | None = None,
        source_excerpt: str | None = None,
        verified: bool = True,
    ) -> QuizQuestion:
        """Insert (or overwrite, keyed on question_id) one question. Validates the
        multiple-choice shape so a malformed generation can't poison the pool."""
        level = (level or "").strip()
        stem = (stem or "").strip()
        opts = tuple(str(o).strip() for o in options if str(o).strip())
        if not level or not stem:
            raise ValueError("question requires level and stem")
        if len(opts) < 2:
            raise ValueError("question requires at least 2 options")
        if not (0 <= int(answer_index) < len(opts)):
            raise ValueError(f"answer_index {answer_index} out of range for {len(opts)} options")
        now = _utc_now_iso()
        question_id = build_question_id(level=level, source_name=source_name, stem=stem)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM quiz_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO quiz_questions (
                    question_id, level, exam_point, stem, options_json, answer_index,
                    explanation, source_type, source_name, source_text_url,
                    source_media_url, source_excerpt, verified, served_count,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(question_id) DO UPDATE SET
                    exam_point = excluded.exam_point,
                    options_json = excluded.options_json,
                    answer_index = excluded.answer_index,
                    explanation = excluded.explanation,
                    source_type = excluded.source_type,
                    source_text_url = excluded.source_text_url,
                    source_media_url = excluded.source_media_url,
                    source_excerpt = excluded.source_excerpt,
                    verified = excluded.verified,
                    updated_at = excluded.updated_at
                """,
                (
                    question_id, level, (exam_point or "").strip(), stem,
                    json.dumps(list(opts), ensure_ascii=False), int(answer_index),
                    (explanation or "").strip(), (source_type or "other").strip(),
                    (source_name or "").strip(), source_text_url, source_media_url,
                    source_excerpt, 1 if verified else 0, created_at, now,
                ),
            )
        loaded = self.get_question(question_id)
        assert loaded is not None
        return loaded

    def get_question(self, question_id: str) -> QuizQuestion | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_questions WHERE question_id = ?", (question_id,)
            ).fetchone()
        return _row_to_question(row) if row else None

    def random_question(
        self,
        *,
        level: str | None = None,
        source_type: str | None = None,
        verified_only: bool = True,
        prefer_unserved: bool = True,
    ) -> QuizQuestion | None:
        """Pick one question matching the filters. With ``prefer_unserved`` the
        least-served question is preferred (so the user sees fresh ones first),
        breaking ties randomly."""
        clauses: list[str] = []
        params: list[object] = []
        if verified_only:
            clauses.append("verified = 1")
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "served_count ASC, RANDOM()" if prefer_unserved else "RANDOM()"
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM quiz_questions{where} ORDER BY {order} LIMIT 1",
                tuple(params),
            ).fetchone()
        return _row_to_question(row) if row else None

    def recent_questions(self, limit: int = 50) -> list[QuizQuestion]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_questions ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_question(r) for r in rows]

    def count_verified(self, *, level: str | None = None, source_type: str | None = None) -> int:
        clauses = ["verified = 1"]
        params: list[object] = []
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type.strip())
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM quiz_questions WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_served(self, question_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE quiz_questions SET served_count = served_count + 1, updated_at = ? "
                "WHERE question_id = ?",
                (_utc_now_iso(), question_id),
            )

    def delete_question(self, question_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM quiz_questions WHERE question_id = ?", (question_id,)
            )
        return cursor.rowcount > 0

    # ── Authoring knowledge (self-improving) ──────────────────────────────────

    def upsert_authoring_knowledge(
        self,
        *,
        category: str,
        title: str,
        technique: str,
        keywords: tuple[str, ...] = (),
        origin: str = "distilled",
        confidence: float = 0.5,
    ) -> AuthoringKnowledge:
        """Insert or update one abstract authoring rule. Keyed on (category|title);
        higher confidence wins, equal-or-higher overwrites the technique text."""
        category = (category or "").strip() or "other"
        title = (title or "").strip()
        technique = (technique or "").strip()
        if not title or not technique:
            raise ValueError("authoring knowledge requires title and technique")
        if origin not in AUTHORING_ORIGINS:
            logger.warning("upsert_authoring_knowledge: unknown origin=%r", origin)
        now = _utc_now_iso()
        knowledge_id = build_authoring_knowledge_id(category=category, title=title)
        keywords_json = json.dumps([k.strip() for k in keywords if k.strip()], ensure_ascii=False)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT confidence, created_at FROM quiz_authoring_knowledge WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
            if existing is not None and float(existing["confidence"]) > float(confidence):
                logger.info("upsert_authoring_knowledge skip: existing confidence higher for %s", title)
            else:
                created_at = existing["created_at"] if existing else now
                conn.execute(
                    """
                    INSERT INTO quiz_authoring_knowledge (
                        knowledge_id, category, title, technique, keywords_json,
                        origin, confidence, times_applied, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(knowledge_id) DO UPDATE SET
                        category = excluded.category,
                        technique = excluded.technique,
                        keywords_json = excluded.keywords_json,
                        origin = excluded.origin,
                        confidence = excluded.confidence,
                        updated_at = excluded.updated_at
                    """,
                    (
                        knowledge_id, category, title, technique, keywords_json,
                        origin, float(confidence), created_at, now,
                    ),
                )
        loaded = self._get_authoring_knowledge(knowledge_id)
        assert loaded is not None
        return loaded

    def _get_authoring_knowledge(self, knowledge_id: str) -> AuthoringKnowledge | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_authoring_knowledge WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        return _row_to_authoring(row) if row else None

    def all_authoring_knowledge(self) -> list[AuthoringKnowledge]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_authoring_knowledge ORDER BY confidence DESC, updated_at DESC"
            ).fetchall()
        return [_row_to_authoring(r) for r in rows]

    def retrieve_authoring_knowledge(self, request_text: str, k: int = 6) -> list[AuthoringKnowledge]:
        """Return up to ``k`` rules most relevant to ``request_text`` by keyword /
        category / title token overlap, tie-broken by confidence."""
        rows = self.all_authoring_knowledge()
        if not rows:
            return []
        request_lc = (request_text or "").lower()
        scored: list[tuple[float, AuthoringKnowledge]] = []
        for row in rows:
            score = 0.0
            for kw in row.keywords:
                if kw and kw.lower() in request_lc:
                    score += 2.0
            if row.category and row.category.lower() in request_lc:
                score += 1.0
            for token in row.title.lower().split():
                if len(token) >= 3 and token in request_lc:
                    score += 0.5
            score += row.confidence
            scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in scored[: max(1, k)]]

    def mark_authoring_applied(self, knowledge_ids: tuple[str, ...]) -> None:
        if not knowledge_ids:
            return
        now = _utc_now_iso()
        with self.connect() as conn:
            for kid in knowledge_ids:
                conn.execute(
                    "UPDATE quiz_authoring_knowledge SET times_applied = times_applied + 1, "
                    "updated_at = ? WHERE knowledge_id = ?",
                    (now, kid),
                )

    def delete_authoring_knowledge(self, knowledge_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM quiz_authoring_knowledge WHERE knowledge_id = ?", (knowledge_id,)
            )
        return cursor.rowcount > 0


def _row_to_question(row: sqlite3.Row) -> QuizQuestion:
    try:
        opts = json.loads(row["options_json"] or "[]")
        if not isinstance(opts, list):
            opts = []
    except (TypeError, ValueError, json.JSONDecodeError):
        opts = []
    return QuizQuestion(
        question_id=row["question_id"],
        level=row["level"],
        exam_point=row["exam_point"],
        stem=row["stem"],
        options=tuple(str(o) for o in opts),
        answer_index=int(row["answer_index"]),
        explanation=row["explanation"] or "",
        source_type=row["source_type"],
        source_name=row["source_name"] or "",
        source_text_url=row["source_text_url"],
        source_media_url=row["source_media_url"],
        source_excerpt=row["source_excerpt"],
        verified=bool(row["verified"]),
        served_count=int(row["served_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_authoring(row: sqlite3.Row) -> AuthoringKnowledge:
    try:
        kws = json.loads(row["keywords_json"] or "[]")
        if not isinstance(kws, list):
            kws = []
    except (TypeError, ValueError, json.JSONDecodeError):
        kws = []
    return AuthoringKnowledge(
        knowledge_id=row["knowledge_id"],
        category=row["category"],
        title=row["title"],
        technique=row["technique"],
        keywords=tuple(str(k) for k in kws),
        origin=row["origin"],
        confidence=float(row["confidence"]),
        times_applied=int(row["times_applied"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def format_authoring_knowledge_block(rows: list[AuthoringKnowledge], *, max_chars: int = 2200) -> str:
    """Render retrieved rules into the generator prompt's authoring-guidance block."""
    if not rows:
        return "(無)"
    lines: list[str] = []
    used = 0
    for row in rows:
        line = f"- [{row.category}] {row.title}：{row.technique.strip()}"
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
