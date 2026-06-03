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
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


# ── Source-grounding gate ──────────────────────────────────────────────────────
# HARD INVARIANT (enforced here, not merely advised in the authoring KB): every
# question's answer-bearing, user-visible text must come from real source text —
# a real lyric line or a real article sentence — never a fabricated sentence
# merely "themed on" a song. The KB can't enforce this (it's a soft RAG hint that
# can be missed); this gate makes ungrounded questions impossible to insert.

# exam_points whose 本文 (== source_excerpt) is itself rendered to the user.
READING_MARKERS: tuple[str, ...] = ("内容理解", "主張", "統合", "情報検索", "読解")

# Tokens we treat as a blank / reorder slot when splitting a stem.
_BLANK_SPLIT = re.compile(r"[＿_]{1,}|★|[（(][\s　]*[）)]")
# Whitespace, line separators and sentence punctuation to ignore when matching.
_NOISE = re.compile(r"[\s　、。，．・…‥「」『』（）()〈〉【】！？!?～~／/＝=]+")
# Quoted spans inside a stem — these hold the real line for 言い換え / quoted types.
_QUOTED = re.compile(r"[「『]([^「『」』]+)[」』]")
# Minimum verbatim run that counts as a real-text anchor (avoid trivial overlaps).
_MIN_ANCHOR = 4
# A cloze carrier must reproduce at least this fraction of the real line — a genuine
# blank removes only one word, so coverage stays high; a fabricated paraphrase that
# merely reuses a short fragment (e.g.「どれも不正解」) covers far less and is rejected.
_MIN_COVERAGE = 0.6


def is_reading_exam_point(exam_point: str | None) -> bool:
    ep = exam_point or ""
    return any(m in ep for m in READING_MARKERS)


def _normalize_grounding(text: str | None) -> str:
    return _NOISE.sub("", text or "")


def _stem_segments(stem: str) -> list[str]:
    """Split a stem on blank/reorder slots, keeping bracketed real words (the 〈〉
    of a 言い換え target is part of the real line), then normalize each piece."""
    pieces = _BLANK_SPLIT.split(stem or "")
    return [seg for seg in (_normalize_grounding(p) for p in pieces) if len(seg) >= 2]


def _longest_common_substring_len(a: str, b: str) -> int:
    """Length of the longest contiguous run shared by ``a`` and ``b``. Used to
    confirm a cloze carrier IS the real line even when meta boilerplate ("…の」の…")
    bleeds into the carrier segment — a verbatim run survives, a paraphrase doesn't."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch_a in a:
        cur = [0] * (len(b) + 1)
        for j, ch_b in enumerate(b):
            if ch_a == ch_b:
                cur[j + 1] = prev[j] + 1
                if cur[j + 1] > best:
                    best = cur[j + 1]
        prev = cur
    return best


def is_grounded(
    *,
    exam_point: str,
    stem: str,
    options: tuple[str, ...],
    answer_index: int,
    source_excerpt: str | None,
    explanation: str = "",
) -> bool:
    """True iff the question is anchored in real source text, so fabrication is
    rejected. Grounding is satisfied by ANY of three tiers (the user's ruling):

      A. the stem IS the real line (cloze carrier verbatim, or 言い換え/漢字読み quote),
      B. the CORRECT OPTION is the real line (the only stem-side anchor for 用法),
      C. the EXPLANATION quotes the equivalent real line verbatim (等価於哪句原歌詞) —
         needed because N1 grammar rarely appears verbatim in lyrics; a constructed
         stem is acceptable ONLY if it transparently cites the line it mirrors.

    Reading types are grounded by presence (本文 == excerpt is rendered verbatim).
    """
    excerpt = _normalize_grounding(source_excerpt)
    if len(excerpt) < _MIN_ANCHOR:
        return False
    ep = exam_point or ""

    # Reading comp: the 本文 (== excerpt) is itself rendered to the user verbatim.
    if is_reading_exam_point(ep):
        return True

    # Tier C — the explanation quotes the full equivalent real line.
    if excerpt in _normalize_grounding(explanation):
        return True

    # 用法: the test sentences (options) are necessarily constructed, so the ONLY
    # real-text anchor is the correct option being the actual lyric line. A mere
    # headword match (e.g. quoted「ほろ苦い」appearing in the lyric) is NOT enough.
    if "用法" in ep:
        if 0 <= answer_index < len(options):
            correct = _normalize_grounding(options[answer_index])
            return len(correct) >= _MIN_ANCHOR and correct in excerpt
        return False

    # Cloze / grammar / 組み立て (stem carries a blank slot): the carrier sentence
    # minus its blank must BE the real line. A genuine cloze removes only one word,
    # so the surviving segments still cover MOST of the real line; a fabricated
    # paraphrase that merely reuses a short fragment (LCS 6–11 chars) covers far
    # less. We require coverage ≥ _MIN_COVERAGE of the excerpt, summing the verbatim
    # run each stem segment contributes. Meta boilerplate ("…に入るものは…") shares no
    # run with the excerpt, so it adds nothing and is naturally ignored.
    if _BLANK_SPLIT.search(stem or ""):
        segs = _stem_segments(stem)
        covered = sum(
            min(len(s), _longest_common_substring_len(s, excerpt)) for s in segs
        )
        return covered >= _MIN_COVERAGE * len(excerpt)

    # No blank (言い換え / 漢字読み / quoted carriers): the real line is quoted in the
    # stem, or the whole excerpt is embedded in the stem.
    if excerpt in _normalize_grounding(stem):
        return True
    for span in _QUOTED.findall(stem or ""):
        anchor = _normalize_grounding(span)
        if len(anchor) >= _MIN_ANCHOR and anchor in excerpt:
            return True
    # Last resort: the correct option happens to be the real line.
    if 0 <= answer_index < len(options):
        correct = _normalize_grounding(options[answer_index])
        if len(correct) >= _MIN_ANCHOR and correct in excerpt:
            return True
    return False


def correct_option_is_verbatim_copy(
    *,
    options: tuple[str, ...],
    answer_index: int,
    source_excerpt: str | None,
    threshold: float = 0.9,
    distractor_ceiling: float = 0.7,
) -> bool:
    """True iff the CORRECT option is a (near-)verbatim span of 本文 while the
    distractors are not — i.e. a reading question degenerates to 'spot the copied
    sentence', requiring no comprehension. Enforces the 内容理解 逐字コピー禁止 rule
    structurally (the prompt-level reminder alone doesn't stop the model copying).

    Coverage = longest shared run / option length, measured on noise-stripped text
    (the same normalization is_grounded uses), so punctuation/spacing differences
    don't hide a verbatim lift. Scoped by the caller to reading types only — for
    用法 the correct option being the real line is the *required* grounding anchor.
    """
    excerpt = _normalize_grounding(source_excerpt)
    if len(excerpt) < _MIN_ANCHOR or not (0 <= answer_index < len(options)):
        return False

    def coverage(opt: str) -> float:
        normalized = _normalize_grounding(opt)
        if len(normalized) < _MIN_ANCHOR:
            return 0.0
        return _longest_common_substring_len(normalized, excerpt) / len(normalized)

    correct = coverage(options[answer_index])
    distractor = max(
        (coverage(o) for i, o in enumerate(options) if i != answer_index),
        default=0.0,
    )
    return correct >= threshold and distractor < distractor_ceiling


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
    author           TEXT NOT NULL DEFAULT 'Claude',
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
    author: str = "Claude"
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
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_questions)")}
            if "author" not in cols:
                conn.execute(
                    "ALTER TABLE quiz_questions ADD COLUMN author TEXT NOT NULL DEFAULT 'Claude'"
                )

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
        author: str = "Claude",
        allow_ungrounded: bool = False,
    ) -> QuizQuestion:
        """Insert (or overwrite, keyed on question_id) one question. Validates the
        multiple-choice shape AND source-grounding so a fabricated or malformed
        generation can't poison the pool. ``allow_ungrounded`` bypasses only the
        grounding gate (for tests of unrelated DB mechanics)."""
        level = (level or "").strip()
        stem = (stem or "").strip()
        opts = tuple(str(o).strip() for o in options if str(o).strip())
        if not level or not stem:
            raise ValueError("question requires level and stem")
        if len(opts) < 2:
            raise ValueError("question requires at least 2 options")
        if not (0 <= int(answer_index) < len(opts)):
            raise ValueError(f"answer_index {answer_index} out of range for {len(opts)} options")
        if not allow_ungrounded and not is_grounded(
            exam_point=exam_point or "",
            stem=stem,
            options=opts,
            answer_index=int(answer_index),
            source_excerpt=source_excerpt,
            explanation=explanation or "",
        ):
            raise ValueError(
                "question not grounded: the user-visible real text (stem minus "
                "blanks, the quoted line, or the correct option) must be a verbatim "
                "substring of source_excerpt — no fabricated sentences"
            )
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
                    author, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
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
                    author = excluded.author,
                    updated_at = excluded.updated_at
                """,
                (
                    question_id, level, (exam_point or "").strip(), stem,
                    json.dumps(list(opts), ensure_ascii=False), int(answer_index),
                    (explanation or "").strip(), (source_type or "other").strip(),
                    (source_name or "").strip(), source_text_url, source_media_url,
                    source_excerpt, 1 if verified else 0,
                    (author or "Claude").strip(), created_at, now,
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
        author=row["author"],
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
