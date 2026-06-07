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
- The answer must not be the same word in kana/kanji as the stem target.
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
