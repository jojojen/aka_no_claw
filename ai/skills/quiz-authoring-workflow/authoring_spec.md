# Quiz Authoring Spec (stable prefix)

This file is the **stable, cacheable** half of the authoring prompt: the JLPT
format rules, the output schema, and worked examples that DO NOT change per song.
Reference it once; do not re-inline these rules into every authoring turn. The
variable half (this song's candidate pack, the RAG-retrieved lessons for this
source) is what changes call-to-call.

Token rule: keep this content verbatim and in a fixed position so it stays in the
prompt cache across questions. If you paraphrase it each turn, the cache misses and
you pay full input cost every time. Edit this file only when a rule genuinely
changes.

---

## Output schema (every question)

Author a single JSON object, then insert with `db.insert_question(...)`:

```json
{
  "level": "JLPT N1",
  "exam_point": "<漢字読み | 言い換え類義 | 文脈規定 | ...>",
  "stem": "<question stem>",
  "options": ["<A>", "<B>", "<C>", "<D>"],
  "answer_index": 0,
  "explanation": "<zh-TW explanation; quote the option text, never bare indices>",
  "tested_point": "<the specific word/point being tested>",
  "tested_jlpt_level": "<N1 | N2 — the ITEM's true difficulty; NULL/omit = N1>",
  "source_name": "<song name>",
  "source_text_url": "<lyrics/commentary url>",
  "source_media_url": "<PV/audio url>",
  "source_excerpt": "<verbatim grounding text — a real lyric line>"
}
```

Exactly 4 options. `answer_index` is 0-based. `author` is set to `codex` (or
`Claude`) by the caller.

## Universal hard gates (deterministic — code enforces, don't fight them)

These run in `quiz_db.py` before any LLM grader. Author so they pass:

- No duplicate options (`options_have_duplicates`).
- The correct option must NOT appear verbatim in the stem (`answer_leaks_into_stem`).
- `source_excerpt` must be real grounding text; for vocab cards `example_ja` must be
  a verbatim substring of `source_excerpt`.

## Vocab-card example QUALITY (beyond the verbatim gate)

`example_ja` is the sentence the learner actually studies, so it must teach REAL
USAGE — not a bare fragment, not a memoryless template. "Quality/correctness" is
NOT just the question being right: the example is itself part of the quality bar.
It must (1) actively AID MEMORY and (2) ideally pin down the word's 語感 so the
learner can tell it apart from its 類義語. Choose a context that makes the nuance
unmistakable — 妥協 in a give-and-take negotiation that collapses (vs 譲歩 = one-sided
yielding), 凶暴 as a savage dangerous animal (vs 乱暴 = merely rough), 偽装 as
camouflage to deceive the eye (vs 偽造 = forging a fake). A bland "X happened"
sentence is a MISS even when grammatically correct and grounded. It is auto-derived by
`_backfill_vocab_cards` from the **primary question's `source_excerpt`** (it takes
the SHORTEST usable line containing the headword and OVERWRITES the card on every
bootstrap), so you control it entirely through the excerpt you author:

- DEFAULT — use the FULL lyric line / couplet as `source_excerpt`, never a 3–4 char
  word fragment. BAD: 「妥協大好き」, 「此処は宴」, 「生真面目そうな」. GOOD: the whole
  line 「此処は宴　鋼の檻　その断頭台で見下ろして」.
- CRYPTIC FALLBACK — when even the full lyric line is too cryptic to show standard
  usage (Vocaloid lyrics often drop a word in as a bare noun), author a natural N1
  sentence as the excerpt instead. It MUST carry concrete, memorable context.
  - GOOD: 「双方が一歩も妥協せず、交渉は決裂した。」 / 「初めての告白に、彼女は恥じらいを隠せなかった。」
  - BANNED (memoryless templates): 「〜という言葉を覚えた」「〜について考えた」「〜を学んだ」
    「〜の意味を調べた」, or any cookie-cutter frame that could slot ANY word.
    `vocab_example_is_low_value()` blocks a few literal strings, but the user's bar
    is BROADER — judge memorability yourself, don't just dodge the blocked list.
  - Keep an authored sentence a SINGLE clause (no 。！？ mid-split) so the whole
    sentence shows; `source_name` still cites the song the word was found in.
- ONE CARD PER NORMALIZED LINE — don't set an excerpt whose headword-line another
  headword already cards (e.g. 恥じらい and 素足 share 「恥じらいの素足をからめる」), or
  the dedup silently drops one card. Give each headword a distinct line/sentence.

## Per-type rules

### 漢字読み
- `tested_point` must be genuine **N1** vocabulary: 難読語・複合語・文語語彙
  (老舗・矜持・逡巡・蹉跌・執拗・脆弱・凋落). NOT N3/N4 basics
  (視聴者・創造・形成・予測・脱出・裏切る・批判).
- **Distractors must be plausible WRONG READINGS OF THE TARGET WORD** — alternative
  on/kun readings, similar-character confusion, connected-sound (連濁) variants.
  NEVER readings of unrelated words.
  - Good: 【創造】そうぞう → そうさく / さくぞう / そうきょう
  - Bad:  【創造】そうぞう → けいばつ / やきめ (zero relation to the answer)
- Run `audit_kanji_reading_distractors(options, answer_index)` — a cheap additive
  filter. A non-empty result means a distractor shares no sound with the answer or
  is phrase-length; fix it. An empty result does NOT certify quality — it only means
  no obvious garbage.

### 言い換え類義
- The answer must not be the same word in kana/kanji as the stem target. This means
  the correct option may NOT contain the headword's reading (in kana) NOR the
  headword's own kanji string — that hands the answer to the solver (the word is
  inside its own "synonym"). BAD: 【建前】→「表向きの方針やたてまえ」(restates たてまえ),
  【喚く】→「大声でわめき叫ぶ」(restates わめく), 【塊】→「一つにかたまったもの」. GOOD:
  paraphrase with DIFFERENT words — 【建前】→「表向きに示す名目や方針」. (Reusing ONE
  incidental kanji in an otherwise-different paraphrase is fine: 【転移】→「他の場所へ
  移り広がる」.) Run `synonym_answer_restates_headword(headword, reading, option)` —
  True = the answer restates the word; rewrite it. `answer_leaks_into_stem` does NOT
  catch this (the leak is in the OPTION, not the stem).
- Also avoid pre-stating the answer in the EXCERPT: if the example sentence already
  contains the answer's key words (【喚く】excerpt「大声で喚く」 + answer「大声で…」), the
  lexical overlap gives it away — pick an excerpt that shows the word in context
  WITHOUT lexically defining it.
- All four options same register/length.

### 文脈規定
- Answerable by language context, not by remembering the lyric.
- Needs wider context? Read the cached full lyrics / commentary first (see pack note).

### Reading types (内容理解・主張理解・統合・情報検索・読解)
- The correct option must NOT be a verbatim 本文 line (`correct_option_is_verbatim_copy`).
- The leak probe: stem+options without 本文 must be unanswerable. If solvable
  without the passage, reject.

## Difficulty calibration

Difficulty/N1-level judgment is a **strong-model** call. Do not delegate it to the
local solver (it is sub-N1; its agreement/rejection is advisory evidence only, never
the deciding vote). The local model's job is the blind correctness solve, run at
100% — never sampled.

**N1-preferred, N2 fallback + `tested_jlpt_level`.** Prefer genuine N1 vocabulary.
When a song has no further suitable N1 word, a genuine N2 word is allowed (never
N3/N4). Record the item's TRUE difficulty in `tested_jlpt_level` (`N1`/`N2`; NULL = N1)
— `level` stays `JLPT N1` (the study pool). Judge by type:
- **漢字読み**: question difficulty equals the word's reading difficulty. N2 word ⇒
  N2 item; tag `N2`. It cannot be made N1 by framing.
- **言い換え類義 / 文脈規定**: difficulty may come from the options/context. An N2
  surface headword can still be an N1 item if choosing the answer needs N1-level
  synonyms or N1-level context — tag `N1` then; otherwise `N2`.

## Worked examples

**漢字読み (good):**
```
exam_point: 漢字読み
tested_point: 矜持
stem: 「矜持」の読みとして最も適切なものはどれか。
options: ["きょうじ", "きんじ", "きょうじゃく", "けいじ"]
answer_index: 0
explanation: 「矜持（きょうじ）」=自尊心。距離の近い音「きんじ／けいじ」を誤答に配置。
```

**言い換え類義 (good):**
```
exam_point: 言い換え類義
tested_point: とりとめのない
stem: 〈とりとめのない〉に最も近い意味はどれか。
options: ["まとまりや結論のない", "心から誠実に込めた", "互いに深く通じ合う", "丁寧に吟味した"]
answer_index: 0
```
