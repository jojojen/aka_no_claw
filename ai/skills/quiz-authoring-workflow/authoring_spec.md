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
- The vocab seed's `zh_gloss_short` (the **中文** field) must be **Han-only Chinese** —
  `upsert_vocab_seed` raises `ValueError` on any kana (`_contains_kana`). A Japanese
  言い換え is NOT a Chinese gloss; write a real zh-TW meaning.

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
- CONJUGATED-FORM TRAP — the headword must appear **verbatim** in the excerpt. If the
  lyric only has a conjugated form (装った, not 装う / 佇んで, not 佇む), the dict-form
  headword is not a substring → no card. Author a **dict-form memorable sentence** as
  the excerpt instead (it still teaches real usage; `source_name` still cites the song).

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
- AVOID DUAL-READING HEADWORDS — a word with two legitimate readings is unfair as a
  漢字読み item (both readings are "correct"). Drop it. Examples: 艶やか (つややか／あでやか),
  卒塔婆 (そとば／そとうば).
- WATCH づ/ず・ぢ/じ HOMOPHONE DISTRACTORS — a distractor that is the old-kana spelling
  of the answer is pronounced identically, i.e. a second correct answer. BAD:
  【躓く】つまずく with distractor つまづく. Use genuinely different readings instead.

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

### 用法
- Present the target word used in 4 different sentences; exactly ONE uses it correctly.
- DON'T let only the correct option contain the target word while every distractor swaps
  it for a near-synonym — that tests visual presence, not usage. `youhou_target_word_presence_leaks`
  rejects this; all four options should contain the word, only one used correctly.
- The correct option is the grounding anchor (it carries the real line for 用法).

### Reading types (内容理解・主張理解・統合理解・情報検索・読解・文章の文法)
- **Passage must be ADAPTED FROM A REAL, EXISTING ARTICLE — never fully original prose.**
  Summarize / restructure / simplify a real source (utaten 考察・特集, ニコニコ大百科,
  Wikipedia, VocaDB, lyrical-nonsense 解説, etc.) into an N1-level 本文. Set
  `source_text_url` to the actual article adapted from. Don't copy long spans verbatim
  (copyright) — paraphrase and condense.
- **No leading hint/giveaway sentence.** The 本文 is the article BODY only — never prepend
  a sentence that states the conclusion/主張. (The renderer shows `source_excerpt` as 【本文】
  before the stem, so a giveaway snippet hands the answer away.)
- **Correct option must be a PARAPHRASE, not a verbatim lift** of a 本文 sentence
  (`correct_option_is_verbatim_copy` rejects ≥90%-coverage copies). Rewrite the meaning with
  different vocabulary/structure so the item forces a 同義轉換, not 字面 string-matching. If
  needed, also rewrite the 本文 so it doesn't state the answer in the option's words.
- **Distractors are near-synonyms / plausible misreadings of the passage**, not far-from-text
  fillers — a strong "close but wrong" distractor discriminates; weak off-topic ones don't.
- **Leak probe (strict):** stem+options WITHOUT the 本文 must be unanswerable. If the blind
  solver still lands on the answer, the answer leaked into the stem — reject
  (`_passes_reading_discrimination`).

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

## Code-gate reference (source of truth)

These rules are MACHINE-ENFORCED. The code is authoritative; this spec only points to
it. Don't re-derive or fight a gate — author so it passes.

| Rule | Function | Location |
| --- | --- | --- |
| Source grounding (3-tier A/B/C) | `is_grounded` | `quiz_db.py:277` |
| Excerpt-type vs exam-point conflict | `source_excerpt_type_conflicts_with_exam_point` | `quiz_db.py` |
| No duplicate options | `options_have_duplicates` | `quiz_db.py:401` |
| Answer leaks into stem | `answer_leaks_into_stem` | `quiz_db.py:416` |
| 言い換え restates headword | `synonym_answer_restates_headword` | `quiz_db.py:430` |
| 用法 presence-only leak | `youhou_target_word_presence_leaks` | `quiz_db.py:458` |
| Chinese gloss must be Han-only | `_contains_kana` (in `upsert_vocab_seed`) | `quiz_db.py:491` / `:921` |
| 漢字読み distractor audit | `audit_kanji_reading_distractors` | `quiz_db.py:515` |
| Reading: correct option verbatim copy | `correct_option_is_verbatim_copy` | `quiz_db.py:365` |
| Reading: 2-tier discrimination/leak probe | `_passes_reading_discrimination` | `quiz_generator.py:384` |
| Dual-LLM author+blind-grader verify | `_validate_and_verify` | `quiz_generator.py:305` |
| Vocab example length cap (70 chars) | `source_excerpt_vocab_example` (`_MAX_VOCAB_EXAMPLE_CHARS`) | `quiz_db.py:211` |
| Vocab example low-value filter | `vocab_example_is_low_value` | `quiz_db.py:182` |
| Card bootstrap + primary/example pick | `_backfill_vocab_cards`, `_VOCAB_PRIMARY_PRIORITY` | `quiz_db.py` |

Card primary-question priority: **用法 > 文脈規定 > 言い換え類義 > 漢字読み** (`_VOCAB_PRIMARY_PRIORITY`).
Line numbers drift — `grep` the function name if an anchor is stale.
