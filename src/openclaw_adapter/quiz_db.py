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
import random
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator

from .quiz_vocab_seed import QUIZ_VOCAB_SEED

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
SOURCE_EXCERPT_TYPES: tuple[str, ...] = ("lyric", "title", "article", "commentary", "other")
_LYRIC_URL_MARKERS: tuple[str, ...] = (
    "/lyric/",
    "lyrics.php",
    "uta-net.com/song/",
    "atwiki.jp/hmiku/pages/",
    "miraheze.org/wiki/",
)


def is_reading_exam_point(exam_point: str | None) -> bool:
    ep = exam_point or ""
    return any(m in ep for m in READING_MARKERS)


# 考点 = the specific knowledge item a question tests (one word / one grammar
# pattern), as opposed to exam_point which is only the 題型 (question format).
# Mastery is tracked at this grain so a learner weak on one specific item gets
# it more often without inflating the whole 題型.
# A 考点 word in these question types is bracketed in the stem — 〈…〉 / 「…」 / 『…』 —
# and repeated just before a keyword (読み方 / 最も近い / 使い方). Phrasing varies
# (の意味に / の語感に / に意味が…), so we take the LAST bracketed token preceding
# the keyword rather than match one fixed template.
_BRACKET_TOKEN = re.compile(r"[〈「『]([^〈「『〉」』]+)[〉」』]")
# Cloze / grammar-form / passage-grammar: the blank's correct option IS the 考点.
_ANSWER_IS_POINT = ("文脈規定", "文法形式の判断", "文章の文法")


def _token_before(stem: str, keyword: str) -> str | None:
    idx = stem.find(keyword)
    if idx < 0:
        return None
    last = None
    for m in _BRACKET_TOKEN.finditer(stem):
        if m.end() <= idx:
            last = m.group(1).strip()
    return last or None


def derive_tested_point(
    *, exam_point: str | None, stem: str, options, answer_index: int
) -> str | None:
    """Deterministically extract the 考点 from a question's own text. Returns None
    for types with no clean single point (文の組み立て / 読解), which then fall back
    to 題型-grain weighting. Used both to backfill legacy rows and as a generator
    fallback when the author LLM omits tested_point."""
    ep = (exam_point or "").strip()
    stem = stem or ""
    if "漢字読み" in ep:
        return _token_before(stem, "読み方")
    if "言い換え" in ep or "類義" in ep:
        return _token_before(stem, "最も近い")
    if "用法" in ep:
        return _token_before(stem, "使い方")
    if any(k in ep for k in _ANSWER_IS_POINT):
        opts = list(options or [])
        if 0 <= answer_index < len(opts):
            return str(opts[answer_index]).strip() or None
    return None


def _normalize_grounding(text: str | None) -> str:
    return _NOISE.sub("", text or "")


def _normalize_source_excerpt_type(source_excerpt_type: str | None) -> str:
    kind = (source_excerpt_type or "").strip().lower()
    return kind if kind in SOURCE_EXCERPT_TYPES else "other"


def infer_source_excerpt_type(
    *,
    source_text_url: str | None,
    source_excerpt: str | None,
    source_name: str | None,
    source_excerpt_type: str | None = None,
) -> str:
    """Return the best-known source excerpt kind.

    Explicit stored values win. Otherwise infer conservatively from exact title
    matches and well-known lyric/article URL patterns.
    """
    explicit = (source_excerpt_type or "").strip().lower()
    if explicit in SOURCE_EXCERPT_TYPES and explicit != "other":
        return explicit

    excerpt = _normalize_grounding(source_excerpt)
    name = _normalize_grounding(source_name)
    if excerpt and name and excerpt == name:
        return "title"

    url = (source_text_url or "").strip().lower()
    if any(marker in url for marker in _LYRIC_URL_MARKERS):
        return "lyric"
    if "mitchie-m.com/blog/" in url and "/lyrics/" in url:
        return "lyric"
    if "specialarticle" in url:
        return "article"
    if "vocadb.net/s/" in url:
        return "commentary"
    return "other"


def source_excerpt_type_conflicts_with_exam_point(
    *, exam_point: str | None, source_excerpt_type: str | None
) -> bool:
    """Known-bad pairings between excerpt kind and question type.

    This intentionally rejects only combinations that have already produced bad
    questions in practice, keeping compatibility with older ambiguous rows.
    """
    ep = (exam_point or "").strip()
    kind = _normalize_source_excerpt_type(source_excerpt_type)
    if is_reading_exam_point(ep):
        return False
    if kind in {"article", "commentary"}:
        return True
    if kind == "title" and "漢字読み" not in ep:
        return True
    return False


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

    # 文の組み立て: the four options ARE the real sentence's fragments (the question
    # is to reorder them), so the only fabrication risk is an invented fragment.
    # Grounding is satisfied iff every option is a verbatim piece of the real line;
    # correctness of the ordering is the grader's job, not grounding's.
    if "組み立て" in ep or "組立て" in ep or "組立" in ep:
        frags = [f for f in (_normalize_grounding(o) for o in options) if len(f) >= 2]
        return len(frags) >= 2 and all(f in excerpt for f in frags)

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
    # minus its blank must BE a real line. A genuine cloze removes only one word, so
    # the surviving segments still cover MOST of that line; a fabricated paraphrase
    # that merely reuses a short fragment (LCS 6–11 chars) covers far less.
    # Coverage is measured against the CARRIER's own length, capped by the excerpt —
    # a single-line cloze cannot span a multi-line excerpt, yet it is fully grounded
    # as long as it reproduces a real line minus its one blank. The min(...) cap
    # keeps this backward-compatible with short single-line excerpts (where the old
    # excerpt-relative threshold was equivalent). An absolute floor rejects trivially
    # short carriers.
    if _BLANK_SPLIT.search(stem or ""):
        segs = _stem_segments(stem)
        covered = sum(
            min(len(s), _longest_common_substring_len(s, excerpt)) for s in segs
        )
        carrier_len = sum(len(s) for s in segs)
        return (
            carrier_len > 0
            and covered >= _MIN_ANCHOR
            and covered >= _MIN_COVERAGE * min(len(excerpt), carrier_len)
        )

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


def options_have_duplicates(options: tuple[str, ...]) -> bool:
    """True iff two options are the same after noise-stripping. A multiple-choice
    item with identical options has zero discrimination (the grader 'agrees'
    trivially); this catches that structurally, for ANY question type."""
    seen: set[str] = set()
    for opt in options:
        key = _normalize_grounding(opt)
        if not key:
            continue
        if key in seen:
            return True
        seen.add(key)
    return False


def answer_leaks_into_stem(
    *, stem: str, options: tuple[str, ...], answer_index: int
) -> bool:
    """True iff the correct option appears verbatim inside the stem — the answer is
    handed to the solver, so the item tests nothing. Measured on noise-stripped text
    with a minimum length so an incidental short kana run isn't a false positive."""
    if not (0 <= answer_index < len(options)):
        return False
    correct = _normalize_grounding(options[answer_index])
    if len(correct) < _MIN_ANCHOR:
        return False
    return correct in _normalize_grounding(stem)


def youhou_target_word_presence_leaks(
    *, exam_point: str | None, stem: str, options: tuple[str, ...], answer_index: int
) -> bool:
    """True iff a 用法 item is answerable just by spotting the target word.

    The common failure mode is: only the correct option contains the asked word,
    while every distractor swaps it out for a near-synonym. That tests visual
    presence, not usage.
    """
    ep = (exam_point or "").strip()
    if "用法" not in ep or not (0 <= answer_index < len(options)):
        return False
    target = _normalize_grounding(_token_before(stem or "", "使い方"))
    if len(target) < 2:
        return False

    def _contains_target_form(opt: str) -> bool:
        normalized = _normalize_grounding(opt)
        if not normalized:
            return False
        overlap = _longest_common_substring_len(target, normalized)
        needed = max(2, int(len(target) * 0.6))
        return overlap >= needed

    if not _contains_target_form(options[answer_index]):
        return False
    distractor_hits = sum(
        1 for i, opt in enumerate(options)
        if i != answer_index and _contains_target_form(opt)
    )
    return distractor_hits == 0


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
    source_excerpt_type TEXT NOT NULL DEFAULT 'other',
    tested_point     TEXT,
    verified         INTEGER NOT NULL DEFAULT 0,
    served_count     INTEGER NOT NULL DEFAULT 0,
    author           TEXT NOT NULL DEFAULT 'Claude',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quiz_level ON quiz_questions(level);
CREATE INDEX IF NOT EXISTS idx_quiz_source_type ON quiz_questions(source_type);
CREATE INDEX IF NOT EXISTS idx_quiz_verified ON quiz_questions(verified);

-- Per-answer log driving adaptive selection. Mastery is computed per learner
-- (chat_id) at two grains: the specific 考点 (tested_point) when known, else the
-- 題型 (exam_point). Both are denormalized here so aggregation needs no join.
CREATE TABLE IF NOT EXISTS quiz_attempts (
    attempt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id  TEXT NOT NULL,
    exam_point   TEXT NOT NULL DEFAULT '',
    tested_point TEXT,
    level        TEXT NOT NULL DEFAULT '',
    chat_id      TEXT NOT NULL DEFAULT '',
    chosen_index INTEGER NOT NULL,
    correct      INTEGER NOT NULL,
    answered_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempt_chat_ep ON quiz_attempts(chat_id, exam_point);
CREATE INDEX IF NOT EXISTS idx_attempt_chat_tp ON quiz_attempts(chat_id, tested_point);
CREATE INDEX IF NOT EXISTS idx_attempt_chat_q ON quiz_attempts(chat_id, question_id);

CREATE TABLE IF NOT EXISTS quiz_vocab_cards (
    vocab_id                  TEXT PRIMARY KEY,
    level                     TEXT NOT NULL,
    headword                  TEXT NOT NULL,
    reading_hiragana          TEXT NOT NULL,
    zh_gloss_short            TEXT NOT NULL,
    example_ja                TEXT NOT NULL,
    example_source_kind       TEXT NOT NULL DEFAULT 'adapted',
    source_name               TEXT NOT NULL DEFAULT '',
    source_text_url           TEXT,
    primary_question_id       TEXT NOT NULL,
    support_question_ids_json TEXT NOT NULL DEFAULT '[]',
    exam_points_json          TEXT NOT NULL DEFAULT '[]',
    author                    TEXT NOT NULL DEFAULT 'codex',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vocab_level_author ON quiz_vocab_cards(level, author);
CREATE INDEX IF NOT EXISTS idx_vocab_headword ON quiz_vocab_cards(headword);
CREATE INDEX IF NOT EXISTS idx_vocab_source_name ON quiz_vocab_cards(source_name);

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
VOCAB_CARD_EXAM_POINTS: tuple[str, ...] = ("漢字読み", "言い換え類義", "文脈規定", "用法")
_VOCAB_PRIMARY_PRIORITY: dict[str, int] = {
    "用法": 0,
    "文脈規定": 1,
    "言い換え類義": 2,
    "漢字読み": 3,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_question_id(*, level: str, source_name: str, stem: str) -> str:
    key = f"{(level or '').strip()}|{(source_name or '').strip()}|{(stem or '').strip()}"
    return sha1(key.encode("utf-8")).hexdigest()


def build_authoring_knowledge_id(*, category: str, title: str) -> str:
    return sha1(f"{category}|{title}".encode("utf-8")).hexdigest()


def build_vocab_card_id(*, level: str, headword: str) -> str:
    return sha1(f"{level}|{headword}".encode("utf-8")).hexdigest()


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
    source_excerpt_type: str = "other"
    tested_point: str | None = None
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


@dataclass(frozen=True)
class QuizVocabCard:
    vocab_id: str
    level: str
    headword: str
    reading_hiragana: str
    zh_gloss_short: str
    example_ja: str
    example_source_kind: str = "adapted"
    source_name: str = ""
    source_text_url: str | None = None
    primary_question_id: str = ""
    support_question_ids: tuple[str, ...] = ()
    exam_points: tuple[str, ...] = ()
    author: str = "codex"
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
            if "tested_point" not in cols:
                conn.execute("ALTER TABLE quiz_questions ADD COLUMN tested_point TEXT")
            if "source_excerpt_type" not in cols:
                conn.execute(
                    "ALTER TABLE quiz_questions ADD COLUMN source_excerpt_type TEXT "
                    "NOT NULL DEFAULT 'other'"
                )
            # "other" is only a fallback bucket, not a trustworthy explicit value.
            # Re-infer it on every startup so previously migrated rows converge.
            self._backfill_source_excerpt_types(conn, overwrite_other=True)
            self._backfill_vocab_cards(conn)

    def _backfill_source_excerpt_types(
        self, conn: sqlite3.Connection, *, overwrite_other: bool
    ) -> None:
        where = [
            "source_excerpt_type IS NULL",
            "TRIM(source_excerpt_type) = ''",
        ]
        if overwrite_other:
            where.append("source_excerpt_type = 'other'")
        rows = conn.execute(
            "SELECT question_id, source_text_url, source_excerpt, source_name, "
            "source_excerpt_type FROM quiz_questions WHERE " + " OR ".join(where)
        ).fetchall()
        if not rows:
            return
        now = _utc_now_iso()
        for row in rows:
            inferred = infer_source_excerpt_type(
                source_text_url=row["source_text_url"],
                source_excerpt=row["source_excerpt"],
                source_name=row["source_name"],
                source_excerpt_type=None,
            )
            if inferred == _normalize_source_excerpt_type(row["source_excerpt_type"]):
                continue
            conn.execute(
                "UPDATE quiz_questions SET source_excerpt_type = ?, updated_at = ? "
                "WHERE question_id = ?",
                (inferred, now, row["question_id"]),
            )

    def _backfill_vocab_cards(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT * FROM quiz_questions
            WHERE verified = 1
              AND author = 'codex'
              AND level = 'JLPT N1'
              AND tested_point IS NOT NULL
              AND TRIM(tested_point) <> ''
              AND exam_point IN (?, ?, ?, ?)
            ORDER BY created_at ASC, question_id ASC
            """,
            VOCAB_CARD_EXAM_POINTS,
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["tested_point"] or "").strip(), []).append(row)

        seen_ids: set[str] = set()
        now = _utc_now_iso()
        for headword, group in groups.items():
            seed = QUIZ_VOCAB_SEED.get(headword)
            if not seed:
                continue
            group.sort(
                key=lambda r: (
                    _VOCAB_PRIMARY_PRIORITY.get((r["exam_point"] or "").strip(), 99),
                    r["created_at"] or "",
                    r["question_id"] or "",
                )
            )
            primary = group[0]
            support_ids = tuple(dict.fromkeys((r["question_id"] or "").strip() for r in group if (r["question_id"] or "").strip()))
            exam_points = tuple(
                ep for ep, _ in sorted(
                    {
                        ((r["exam_point"] or "").strip(), _VOCAB_PRIMARY_PRIORITY.get((r["exam_point"] or "").strip(), 99))
                        for r in group
                        if (r["exam_point"] or "").strip()
                    },
                    key=lambda pair: (pair[1], pair[0]),
                )
            )
            vocab_id = build_vocab_card_id(level="JLPT N1", headword=headword)
            seen_ids.add(vocab_id)
            existing = conn.execute(
                "SELECT created_at FROM quiz_vocab_cards WHERE vocab_id = ?",
                (vocab_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO quiz_vocab_cards (
                    vocab_id, level, headword, reading_hiragana, zh_gloss_short,
                    example_ja, example_source_kind, source_name, source_text_url,
                    primary_question_id, support_question_ids_json, exam_points_json,
                    author, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vocab_id) DO UPDATE SET
                    reading_hiragana = excluded.reading_hiragana,
                    zh_gloss_short = excluded.zh_gloss_short,
                    example_ja = excluded.example_ja,
                    example_source_kind = excluded.example_source_kind,
                    source_name = excluded.source_name,
                    source_text_url = excluded.source_text_url,
                    primary_question_id = excluded.primary_question_id,
                    support_question_ids_json = excluded.support_question_ids_json,
                    exam_points_json = excluded.exam_points_json,
                    author = excluded.author,
                    updated_at = excluded.updated_at
                """,
                (
                    vocab_id,
                    "JLPT N1",
                    headword,
                    seed["reading_hiragana"],
                    seed["zh_gloss_short"],
                    seed["example_ja"],
                    seed.get("example_source_kind", "adapted"),
                    (primary["source_name"] or "").strip(),
                    primary["source_text_url"],
                    (primary["question_id"] or "").strip(),
                    json.dumps(list(support_ids), ensure_ascii=False),
                    json.dumps(list(exam_points), ensure_ascii=False),
                    "codex",
                    created_at,
                    now,
                ),
            )
        if seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            conn.execute(
                f"DELETE FROM quiz_vocab_cards WHERE author = 'codex' AND level = 'JLPT N1' AND vocab_id NOT IN ({placeholders})",
                tuple(seen_ids),
            )
        else:
            conn.execute(
                "DELETE FROM quiz_vocab_cards WHERE author = 'codex' AND level = 'JLPT N1'"
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
        source_excerpt_type: str | None = None,
        tested_point: str | None = None,
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
        excerpt_kind = infer_source_excerpt_type(
            source_text_url=source_text_url,
            source_excerpt=source_excerpt,
            source_name=source_name,
            source_excerpt_type=source_excerpt_type,
        )
        if not level or not stem:
            raise ValueError("question requires level and stem")
        if len(opts) < 2:
            raise ValueError("question requires at least 2 options")
        if not (0 <= int(answer_index) < len(opts)):
            raise ValueError(f"answer_index {answer_index} out of range for {len(opts)} options")
        if source_excerpt_type_conflicts_with_exam_point(
            exam_point=exam_point, source_excerpt_type=excerpt_kind
        ):
            raise ValueError(
                "source_excerpt_type conflicts with exam_point: non-reading questions "
                "must not ground on commentary/article text, and title-only grounding "
                "is limited to 漢字読み"
            )
        if youhou_target_word_presence_leaks(
            exam_point=exam_point, stem=stem, options=opts, answer_index=int(answer_index)
        ):
            raise ValueError(
                "youhou item leaks by target-word presence: at least one distractor "
                "must also use the target word form"
            )
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
                    source_media_url, source_excerpt, source_excerpt_type,
                    tested_point, verified,
                    served_count, author, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(question_id) DO UPDATE SET
                    exam_point = excluded.exam_point,
                    options_json = excluded.options_json,
                    answer_index = excluded.answer_index,
                    explanation = excluded.explanation,
                    source_type = excluded.source_type,
                    source_text_url = excluded.source_text_url,
                    source_media_url = excluded.source_media_url,
                    source_excerpt = excluded.source_excerpt,
                    source_excerpt_type = excluded.source_excerpt_type,
                    tested_point = excluded.tested_point,
                    verified = excluded.verified,
                    author = excluded.author,
                    updated_at = excluded.updated_at
                """,
                (
                    question_id, level, (exam_point or "").strip(), stem,
                    json.dumps(list(opts), ensure_ascii=False), int(answer_index),
                    (explanation or "").strip(), (source_type or "other").strip(),
                    (source_name or "").strip(), source_text_url, source_media_url,
                    source_excerpt, excerpt_kind, (tested_point or "").strip() or None,
                    1 if verified else 0,
                    (author or "Claude").strip(), created_at, now,
                ),
            )
            self._backfill_vocab_cards(conn)
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

    def exam_point_counts(
        self, *, level: str | None = None, verified_only: bool = True,
        author: str | None = None,
    ) -> list[tuple[str, int]]:
        """List distinct 題型 (exam_point) and how many questions each has, for the
        type-selection menu. Ordered by count desc then name so the menu is stable.
        ``author`` restricts the tally to one 出題者 (the /quiz byauthor path)."""
        clauses: list[str] = []
        params: list[object] = []
        if verified_only:
            clauses.append("verified = 1")
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT exam_point AS ep, COUNT(*) AS n FROM quiz_questions{where} "
                "GROUP BY exam_point ORDER BY n DESC, exam_point ASC",
                tuple(params),
            ).fetchall()
        return [((r["ep"] or "").strip(), int(r["n"])) for r in rows if (r["ep"] or "").strip()]

    def author_counts(
        self, *, level: str | None = None, verified_only: bool = True
    ) -> list[tuple[str, int]]:
        """List distinct 出題者 (author) and how many questions each has, for the
        /quiz byauthor author-selection menu. Ordered by count desc then name."""
        clauses: list[str] = []
        params: list[object] = []
        if verified_only:
            clauses.append("verified = 1")
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT author AS a, COUNT(*) AS n FROM quiz_questions{where} "
                "GROUP BY author ORDER BY n DESC, author ASC",
                tuple(params),
            ).fetchall()
        return [((r["a"] or "").strip(), int(r["n"])) for r in rows if (r["a"] or "").strip()]

    def mark_served(self, question_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE quiz_questions SET served_count = served_count + 1, updated_at = ? "
                "WHERE question_id = ?",
                (_utc_now_iso(), question_id),
            )

    # ── Adaptive selection (answer history → mastery-weighted serving) ─────────
    def record_attempt(
        self,
        *,
        question_id: str,
        exam_point: str,
        tested_point: str | None,
        level: str,
        chat_id: str,
        chosen_index: int,
        correct: bool,
    ) -> None:
        """Log one graded answer. Drives weighted_question's mastery model."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO quiz_attempts (question_id, exam_point, tested_point, "
                "level, chat_id, chosen_index, correct, answered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    question_id, (exam_point or "").strip(),
                    (tested_point or "").strip() or None, (level or "").strip(),
                    str(chat_id or ""), int(chosen_index), 1 if correct else 0,
                    _utc_now_iso(),
                ),
            )

    def _mastery_maps(self, conn, chat_id: str | None):
        """Return (ep_stats, tp_stats, q_last_correct) for one learner.

        ep_stats / tp_stats: key → (attempts, corrects). q_last_correct:
        question_id → bool of the most recent attempt's correctness."""
        ep: dict[str, list[int]] = {}
        tp: dict[str, list[int]] = {}
        q_last: dict[str, bool] = {}
        clause = "WHERE chat_id = ?" if chat_id is not None else ""
        params = (str(chat_id),) if chat_id is not None else ()
        for r in conn.execute(
            "SELECT exam_point, tested_point, question_id, correct FROM quiz_attempts "
            f"{clause} ORDER BY attempt_id",
            params,
        ):
            c = int(r["correct"])
            epk = r["exam_point"] or ""
            slot = ep.setdefault(epk, [0, 0]); slot[0] += 1; slot[1] += c
            tpk = (r["tested_point"] or "").strip()
            if tpk:
                slot = tp.setdefault(tpk, [0, 0]); slot[0] += 1; slot[1] += c
            q_last[r["question_id"]] = bool(c)  # ordered → last write = newest
        return ep, tp, q_last

    def weighted_question(
        self,
        *,
        level: str | None = None,
        chat_id: str | None = None,
        exam_point: str | None = None,
        tested_point: str | None = None,
        wrong_only: bool = False,
        verified_only: bool = True,
        exclude_id: str | None = None,
        author: str | None = None,
        rng: random.Random | None = None,
    ) -> QuizQuestion | None:
        """Pick one question, biased toward the learner's weak points. Weight is
        driven by per-考点 (tested_point) accuracy when that point has history,
        else per-題型 (exam_point) accuracy; plus per-question freshness and
        spaced-repetition adjustments. Falls back to least-served when there is
        no history at all (cold start ≈ random_question). ``exam_point`` restricts
        the candidate pool to one 題型 (the type-menu path). ``wrong_only`` keeps
        only questions whose most-recent attempt by this learner was wrong (錯題本);
        returns None when there is no such question."""
        picker = rng or random
        clauses = ["verified = 1"] if verified_only else []
        params: list[object] = []
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        if exam_point:
            clauses.append("exam_point = ?")
            params.append(exam_point.strip())
        if tested_point:
            clauses.append("tested_point = ?")
            params.append(tested_point.strip())
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM quiz_questions{where}", tuple(params)
            ).fetchall()
            if not rows:
                return None
            ep_stats, tp_stats, q_last = self._mastery_maps(conn, chat_id)
        candidates = [_row_to_question(r) for r in rows]
        if wrong_only:
            candidates = [q for q in candidates if q_last.get(q.question_id) is False]
            if not candidates:
                return None
        weights = [
            _mastery_weight(q, ep_stats, tp_stats, q_last) for q in candidates
        ]
        if exclude_id and len(candidates) > 1:
            for i, q in enumerate(candidates):
                if q.question_id == exclude_id:
                    weights[i] = 0.0
        if sum(weights) <= 0:
            weights = [1.0] * len(candidates)
        return picker.choices(candidates, weights=weights, k=1)[0]

    def mastery_stats(self, *, chat_id: str | None = None) -> dict:
        """Aggregate this learner's history for /quiz stats. Returns per-題型 rows
        and the weakest specific 考点 (tested_point) rows."""
        with self.connect() as conn:
            ep_stats, tp_stats, _ = self._mastery_maps(conn, chat_id)
            total = conn.execute(
                "SELECT COUNT(*) n FROM quiz_attempts "
                + ("WHERE chat_id = ?" if chat_id is not None else ""),
                (str(chat_id),) if chat_id is not None else (),
            ).fetchone()["n"]

        def _rows(d):
            out = []
            for k, (a, c) in d.items():
                out.append({"key": k, "attempts": a, "corrects": c,
                            "accuracy": c / a if a else 0.0})
            return out

        by_type = sorted(_rows(ep_stats), key=lambda r: r["accuracy"])
        by_point = sorted(_rows(tp_stats), key=lambda r: (r["accuracy"], -r["attempts"]))
        return {"total": int(total), "by_type": by_type, "by_point": by_point}

    # ── Vocabulary cards ─────────────────────────────────────────────────────

    def get_vocab_card(
        self,
        *,
        vocab_id: str | None = None,
        headword: str | None = None,
        level: str | None = None,
        author: str = "codex",
    ) -> QuizVocabCard | None:
        if not vocab_id and not headword:
            raise ValueError("get_vocab_card requires vocab_id or headword")
        clauses = ["author = ?"]
        params: list[object] = [(author or "codex").strip()]
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        if vocab_id:
            clauses.append("vocab_id = ?")
            params.append(vocab_id.strip())
        else:
            clauses.append("headword = ?")
            params.append((headword or "").strip())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_vocab_cards WHERE " + " AND ".join(clauses) + " LIMIT 1",
                tuple(params),
            ).fetchone()
        return _row_to_vocab_card(row) if row else None

    def list_vocab_cards(
        self,
        *,
        level: str,
        chat_id: str | None = None,
        author: str = "codex",
        mode: str = "weak",
    ) -> list[QuizVocabCard]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM quiz_vocab_cards
                WHERE level = ? AND author = ?
                ORDER BY headword ASC
                """,
                ((level or "").strip(), (author or "codex").strip()),
            ).fetchall()
            tp_stats, tp_last = self._vocab_progress_maps(conn, chat_id)
        cards = [_row_to_vocab_card(r) for r in rows]
        mode = (mode or "weak").strip().lower()
        if mode == "wrong":
            cards = [c for c in cards if tp_last.get(c.headword) is False]
            cards.sort(key=lambda c: (tp_stats.get(c.headword, (0, 0))[1] / max(tp_stats.get(c.headword, (0, 0))[0], 1), -tp_stats.get(c.headword, (0, 0))[0], c.headword))
            return cards
        if mode == "all":
            return cards
        if mode == "random":
            if not cards:
                return []
            return [random.choice(cards)]
        cards.sort(
            key=lambda c: (
                _vocab_mastery_sort_key(c.headword, tp_stats, tp_last),
                c.headword,
            )
        )
        return cards

    def find_vocab_cards(
        self,
        *,
        level: str,
        query: str,
        author: str = "codex",
    ) -> list[QuizVocabCard]:
        q = f"%{(query or '').strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM quiz_vocab_cards
                WHERE level = ? AND author = ?
                  AND (headword LIKE ? OR reading_hiragana LIKE ? OR zh_gloss_short LIKE ?)
                ORDER BY headword ASC
                """,
                ((level or "").strip(), (author or "codex").strip(), q, q, q),
            ).fetchall()
        return [_row_to_vocab_card(r) for r in rows]

    def vocab_cards_for_source(
        self,
        *,
        level: str,
        source_name: str,
        author: str = "codex",
    ) -> list[QuizVocabCard]:
        q = f"%{(source_name or '').strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM quiz_vocab_cards
                WHERE level = ? AND author = ? AND source_name LIKE ?
                ORDER BY headword ASC
                """,
                ((level or "").strip(), (author or "codex").strip(), q),
            ).fetchall()
        return [_row_to_vocab_card(r) for r in rows]

    def _vocab_progress_maps(self, conn, chat_id: str | None):
        attempts: dict[str, list[int]] = {}
        last: dict[str, bool] = {}
        params: tuple[object, ...]
        if chat_id is not None:
            sql = (
                "SELECT tested_point, correct FROM quiz_attempts "
                "WHERE chat_id = ? AND tested_point IS NOT NULL ORDER BY attempt_id"
            )
            params = (str(chat_id),)
        else:
            sql = (
                "SELECT tested_point, correct FROM quiz_attempts "
                "WHERE tested_point IS NOT NULL ORDER BY attempt_id"
            )
            params = ()
        for row in conn.execute(sql, params):
            tp = (row["tested_point"] or "").strip()
            if not tp:
                continue
            slot = attempts.setdefault(tp, [0, 0])
            slot[0] += 1
            slot[1] += int(row["correct"])
            last[tp] = bool(int(row["correct"]))
        return attempts, last

    def confusion_pairs(
        self, *, chat_id: str | None = None, limit: int = 8
    ) -> list[dict]:
        """For the learner's wrong answers, which (正解選項 → 誤選選項) pairs recur —
        e.g. always picking 「にして」 when the answer is 「にあって」. Skips reading
        types (their options are full sentences, not a diagnosable confusion).
        Returns rows {exam_point, correct, chosen, count} sorted by count desc."""
        where = ["a.correct = 0"]
        params: list[object] = []
        if chat_id is not None:
            where.append("a.chat_id = ?")
            params.append(str(chat_id))
        sql = (
            "SELECT a.chosen_index AS ci, q.answer_index AS ai, "
            "q.options_json AS oj, q.exam_point AS ep FROM quiz_attempts a "
            "JOIN quiz_questions q ON q.question_id = a.question_id "
            "WHERE " + " AND ".join(where)
        )
        counts: dict[tuple, int] = {}
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        for r in rows:
            ep = (r["ep"] or "").strip()
            if is_reading_exam_point(ep):
                continue
            try:
                opts = json.loads(r["oj"] or "[]")
            except (TypeError, ValueError):
                continue
            ci, ai = int(r["ci"]), int(r["ai"])
            if not (0 <= ci < len(opts) and 0 <= ai < len(opts)) or ci == ai:
                continue
            key = (ep, str(opts[ai]).strip(), str(opts[ci]).strip())
            counts[key] = counts.get(key, 0) + 1
        out = [
            {"exam_point": ep, "correct": cor, "chosen": cho, "count": n}
            for (ep, cor, cho), n in counts.items()
        ]
        out.sort(key=lambda r: (-r["count"], r["exam_point"]))
        return out[: max(0, int(limit))]

    def delete_question(self, question_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM quiz_questions WHERE question_id = ?", (question_id,)
            )
            self._backfill_vocab_cards(conn)
        return cursor.rowcount > 0

    def missing_media_song_questions(self) -> list[tuple[str, str]]:
        """(question_id, source_name) for song-typed questions lacking a 音檔 URL —
        the backfill worklist. Reading-comprehension items built from 賞析 articles
        never carried a PV, so they show up here even though the song has one."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT question_id, source_name FROM quiz_questions "
                "WHERE source_type LIKE '%song%' "
                "AND (source_media_url IS NULL OR source_media_url = '')"
            ).fetchall()
        return [(r["question_id"], r["source_name"] or "") for r in rows]

    def set_media_url(self, question_id: str, url: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE quiz_questions SET source_media_url = ?, updated_at = ? "
                "WHERE question_id = ?",
                (url, _utc_now_iso(), question_id),
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


# Mastery-weighting knobs. Weight rises as accuracy falls, so weak points are
# served more often; an untried point gets an exploration weight so it still
# surfaces without dominating.
_W_MIN = 0.5
_W_MAX = 4.0
_W_EXPLORE = 2.5
_FRESH_BOOST = 1.5      # never answered by this learner → surface new material
_RIGHT_DECAY = 0.25     # last answer was correct → rarely repeat
_WRONG_BOOST = 1.3      # last answer was wrong → spaced repetition brings it back


def _accuracy_weight(attempts: int, corrects: int) -> float:
    if attempts <= 0:
        return _W_EXPLORE
    acc = (corrects + 1) / (attempts + 2)  # Laplace-smoothed: no 0%/100% from n=1
    return _W_MIN + (_W_MAX - _W_MIN) * (1.0 - acc)


def _mastery_weight(question, ep_stats, tp_stats, q_last_correct) -> float:
    """Per-question serving weight from one learner's history. Prefers the
    specific 考点 (tested_point) grain when that point has been seen, else the
    題型 (exam_point) grain; then applies question-level freshness / spaced
    repetition."""
    tp = (getattr(question, "tested_point", None) or "").strip()
    if tp and tp in tp_stats:
        a, c = tp_stats[tp]
    else:
        a, c = ep_stats.get(question.exam_point, (0, 0))
    w = _accuracy_weight(a, c)
    last = q_last_correct.get(question.question_id)
    if last is None:
        w *= _FRESH_BOOST
    elif last:
        w *= _RIGHT_DECAY
    else:
        w *= _WRONG_BOOST
    return max(w, 0.01)


def _vocab_mastery_sort_key(headword: str, tp_stats, tp_last) -> tuple[float, int, int]:
    attempts, corrects = tp_stats.get(headword, (0, 0))
    accuracy = corrects / attempts if attempts else 2.0
    last = tp_last.get(headword)
    last_rank = 0 if last is False else 1 if last is None else 2
    return (accuracy, -attempts, last_rank)


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
        source_excerpt_type=(
            row["source_excerpt_type"]
            if "source_excerpt_type" in row.keys()
            else infer_source_excerpt_type(
                source_text_url=row["source_text_url"],
                source_excerpt=row["source_excerpt"],
                source_name=row["source_name"],
            )
        ),
        tested_point=(row["tested_point"] if "tested_point" in row.keys() else None),
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


def _row_to_vocab_card(row: sqlite3.Row) -> QuizVocabCard:
    try:
        support_ids = json.loads(row["support_question_ids_json"] or "[]")
        if not isinstance(support_ids, list):
            support_ids = []
    except (TypeError, ValueError, json.JSONDecodeError):
        support_ids = []
    try:
        exam_points = json.loads(row["exam_points_json"] or "[]")
        if not isinstance(exam_points, list):
            exam_points = []
    except (TypeError, ValueError, json.JSONDecodeError):
        exam_points = []
    return QuizVocabCard(
        vocab_id=row["vocab_id"],
        level=row["level"],
        headword=row["headword"],
        reading_hiragana=row["reading_hiragana"],
        zh_gloss_short=row["zh_gloss_short"],
        example_ja=row["example_ja"],
        example_source_kind=row["example_source_kind"] or "adapted",
        source_name=row["source_name"] or "",
        source_text_url=row["source_text_url"],
        primary_question_id=row["primary_question_id"] or "",
        support_question_ids=tuple(str(x).strip() for x in support_ids if str(x).strip()),
        exam_points=tuple(str(x).strip() for x in exam_points if str(x).strip()),
        author=row["author"] or "codex",
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
