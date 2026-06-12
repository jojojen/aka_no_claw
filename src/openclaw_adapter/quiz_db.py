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
    # Article/commentary excerpts may ground vocabulary questions when the stem
    # quotes the real sentence verbatim. The grounding gate below still rejects
    # fabricated or merely themed-on-source stems.
    if kind == "title" and "漢字読み" not in ep:
        return True
    return False



def vocab_example_is_low_value(headword: str, example_ja: str | None) -> bool:
    """Reject generic examples that do not help remember real usage."""
    headword = (headword or "").strip()
    example = (example_ja or "").strip()
    if not headword or not example:
        return True
    quoted = f"「{headword}」"
    generic_examples = {
        f"{headword}という言葉を覚えた。",
        f"{quoted}という言葉を覚えた。",
        f"{quoted}という言葉が心に残った。",
        f"{headword}について考えた。",
        f"{quoted}について考えた。",
        f"{headword}の意味を調べた。",
        f"{quoted}の意味を調べた。",
        f"{headword}を調べた。",
        f"{quoted}を調べた。",
        f"{headword}を学んだ。",
        f"{quoted}を学んだ。",
    }
    return example in generic_examples


# A vocab card example must be a single readable line. Lyric excerpts that have
# no sentence delimiters to split on collapse into one giant blob; anything past
# this length is a blob, not an example, and is rejected.
_MAX_VOCAB_EXAMPLE_CHARS = 70
_MAX_VOCAB_EXAMPLE_SENTENCES = 3


def _clean_vocab_source_excerpt(excerpt: str) -> str:
    text = re.sub(r"\s+", " ", (excerpt or "").strip())
    text = re.sub(r"【文章[^\]]*?】", " ", text)
    text = re.sub(r"【[^】]*】", " ", text)
    text = re.sub(r"≪[^≫]*≫", " ", text)
    text = re.sub(r"-{3,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _count_vocab_example_sentences(text: str) -> int:
    return len([p for p in re.split(r"[。！？!?]+", text) if p.strip()])


def _extract_token_window_candidate(text: str, headword: str) -> str | None:
    if " " not in text:
        return None
    tokens = [t for t in text.split(" ") if t]
    hit_indexes = [i for i, token in enumerate(tokens) if headword in token]
    best = None
    for hit in hit_indexes:
        for left in range(hit, -1, -1):
            for right in range(hit, len(tokens)):
                cand = " ".join(tokens[left : right + 1]).strip()
                if headword not in cand:
                    continue
                if len(cand) > _MAX_VOCAB_EXAMPLE_CHARS:
                    break
                if len(_normalize_grounding(cand)) <= len(_normalize_grounding(headword)) + 2:
                    continue
                if best is None or len(cand) < len(best):
                    best = cand
    return best


def source_excerpt_vocab_example(
    *, headword: str, source_excerpt: str | None, source_excerpt_type: str | None
) -> str | None:
    """Pick a real source sentence containing the headword for a vocab card."""
    headword = (headword or "").strip()
    excerpt = _clean_vocab_source_excerpt(source_excerpt or "")
    if not headword or not excerpt or headword not in excerpt:
        return None
    if _normalize_source_excerpt_type(source_excerpt_type) == "title":
        # A title can ground a reading question, but it is not a memory-helpful
        # example sentence by itself.
        return None
    quoted_candidates = []
    for pat in (
        rf"「[^」]*{re.escape(headword)}[^」]*」",
        rf"『[^』]*{re.escape(headword)}[^』]*』",
        rf'"[^"]*{re.escape(headword)}[^"]*"',
    ):
        quoted_candidates.extend(m.group(0).strip() for m in re.finditer(pat, excerpt))
    pieces = [
        p.strip()
        for p in re.split(r"(?<=[。！？!?])|[\r\n]+", excerpt)
        if p.strip()
    ]
    candidates = quoted_candidates + [p for p in pieces if headword in p]
    if not candidates:
        candidates = [excerpt]
    window_candidates = []
    for cand in list(candidates):
        if len(cand) <= _MAX_VOCAB_EXAMPLE_CHARS:
            continue
        token_window = _extract_token_window_candidate(cand, headword)
        if token_window:
            window_candidates.append(token_window)
    candidates.extend(window_candidates)
    candidates = [
        p for p in candidates
        if not p.endswith(("「", "『", "（", "(", "、", ","))
    ]
    usable = [
        p for p in candidates
        if (
            headword in p
            and len(_normalize_grounding(p)) > len(_normalize_grounding(headword)) + 2
            and _count_vocab_example_sentences(p) <= _MAX_VOCAB_EXAMPLE_SENTENCES
        )
    ]
    if not usable:
        return None
    best = min(usable, key=len)
    if (
        " " not in best
        and not re.search(r"[、。！？!?「」『』（）()…]", best)
        and len(best) > len(headword) + 8
    ):
        return None
    if len(best) > _MAX_VOCAB_EXAMPLE_CHARS:
        return None
    return best


def _normalize_vocab_example_identity(example: str | None) -> str:
    return _normalize_grounding(example or "")


def _grammar_card_example(row: sqlite3.Row) -> str:
    excerpt = _clean_vocab_source_excerpt(row["source_excerpt"] or "")
    if excerpt:
        return excerpt
    stem = re.sub(r"\s+", " ", (row["stem"] or "").strip())
    return stem


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


def synonym_answer_restates_headword(
    *, headword: str, reading: str | None, option: str
) -> bool:
    """True iff a 言い換え類義 correct option just RESTATES the asked word — so the
    item is solvable by spotting the word inside its own 'synonym'. The structural
    ``answer_leaks_into_stem`` gate misses this because the leak is in the OPTION,
    not the stem (e.g. 【建前】→「表向きの方針やたてまえ」, 【喚く】→「大声でわめき叫ぶ」,
    【塊】→「一つにかたまったもの」).

    Deliberately CONSERVATIVE — only fires on a whole-word restatement, never on an
    incidental single shared kanji, so a genuine paraphrase that reuses one
    character (【転移】→「他の場所へ移り広がること」 reuses 移) is NOT flagged:
      * the contiguous headword kanji string appears verbatim in the option, OR
      * the headword's reading (minus trailing okurigana for inflected words)
        appears as a kana run inside the option.
    """
    hw = (headword or "").strip()
    if hw and len(hw) >= 2 and hw in option:
        return True
    r = _to_hiragana(reading)
    if len(r) < 2:
        return False
    stem = r[:-1] if len(r) > 2 else r          # drop okurigana on verbs/adjs
    if len(stem) < 2:
        return False
    return stem in _to_hiragana(option)


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


def _contains_kana(text: str | None) -> bool:
    """True if the text holds any hiragana/katakana. A Chinese gloss must be
    Han-only; any kana means a Japanese paraphrase leaked into the 中文 field."""
    for ch in text or "":
        code = ord(ch)
        if 0x3041 <= code <= 0x3096 or 0x30A1 <= code <= 0x30F6:
            return True
    return False


def _to_hiragana(text: str | None) -> str:
    """Keep only kana, folding katakana → hiragana so '読み' comparisons are
    script-insensitive. Everything else (kanji, latin, punctuation, the prolonged
    mark) is dropped. Used by the 漢字読み distractor audit below."""
    out: list[str] = []
    for ch in text or "":
        code = ord(ch)
        if 0x3041 <= code <= 0x3096:           # hiragana
            out.append(ch)
        elif 0x30A1 <= code <= 0x30F6:          # katakana → hiragana
            out.append(chr(code - 0x60))
    return "".join(out)


def audit_kanji_reading_distractors(
    *,
    options: tuple[str, ...],
    answer_index: int,
    max_reading_len: int = 9,
) -> list[tuple[int, str, str]]:
    """ADDITIVE, ADVISORY cheap filter for 漢字読み distractors. Returns a list of
    ``(index, option, reason)`` for distractors that are almost certainly wrong
    readings of an *unrelated* word rather than a plausible misreading of the
    target — the systematic codex failure mode (e.g. 【創造】そうぞう with distractors
    けいばつ / やきめ that share no sound with the answer).

    Two deterministic red flags:
      * the distractor shares ZERO kana with the correct reading, or
      * the distractor is longer than ``max_reading_len`` (a phrase reading, not a
        single-word reading).

    This is a *filter to catch obvious garbage cheaply*, NOT an oracle: a distractor
    that shares one incidental kana with the answer still passes here. Distractor
    plausibility in the subtle band stays the author model's job. An empty list
    means "no obvious garbage", not "distractors are good".
    """
    if not (0 <= answer_index < len(options)):
        return []
    correct_kana = set(_to_hiragana(options[answer_index]))
    suspects: list[tuple[int, str, str]] = []
    for i, opt in enumerate(options):
        if i == answer_index:
            continue
        opt_kana = _to_hiragana(opt)
        if len(opt_kana) > max_reading_len:
            suspects.append((i, opt, f"reading too long ({len(opt_kana)} kana) — phrase, not a word"))
        elif correct_kana and opt_kana and not (correct_kana & set(opt_kana)):
            suspects.append((i, opt, f"shares no kana with correct reading {options[answer_index]!r}"))
    return suspects


def question_similarity(a_stem: str, b_stem: str) -> float:
    """Normalized longest-common-substring ratio over the shorter noise-stripped
    stem, in [0, 1]. Cheap, deterministic, no LLM. Two near-identical stems
    (same cloze line, trivial wording change) score near 1.0."""
    a = _normalize_grounding(a_stem)
    b = _normalize_grounding(b_stem)
    if not a or not b:
        return 0.0
    shortest = min(len(a), len(b))
    if shortest == 0:
        return 0.0
    return _longest_common_substring_len(a, b) / shortest


def questions_are_near_duplicate(
    *,
    a_stem: str,
    b_stem: str,
    a_tested_point: str | None = None,
    b_tested_point: str | None = None,
    threshold: float = 0.85,
) -> bool:
    """True iff two questions are near-duplicates. A shared tested_point lowers the
    bar (same word, near-same stem = dup); otherwise pure stem similarity must clear
    ``threshold``. Deterministic — runs at final-output dedup, never an LLM step."""
    same_point = bool(
        a_tested_point and b_tested_point
        and _normalize_grounding(a_tested_point) == _normalize_grounding(b_tested_point)
    )
    sim = question_similarity(a_stem, b_stem)
    if same_point and sim >= threshold - 0.15:
        return True
    return sim >= threshold


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
    tested_jlpt_level TEXT,
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
    source_media_url          TEXT,
    primary_question_id       TEXT NOT NULL,
    support_question_ids_json TEXT NOT NULL DEFAULT '[]',
    exam_points_json          TEXT NOT NULL DEFAULT '[]',
    tested_jlpt_level         TEXT,
    author                    TEXT NOT NULL DEFAULT 'codex',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vocab_level_author ON quiz_vocab_cards(level, author);
CREATE INDEX IF NOT EXISTS idx_vocab_headword ON quiz_vocab_cards(headword);
CREATE INDEX IF NOT EXISTS idx_vocab_source_name ON quiz_vocab_cards(source_name);

CREATE TABLE IF NOT EXISTS quiz_grammar_cards (
    card_id                   TEXT PRIMARY KEY,
    level                     TEXT NOT NULL,
    headword                  TEXT NOT NULL,
    explanation_zh            TEXT NOT NULL,
    example_ja                TEXT NOT NULL,
    example_source_kind       TEXT NOT NULL DEFAULT 'source_excerpt',
    source_name               TEXT NOT NULL DEFAULT '',
    source_text_url           TEXT,
    source_media_url          TEXT,
    primary_question_id       TEXT NOT NULL,
    support_question_ids_json TEXT NOT NULL DEFAULT '[]',
    exam_points_json          TEXT NOT NULL DEFAULT '[]',
    tested_jlpt_level         TEXT,
    author                    TEXT NOT NULL DEFAULT 'codex',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grammar_level_author ON quiz_grammar_cards(level, author);
CREATE INDEX IF NOT EXISTS idx_grammar_headword ON quiz_grammar_cards(headword);
CREATE INDEX IF NOT EXISTS idx_grammar_source_name ON quiz_grammar_cards(source_name);

-- Song corpus the quiz system pulls lyrics/vocab from. `favorite` (1/0) separates
-- songs the user explicitly hearted (1) from songs ingested purely as quiz
-- material (0). The user-facing 最愛 views must filter favorite = 1.
CREATE TABLE IF NOT EXISTS quiz_songs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,
    artist            TEXT NOT NULL DEFAULT '',
    youtube_url       TEXT NOT NULL,
    youtube_short_url TEXT NOT NULL UNIQUE,
    lyrics_url        TEXT,
    youtube_title_raw TEXT,
    video_id          TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    favorite          INTEGER NOT NULL DEFAULT 1,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quiz_songs_status ON quiz_songs(status);
CREATE INDEX IF NOT EXISTS idx_quiz_songs_title ON quiz_songs(title);
CREATE INDEX IF NOT EXISTS idx_quiz_songs_favorite ON quiz_songs(favorite);

CREATE TABLE IF NOT EXISTS lyrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id    INTEGER NOT NULL UNIQUE,
    full_text  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(song_id) REFERENCES quiz_songs(id)
);

CREATE TABLE IF NOT EXISTS sentences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id        INTEGER NOT NULL,
    sentence_text  TEXT NOT NULL,
    sentence_index INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    FOREIGN KEY(song_id) REFERENCES quiz_songs(id)
);

CREATE INDEX IF NOT EXISTS idx_sentences_song_idx ON sentences(song_id, sentence_index);

CREATE TABLE IF NOT EXISTS vocabulary_tokens (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id              INTEGER NOT NULL,
    sentence_id          INTEGER NOT NULL,
    surface              TEXT NOT NULL,
    dictionary_form      TEXT NOT NULL,
    reading              TEXT NOT NULL DEFAULT '',
    pos                  TEXT NOT NULL DEFAULT '',
    jlpt_level           TEXT,
    used_quiz_count      INTEGER NOT NULL DEFAULT 0,
    used_flashcard_count INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    FOREIGN KEY(song_id) REFERENCES quiz_songs(id),
    FOREIGN KEY(sentence_id) REFERENCES sentences(id)
);

CREATE INDEX IF NOT EXISTS idx_vocab_tokens_song ON vocabulary_tokens(song_id);
CREATE INDEX IF NOT EXISTS idx_vocab_tokens_jlpt ON vocabulary_tokens(jlpt_level, used_quiz_count);
CREATE INDEX IF NOT EXISTS idx_vocab_tokens_dict_form ON vocabulary_tokens(dictionary_form);

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

CREATE TABLE IF NOT EXISTS vocab_seed (
    headword         TEXT PRIMARY KEY,
    reading_hiragana TEXT NOT NULL,
    zh_gloss_short   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_settings (
    chat_id    TEXT PRIMARY KEY,
    speed      REAL NOT NULL DEFAULT 1.0,
    pitch      REAL NOT NULL DEFAULT 0.0,
    intonation REAL NOT NULL DEFAULT 1.0,
    tempo      REAL NOT NULL DEFAULT 1.0,
    volume     REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL
);
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
GRAMMAR_CARD_EXAM_POINTS: tuple[str, ...] = ("文法形式の判断", "文章の文法", "文の組み立て")
_GRAMMAR_PRIMARY_PRIORITY: dict[str, int] = {
    "文章の文法": 0,
    "文法形式の判断": 1,
    "文の組み立て": 2,
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


def build_grammar_card_id(*, level: str, headword: str) -> str:
    return sha1(f"grammar|{level}|{headword}".encode("utf-8")).hexdigest()


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
    tested_jlpt_level: str | None = None
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
    source_media_url: str | None = None
    primary_question_id: str = ""
    support_question_ids: tuple[str, ...] = ()
    exam_points: tuple[str, ...] = ()
    tested_jlpt_level: str | None = None
    author: str = "codex"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class QuizGrammarCard:
    card_id: str
    level: str
    headword: str
    explanation_zh: str
    example_ja: str
    example_source_kind: str = "source_excerpt"
    source_name: str = ""
    source_text_url: str | None = None
    source_media_url: str | None = None
    primary_question_id: str = ""
    support_question_ids: tuple[str, ...] = ()
    exam_points: tuple[str, ...] = ()
    tested_jlpt_level: str | None = None
    author: str = "codex"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class FavoriteSongToken:
    token_id: int
    song_id: int
    sentence_id: int
    song_title: str
    song_artist: str
    youtube_short_url: str
    lyrics_url: str | None
    sentence_text: str
    surface: str
    dictionary_form: str
    reading: str
    pos: str
    jlpt_level: str | None
    used_quiz_count: int = 0
    used_flashcard_count: int = 0


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
            # Must run BEFORE _SCHEMA: otherwise CREATE TABLE IF NOT EXISTS quiz_songs
            # would make a fresh empty table beside the real data still in favorite_songs.
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "favorite_songs" in tables and "quiz_songs" not in tables:
                conn.execute("ALTER TABLE favorite_songs RENAME TO quiz_songs")
                conn.execute("DROP INDEX IF EXISTS idx_favorite_songs_status")
                conn.execute("DROP INDEX IF EXISTS idx_favorite_songs_title")
                tables.add("quiz_songs")
            # `favorite` must exist before _SCHEMA, which builds an index on it.
            # Pre-existing rows are genuine user favorites → default 1.
            if "quiz_songs" in tables:
                song_cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_songs)")}
                if "favorite" not in song_cols:
                    conn.execute(
                        "ALTER TABLE quiz_songs ADD COLUMN favorite INTEGER NOT NULL DEFAULT 1"
                    )
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
            if "tested_jlpt_level" not in cols:
                conn.execute("ALTER TABLE quiz_questions ADD COLUMN tested_jlpt_level TEXT")
            vocab_cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_vocab_cards)")}
            if "source_media_url" not in vocab_cols:
                conn.execute("ALTER TABLE quiz_vocab_cards ADD COLUMN source_media_url TEXT")
            if "tested_jlpt_level" not in vocab_cols:
                conn.execute("ALTER TABLE quiz_vocab_cards ADD COLUMN tested_jlpt_level TEXT")
            # "other" is only a fallback bucket, not a trustworthy explicit value.
            # Re-infer it on every startup so previously migrated rows converge.
            self._backfill_source_excerpt_types(conn, overwrite_other=True)
            self._backfill_vocab_cards(conn)
            self._backfill_grammar_cards(conn)

    def upsert_vocab_seed(
        self, headword: str, reading_hiragana: str, zh_gloss_short: str
    ) -> None:
        if _contains_kana(zh_gloss_short):
            raise ValueError(
                f"zh_gloss_short must be Chinese (Han-only), not Japanese: "
                f"headword={headword!r} gloss={zh_gloss_short!r}"
            )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO vocab_seed (headword, reading_hiragana, zh_gloss_short)
                   VALUES (?, ?, ?)
                   ON CONFLICT(headword) DO UPDATE SET
                       reading_hiragana = excluded.reading_hiragana,
                       zh_gloss_short = excluded.zh_gloss_short""",
                (headword, reading_hiragana, zh_gloss_short),
            )
            self._backfill_vocab_cards(conn)
            self._backfill_grammar_cards(conn)

    def get_voice_params(self, chat_id: str) -> "VoiceParams":
        from .quiz_vocab_audio import VoiceParams

        with self.connect() as conn:
            row = conn.execute(
                "SELECT speed, pitch, intonation, tempo, volume "
                "FROM voice_settings WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        if row is None:
            return VoiceParams()
        return VoiceParams(
            speed=row["speed"],
            pitch=row["pitch"],
            intonation=row["intonation"],
            tempo=row["tempo"],
            volume=row["volume"],
        )

    def set_voice_params(self, chat_id: str, params: "VoiceParams") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO voice_settings
                       (chat_id, speed, pitch, intonation, tempo, volume, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       speed = excluded.speed,
                       pitch = excluded.pitch,
                       intonation = excluded.intonation,
                       tempo = excluded.tempo,
                       volume = excluded.volume,
                       updated_at = excluded.updated_at""",
                (
                    str(chat_id),
                    params.speed,
                    params.pitch,
                    params.intonation,
                    params.tempo,
                    params.volume,
                    _utc_now_iso(),
                ),
            )

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
              AND author IN ('codex', 'Claude')
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
        used_example_identities: set[str] = set()
        now = _utc_now_iso()
        for headword, group in groups.items():
            vocab_id = build_vocab_card_id(level="JLPT N1", headword=headword)
            existing = conn.execute(
                "SELECT * FROM quiz_vocab_cards WHERE vocab_id = ?",
                (vocab_id,),
            ).fetchone()
            seed_row = conn.execute(
                "SELECT reading_hiragana, zh_gloss_short FROM vocab_seed WHERE headword = ?",
                (headword,),
            ).fetchone()
            if seed_row:
                seed = {"reading_hiragana": seed_row["reading_hiragana"], "zh_gloss_short": seed_row["zh_gloss_short"]}
            elif existing is not None:
                seed = {"reading_hiragana": existing["reading_hiragana"], "zh_gloss_short": existing["zh_gloss_short"]}
            else:
                continue
            group.sort(
                key=lambda r: (
                    _VOCAB_PRIMARY_PRIORITY.get((r["exam_point"] or "").strip(), 99),
                    r["created_at"] or "",
                    r["question_id"] or "",
                )
            )
            # The primary is the first priority-ordered question that actually
            # yields a usable example; questions whose excerpt is an article/
            # title (no card example) must not sink the whole card. This keeps
            # the card's author badge consistent with the example shown.
            primary = None
            example_ja = None
            for cand in group:
                ex = source_excerpt_vocab_example(
                    headword=headword,
                    source_excerpt=cand["source_excerpt"],
                    source_excerpt_type=cand["source_excerpt_type"],
                )
                if ex:
                    primary = cand
                    example_ja = ex
                    break
            example_source_kind = "source_excerpt"
            if not example_ja:
                # The length cap blocks NEW long-example cards but must not
                # retroactively delete a card that already shipped: keep any
                # existing card untouched until its source question is fixed.
                # The cap therefore applies to future cards, not old ones.
                if existing is not None:
                    seen_ids.add(vocab_id)
                continue
            example_identity = _normalize_vocab_example_identity(example_ja)
            if not example_identity or example_identity in used_example_identities:
                continue
            used_example_identities.add(example_identity)
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
            seen_ids.add(vocab_id)
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO quiz_vocab_cards (
                    vocab_id, level, headword, reading_hiragana, zh_gloss_short,
                    example_ja, example_source_kind, source_name, source_text_url,
                    source_media_url,
                    primary_question_id, support_question_ids_json, exam_points_json,
                    tested_jlpt_level, author, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vocab_id) DO UPDATE SET
                    reading_hiragana = excluded.reading_hiragana,
                    zh_gloss_short = excluded.zh_gloss_short,
                    example_ja = excluded.example_ja,
                    example_source_kind = excluded.example_source_kind,
                    source_name = excluded.source_name,
                    source_text_url = excluded.source_text_url,
                    source_media_url = excluded.source_media_url,
                    primary_question_id = excluded.primary_question_id,
                    support_question_ids_json = excluded.support_question_ids_json,
                    exam_points_json = excluded.exam_points_json,
                    tested_jlpt_level = excluded.tested_jlpt_level,
                    author = excluded.author,
                    updated_at = excluded.updated_at
                """,
                (
                    vocab_id,
                    "JLPT N1",
                    headword,
                    seed["reading_hiragana"],
                    seed["zh_gloss_short"],
                    example_ja,
                    example_source_kind,
                    (primary["source_name"] or "").strip(),
                    primary["source_text_url"],
                    primary["source_media_url"],
                    (primary["question_id"] or "").strip(),
                    json.dumps(list(support_ids), ensure_ascii=False),
                    json.dumps(list(exam_points), ensure_ascii=False),
                    (
                        primary["tested_jlpt_level"]
                        if "tested_jlpt_level" in primary.keys()
                        else None
                    ),
                    (primary["author"] or "codex").strip(),
                    created_at,
                    now,
                ),
            )
        if seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            conn.execute(
                f"DELETE FROM quiz_vocab_cards WHERE author IN ('codex', 'Claude') AND level = 'JLPT N1' AND vocab_id NOT IN ({placeholders})",
                tuple(seen_ids),
            )
        else:
            conn.execute(
                "DELETE FROM quiz_vocab_cards WHERE author IN ('codex', 'Claude') AND level = 'JLPT N1'"
            )

    def _backfill_grammar_cards(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT * FROM quiz_questions
            WHERE verified = 1
              AND author IN ('codex', 'Claude')
              AND level = 'JLPT N1'
              AND tested_point IS NOT NULL
              AND TRIM(tested_point) <> ''
              AND exam_point IN (?, ?, ?)
            ORDER BY created_at ASC, question_id ASC
            """,
            GRAMMAR_CARD_EXAM_POINTS,
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["tested_point"] or "").strip(), []).append(row)

        seen_ids: set[str] = set()
        now = _utc_now_iso()
        for headword, group in groups.items():
            card_id = build_grammar_card_id(level="JLPT N1", headword=headword)
            existing = conn.execute(
                "SELECT * FROM quiz_grammar_cards WHERE card_id = ?",
                (card_id,),
            ).fetchone()
            group.sort(
                key=lambda r: (
                    _GRAMMAR_PRIMARY_PRIORITY.get((r["exam_point"] or "").strip(), 99),
                    r["created_at"] or "",
                    r["question_id"] or "",
                )
            )
            primary = next(
                (
                    cand for cand in group
                    if ((cand["source_excerpt"] or "").strip() or (cand["stem"] or "").strip())
                    and (cand["explanation"] or "").strip()
                ),
                group[0],
            )
            example_ja = _grammar_card_example(primary)
            explanation_zh = (primary["explanation"] or "").strip()
            if not example_ja or not explanation_zh:
                if existing is not None:
                    seen_ids.add(card_id)
                continue
            support_ids = tuple(
                dict.fromkeys(
                    (r["question_id"] or "").strip()
                    for r in group
                    if (r["question_id"] or "").strip()
                )
            )
            exam_points = tuple(
                ep for ep, _ in sorted(
                    {
                        (
                            (r["exam_point"] or "").strip(),
                            _GRAMMAR_PRIMARY_PRIORITY.get((r["exam_point"] or "").strip(), 99),
                        )
                        for r in group
                        if (r["exam_point"] or "").strip()
                    },
                    key=lambda pair: (pair[1], pair[0]),
                )
            )
            seen_ids.add(card_id)
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO quiz_grammar_cards (
                    card_id, level, headword, explanation_zh, example_ja,
                    example_source_kind, source_name, source_text_url, source_media_url,
                    primary_question_id, support_question_ids_json, exam_points_json,
                    tested_jlpt_level, author, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    explanation_zh = excluded.explanation_zh,
                    example_ja = excluded.example_ja,
                    example_source_kind = excluded.example_source_kind,
                    source_name = excluded.source_name,
                    source_text_url = excluded.source_text_url,
                    source_media_url = excluded.source_media_url,
                    primary_question_id = excluded.primary_question_id,
                    support_question_ids_json = excluded.support_question_ids_json,
                    exam_points_json = excluded.exam_points_json,
                    tested_jlpt_level = excluded.tested_jlpt_level,
                    author = excluded.author,
                    updated_at = excluded.updated_at
                """,
                (
                    card_id,
                    "JLPT N1",
                    headword,
                    explanation_zh,
                    example_ja,
                    "source_excerpt" if (primary["source_excerpt"] or "").strip() else "stem",
                    (primary["source_name"] or "").strip(),
                    primary["source_text_url"],
                    primary["source_media_url"],
                    (primary["question_id"] or "").strip(),
                    json.dumps(list(support_ids), ensure_ascii=False),
                    json.dumps(list(exam_points), ensure_ascii=False),
                    (
                        primary["tested_jlpt_level"]
                        if "tested_jlpt_level" in primary.keys()
                        else None
                    ),
                    (primary["author"] or "codex").strip(),
                    created_at,
                    now,
                ),
            )
        if seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            conn.execute(
                f"DELETE FROM quiz_grammar_cards WHERE author IN ('codex', 'Claude') AND level = 'JLPT N1' AND card_id NOT IN ({placeholders})",
                tuple(seen_ids),
            )
        else:
            conn.execute(
                "DELETE FROM quiz_grammar_cards WHERE author IN ('codex', 'Claude') AND level = 'JLPT N1'"
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
        tested_jlpt_level: str | None = None,
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
                "may use article/commentary text only when the quoted source text is "
                "verbatim grounded, and title-only grounding is limited to 漢字読み"
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
                    tested_point, tested_jlpt_level, verified,
                    served_count, author, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
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
                    tested_jlpt_level = excluded.tested_jlpt_level,
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
                    (tested_jlpt_level or "").strip() or None,
                    1 if verified else 0,
                    (author or "Claude").strip(), created_at, now,
                ),
            )
            self._backfill_vocab_cards(conn)
            self._backfill_grammar_cards(conn)
        loaded = self.get_question(question_id)
        assert loaded is not None
        return loaded

    def get_question(self, question_id: str) -> QuizQuestion | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_questions WHERE question_id = ?", (question_id,)
            ).fetchone()
        return _row_to_question(row) if row else None

    def find_duplicate_questions(
        self,
        *,
        stem: str,
        tested_point: str | None = None,
        exam_point: str | None = None,
        source_name: str | None = None,
        threshold: float = 0.85,
        limit: int = 5,
    ) -> list[QuizQuestion]:
        """Return existing questions that are near-duplicates of a candidate, using
        the deterministic ``questions_are_near_duplicate`` helper (no LLM). Scope the
        comparison set by exam_point and/or source_name to stay cheap — duplicates
        only ever arise within the same type+source anyway. Call this ONCE at the
        final-output stage, not on every authoring step."""
        clauses: list[str] = []
        params: list[object] = []
        if exam_point:
            clauses.append("exam_point = ?")
            params.append(exam_point.strip())
        if source_name:
            clauses.append("source_name = ?")
            params.append(source_name.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM quiz_questions{where}", tuple(params)
            ).fetchall()
        matches: list[QuizQuestion] = []
        for row in rows:
            if questions_are_near_duplicate(
                a_stem=stem,
                b_stem=row["stem"],
                a_tested_point=tested_point,
                b_tested_point=row["tested_point"],
                threshold=threshold,
            ):
                matches.append(_row_to_question(row))
                if len(matches) >= limit:
                    break
        return matches

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
        author: str | None = None,
    ) -> QuizVocabCard | None:
        if not vocab_id and not headword:
            raise ValueError("get_vocab_card requires vocab_id or headword")
        clauses: list[str] = []
        params: list[object] = []
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
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
        author: str | None = None,
        mode: str = "weak",
    ) -> list[QuizVocabCard]:
        clauses = ["level = ?"]
        params: list[object] = [(level or "").strip()]
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_vocab_cards WHERE "
                + " AND ".join(clauses)
                + " ORDER BY headword ASC",
                tuple(params),
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
        if mode == "recent":
            cards.sort(key=lambda c: (c.created_at or "", c.vocab_id), reverse=True)
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
        author: str | None = None,
    ) -> list[QuizVocabCard]:
        q = f"%{(query or '').strip()}%"
        clauses = ["level = ?"]
        params: list[object] = [(level or "").strip()]
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        clauses.append("(headword LIKE ? OR reading_hiragana LIKE ? OR zh_gloss_short LIKE ?)")
        params.extend([q, q, q])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_vocab_cards WHERE "
                + " AND ".join(clauses)
                + " ORDER BY headword ASC",
                tuple(params),
            ).fetchall()
        return [_row_to_vocab_card(r) for r in rows]

    def vocab_cards_for_source(
        self,
        *,
        level: str,
        source_name: str,
        author: str | None = None,
    ) -> list[QuizVocabCard]:
        q = f"%{(source_name or '').strip()}%"
        clauses = ["level = ?"]
        params: list[object] = [(level or "").strip()]
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        clauses.append("source_name LIKE ?")
        params.append(q)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_vocab_cards WHERE "
                + " AND ".join(clauses)
                + " ORDER BY headword ASC",
                tuple(params),
            ).fetchall()
        return [_row_to_vocab_card(r) for r in rows]

    def get_grammar_card(
        self,
        *,
        card_id: str | None = None,
        headword: str | None = None,
        level: str | None = None,
        author: str | None = None,
    ) -> QuizGrammarCard | None:
        if not card_id and not headword:
            raise ValueError("get_grammar_card requires card_id or headword")
        clauses: list[str] = []
        params: list[object] = []
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        if level:
            clauses.append("level = ?")
            params.append(level.strip())
        if card_id:
            clauses.append("card_id = ?")
            params.append(card_id.strip())
        else:
            clauses.append("headword = ?")
            params.append((headword or "").strip())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quiz_grammar_cards WHERE " + " AND ".join(clauses) + " LIMIT 1",
                tuple(params),
            ).fetchone()
        return _row_to_grammar_card(row) if row else None

    def list_grammar_cards(
        self,
        *,
        level: str,
        chat_id: str | None = None,
        author: str | None = None,
        mode: str = "weak",
    ) -> list[QuizGrammarCard]:
        clauses = ["level = ?"]
        params: list[object] = [(level or "").strip()]
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_grammar_cards WHERE "
                + " AND ".join(clauses)
                + " ORDER BY headword ASC",
                tuple(params),
            ).fetchall()
            tp_stats, tp_last = self._vocab_progress_maps(conn, chat_id)
        cards = [_row_to_grammar_card(r) for r in rows]
        mode = (mode or "weak").strip().lower()
        if mode == "wrong":
            cards = [c for c in cards if tp_last.get(c.headword) is False]
            cards.sort(
                key=lambda c: (
                    tp_stats.get(c.headword, (0, 0))[1] / max(tp_stats.get(c.headword, (0, 0))[0], 1),
                    -tp_stats.get(c.headword, (0, 0))[0],
                    c.headword,
                )
            )
            return cards
        if mode == "all":
            return cards
        if mode == "recent":
            cards.sort(key=lambda c: (c.created_at or "", c.card_id), reverse=True)
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

    def find_grammar_cards(
        self,
        *,
        level: str,
        query: str,
        author: str | None = None,
    ) -> list[QuizGrammarCard]:
        q = f"%{(query or '').strip()}%"
        clauses = ["level = ?"]
        params: list[object] = [(level or "").strip()]
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        clauses.append("(headword LIKE ? OR explanation_zh LIKE ?)")
        params.extend([q, q])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_grammar_cards WHERE "
                + " AND ".join(clauses)
                + " ORDER BY headword ASC",
                tuple(params),
            ).fetchall()
        return [_row_to_grammar_card(r) for r in rows]

    def grammar_cards_for_source(
        self,
        *,
        level: str,
        source_name: str,
        author: str | None = None,
    ) -> list[QuizGrammarCard]:
        q = f"%{(source_name or '').strip()}%"
        clauses = ["level = ?"]
        params: list[object] = [(level or "").strip()]
        if author:
            clauses.append("author = ?")
            params.append(author.strip())
        clauses.append("source_name LIKE ?")
        params.append(q)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quiz_grammar_cards WHERE "
                + " AND ".join(clauses)
                + " ORDER BY headword ASC",
                tuple(params),
            ).fetchall()
        return [_row_to_grammar_card(r) for r in rows]

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
            self._backfill_grammar_cards(conn)
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

    # ── Favorite songs / pre-analyzed lyrics ─────────────────────────────────

    def upsert_favorite_song(
        self,
        *,
        title: str,
        artist: str,
        youtube_url: str,
        youtube_short_url: str,
        status: str,
        youtube_title_raw: str | None = None,
        video_id: str | None = None,
        favorite: bool = True,
    ) -> int:
        now = _utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM quiz_songs WHERE youtube_short_url = ?",
                ((youtube_short_url or "").strip(),),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO quiz_songs (
                    title, artist, youtube_url, youtube_short_url, lyrics_url,
                    youtube_title_raw, video_id, status, favorite, last_error,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(youtube_short_url) DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    youtube_url = excluded.youtube_url,
                    youtube_title_raw = excluded.youtube_title_raw,
                    video_id = excluded.video_id,
                    status = excluded.status,
                    -- favorite is sticky: once hearted, a later quiz_source
                    -- re-ingest must not silently un-favorite it.
                    favorite = MAX(quiz_songs.favorite, excluded.favorite),
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    (title or "").strip(),
                    (artist or "").strip(),
                    (youtube_url or "").strip(),
                    (youtube_short_url or "").strip(),
                    (youtube_title_raw or "").strip() or None,
                    (video_id or "").strip() or None,
                    (status or "pending").strip(),
                    1 if favorite else 0,
                    created_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM quiz_songs WHERE youtube_short_url = ?",
                ((youtube_short_url or "").strip(),),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def get_favorite_song_by_youtube_short_url(self, youtube_short_url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM quiz_songs WHERE youtube_short_url = ? LIMIT 1",
                ((youtube_short_url or "").strip(),),
            ).fetchone()

    def update_favorite_song_artist(self, *, song_id: int, artist: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE quiz_songs SET artist = ?, updated_at = ? WHERE id = ?",
                ((artist or "").strip(), _utc_now_iso(), int(song_id)),
            )

    def mark_favorite_song_status(
        self,
        *,
        song_id: int,
        status: str,
        lyrics_url: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE quiz_songs
                SET status = ?, lyrics_url = COALESCE(?, lyrics_url), last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    (status or "pending").strip(),
                    (lyrics_url or "").strip() or None,
                    (last_error or "").strip() or None,
                    _utc_now_iso(),
                    int(song_id),
                ),
            )

    def replace_favorite_song_analysis(
        self,
        *,
        song_id: int,
        title: str | None = None,
        artist: str | None = None,
        lyrics_url: str,
        lyrics_text: str,
        sentences: list[str],
        tokens,
        status: str = "ready",
    ) -> None:
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE quiz_songs
                SET
                    title = COALESCE(NULLIF(?, ''), title),
                    artist = COALESCE(NULLIF(?, ''), artist),
                    lyrics_url = ?,
                    status = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    (title or "").strip(),
                    (artist or "").strip(),
                    (lyrics_url or "").strip(),
                    (status or "ready").strip(),
                    now,
                    int(song_id),
                ),
            )
            existing_lyrics = conn.execute(
                "SELECT created_at FROM lyrics WHERE song_id = ?",
                (int(song_id),),
            ).fetchone()
            lyrics_created_at = existing_lyrics["created_at"] if existing_lyrics else now
            conn.execute("DELETE FROM vocabulary_tokens WHERE song_id = ?", (int(song_id),))
            conn.execute("DELETE FROM sentences WHERE song_id = ?", (int(song_id),))
            conn.execute(
                """
                INSERT INTO lyrics (song_id, full_text, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(song_id) DO UPDATE SET
                    full_text = excluded.full_text,
                    updated_at = excluded.updated_at
                """,
                (int(song_id), (lyrics_text or "").strip(), lyrics_created_at, now),
            )
            sentence_ids: list[int] = []
            for idx, sentence in enumerate(sentences):
                cursor = conn.execute(
                    """
                    INSERT INTO sentences (song_id, sentence_text, sentence_index, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (int(song_id), (sentence or "").strip(), int(idx), now),
                )
                sentence_ids.append(int(cursor.lastrowid))
            for token in tokens:
                if not (0 <= int(token.sentence_index) < len(sentence_ids)):
                    continue
                conn.execute(
                    """
                    INSERT INTO vocabulary_tokens (
                        song_id, sentence_id, surface, dictionary_form, reading, pos,
                        jlpt_level, used_quiz_count, used_flashcard_count, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                    """,
                    (
                        int(song_id),
                        sentence_ids[int(token.sentence_index)],
                        (token.surface or "").strip(),
                        (token.dictionary_form or "").strip(),
                        (token.reading or "").strip(),
                        (token.pos or "").strip(),
                        (token.jlpt_level or "").strip() or None,
                        now,
                    ),
                )

    def favorite_song_analysis_counts(self, song_id: int) -> dict[str, int]:
        with self.connect() as conn:
            song_row = conn.execute(
                "SELECT COUNT(*) AS n FROM sentences WHERE song_id = ?",
                (int(song_id),),
            ).fetchone()
            token_row = conn.execute(
                "SELECT COUNT(*) AS n FROM vocabulary_tokens WHERE song_id = ?",
                (int(song_id),),
            ).fetchone()
            n1_row = conn.execute(
                "SELECT COUNT(*) AS n FROM vocabulary_tokens WHERE song_id = ? AND jlpt_level = 'N1'",
                (int(song_id),),
            ).fetchone()
        return {
            "sentences": int(song_row["n"]) if song_row else 0,
            "tokens": int(token_row["n"]) if token_row else 0,
            "n1_tokens": int(n1_row["n"]) if n1_row else 0,
        }

    def pick_favorite_song_token(
        self,
        *,
        jlpt_level: str = "N1",
        unused_only: bool = True,
        song_status: str = "ready",
    ) -> FavoriteSongToken | None:
        clauses = ["f.status = ?"]
        params: list[object] = [(song_status or "ready").strip()]
        if jlpt_level:
            clauses.append("t.jlpt_level = ?")
            params.append((jlpt_level or "").strip())
        if unused_only:
            clauses.append("t.used_quiz_count = 0")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    t.id AS token_id,
                    t.song_id AS song_id,
                    t.sentence_id AS sentence_id,
                    f.title AS song_title,
                    f.artist AS song_artist,
                    f.youtube_short_url AS youtube_short_url,
                    f.lyrics_url AS lyrics_url,
                    s.sentence_text AS sentence_text,
                    t.surface AS surface,
                    t.dictionary_form AS dictionary_form,
                    t.reading AS reading,
                    t.pos AS pos,
                    t.jlpt_level AS jlpt_level,
                    t.used_quiz_count AS used_quiz_count,
                    t.used_flashcard_count AS used_flashcard_count
                FROM vocabulary_tokens t
                JOIN quiz_songs f ON f.id = t.song_id
                JOIN sentences s ON s.id = t.sentence_id
                WHERE """ + " AND ".join(clauses) + """
                ORDER BY RANDOM()
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return _row_to_favorite_song_token(row) if row else None

    def mark_favorite_token_used(self, *, token_id: int, usage: str) -> bool:
        column = {
            "quiz": "used_quiz_count",
            "flashcard": "used_flashcard_count",
        }.get((usage or "").strip().lower())
        if column is None:
            raise ValueError("usage must be 'quiz' or 'flashcard'")
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE vocabulary_tokens SET {column} = {column} + 1 WHERE id = ?",
                (int(token_id),),
            )
        return cursor.rowcount > 0

    # ── Per-song candidate pack (soft cache for cheap authoring) ───────────────

    def _song_pack_path(self, song_id: int) -> Path:
        return self.path.parent / "quiz_song_packs" / f"{int(song_id)}.json"

    def build_song_candidate_pack(
        self, song_id: int, *, jlpt_level: str = "N1", unused_only: bool = True
    ) -> dict:
        """Build and cache a COMPACT authoring pack for one song, so the author model
        reads a small JSON instead of re-ingesting full lyrics + re-deriving tokens
        on every question. Pulls only from already-cached tables (no fetch, no
        morphology). Contains: song meta, and the unused N1 tokens grouped by the
        sentence they came from (surface/reading/pos + the verbatim sentence that
        doubles as grounding source_excerpt).

        This is a SOFT cache / fast default. The full lyrics stay in the ``lyrics``
        table; when a question needs wider context the author still reads it. The
        pack never replaces that path, it just saves the common cheap case.
        """
        with self.connect() as conn:
            song = conn.execute(
                "SELECT id, title, artist, youtube_short_url, lyrics_url, status "
                "FROM quiz_songs WHERE id = ?",
                (int(song_id),),
            ).fetchone()
            if song is None:
                raise ValueError(f"no favorite_song with id={song_id}")
            clauses = ["t.song_id = ?"]
            params: list[object] = [int(song_id)]
            if jlpt_level:
                clauses.append("t.jlpt_level = ?")
                params.append(jlpt_level.strip())
            if unused_only:
                clauses.append("t.used_quiz_count = 0")
            token_rows = conn.execute(
                """
                SELECT t.id AS token_id, t.sentence_id AS sentence_id,
                       t.surface AS surface, t.dictionary_form AS dictionary_form,
                       t.reading AS reading, t.pos AS pos, t.jlpt_level AS jlpt_level,
                       s.sentence_text AS sentence_text, s.sentence_index AS sentence_index
                FROM vocabulary_tokens t
                JOIN sentences s ON s.id = t.sentence_id
                WHERE """ + " AND ".join(clauses) + """
                ORDER BY s.sentence_index, t.id
                """,
                tuple(params),
            ).fetchall()

        by_sentence: dict[int, dict] = {}
        for r in token_rows:
            sid = int(r["sentence_id"])
            bucket = by_sentence.setdefault(
                sid,
                {
                    "sentence_id": sid,
                    "sentence_index": int(r["sentence_index"]),
                    "sentence_text": r["sentence_text"],
                    "candidates": [],
                },
            )
            bucket["candidates"].append(
                {
                    "token_id": int(r["token_id"]),
                    "surface": r["surface"],
                    "dictionary_form": r["dictionary_form"],
                    "reading": r["reading"],
                    "pos": r["pos"],
                    "jlpt_level": r["jlpt_level"],
                }
            )

        pack = {
            "song_id": int(song["id"]),
            "title": song["title"],
            "artist": song["artist"],
            "youtube_short_url": song["youtube_short_url"],
            "lyrics_url": song["lyrics_url"],
            "jlpt_level": jlpt_level,
            "unused_only": unused_only,
            "built_at": _utc_now_iso(),
            "candidate_token_count": len(token_rows),
            "sentences": [by_sentence[k] for k in sorted(by_sentence)],
        }

        path = self._song_pack_path(int(song["id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
        return pack

    def load_song_candidate_pack(
        self, song_id: int, *, rebuild: bool = False, jlpt_level: str = "N1"
    ) -> dict:
        """Return the cached candidate pack, building it on first use (or when
        ``rebuild`` is set, e.g. after tokens were marked used)."""
        path = self._song_pack_path(int(song_id))
        if rebuild or not path.exists():
            return self.build_song_candidate_pack(song_id, jlpt_level=jlpt_level)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self.build_song_candidate_pack(song_id, jlpt_level=jlpt_level)

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
        tested_jlpt_level=(
            row["tested_jlpt_level"] if "tested_jlpt_level" in row.keys() else None
        ),
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
        source_media_url=row["source_media_url"],
        primary_question_id=row["primary_question_id"] or "",
        support_question_ids=tuple(str(x).strip() for x in support_ids if str(x).strip()),
        exam_points=tuple(str(x).strip() for x in exam_points if str(x).strip()),
        tested_jlpt_level=(
            row["tested_jlpt_level"] if "tested_jlpt_level" in row.keys() else None
        ),
        author=row["author"] or "codex",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_grammar_card(row: sqlite3.Row) -> QuizGrammarCard:
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
    return QuizGrammarCard(
        card_id=row["card_id"],
        level=row["level"],
        headword=row["headword"],
        explanation_zh=row["explanation_zh"],
        example_ja=row["example_ja"],
        example_source_kind=row["example_source_kind"] or "source_excerpt",
        source_name=row["source_name"] or "",
        source_text_url=row["source_text_url"],
        source_media_url=row["source_media_url"],
        primary_question_id=row["primary_question_id"] or "",
        support_question_ids=tuple(str(x).strip() for x in support_ids if str(x).strip()),
        exam_points=tuple(str(x).strip() for x in exam_points if str(x).strip()),
        tested_jlpt_level=(
            row["tested_jlpt_level"] if "tested_jlpt_level" in row.keys() else None
        ),
        author=row["author"] or "codex",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_favorite_song_token(row: sqlite3.Row) -> FavoriteSongToken:
    return FavoriteSongToken(
        token_id=int(row["token_id"]),
        song_id=int(row["song_id"]),
        sentence_id=int(row["sentence_id"]),
        song_title=(row["song_title"] or "").strip(),
        song_artist=(row["song_artist"] or "").strip(),
        youtube_short_url=(row["youtube_short_url"] or "").strip(),
        lyrics_url=row["lyrics_url"],
        sentence_text=(row["sentence_text"] or "").strip(),
        surface=(row["surface"] or "").strip(),
        dictionary_form=(row["dictionary_form"] or "").strip(),
        reading=(row["reading"] or "").strip(),
        pos=(row["pos"] or "").strip(),
        jlpt_level=(row["jlpt_level"] or "").strip() or None,
        used_quiz_count=int(row["used_quiz_count"] or 0),
        used_flashcard_count=int(row["used_flashcard_count"] or 0),
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
