---
name: quiz-authoring-workflow
description: Use when working on aka_no_claw /quiz JLPT question authoring, review, deletion, authoring-knowledge feedback, or handoff tracking. Enforces the user's workflow: confirm before starting; Claude and Codex author in parallel (Claude owns 文法形式 + reading types, Codex owns the 単語 types); the local qwen3:14b model is only a blind solver/grader, never an author; follow the current source scope (temporarily Project Sekai songs); keep at most 3 retained 単語 questions per song; write lessons into quiz_authoring_knowledge; and avoid locking DB/resources so other agents can work.
---

# Quiz Authoring Workflow

Use this skill for `/quiz` JLPT work in `aka_no_claw`.

## First Rule

Before generating questions, deleting questions, writing `quiz_authoring_knowledge`, or changing any quiz DB state, confirm the exact work scope with the user. Do not start just because the workflow is understood.

If the user asks only for analysis, explain findings without changing code or DB.

## Priority Order

The user's authoring principles are **strictly ordered**. Treat this as a
priority ladder, not four equally important goals:

1. **Correctness / quality comes first.**
   If a faster / cheaper / lower-request method lowers question quality,
   grounding quality, distractor quality, vocab-card example quality, or review
   confidence, do **not** use it.
2. **Avoid getting the source sites / IP blocked.**
   If a method would hit external sites more aggressively, refetch pages that are
   already cached, or create avoidable scraper-like traffic, do **not** use it,
   even if it is faster or cheaper in tokens.
3. **Save token usage.**
   After quality and IP-safety are preserved, prefer cache-first, deterministic
   checks, and compact context to reduce model spend.
4. **Speed comes last.**
   Faster is good only after 1, 2, and 3 are already satisfied. Never sacrifice
   correctness, source safety, or token discipline merely to go faster.

If two tactics conflict, resolve them by this order. Examples:

- Prefer a slower manual/source-grounded fix over a quick weak paraphrase.
- Prefer cached lyrics/commentary over re-fetching a site, even if re-fetching is
  more convenient in the moment.
- Prefer a one-question-at-a-time solver check over a flaky batch if the batch
  risks wrong retention.
- Prefer a token-heavier strong-model judgment over a cheap shortcut when that
  shortcut weakens N1 calibration or example quality.

## Hard Constraints

- Do not modify code unless the user explicitly asks for code changes.
- Do not use `QuizGenerator.generate_one_question()` or `/quiz gen20` for this workflow. The questions are authored directly by a strong model.
- **Two authors run in parallel, by question type:** Claude owns **文法形式の判断 + 文の組み立て + the reading types** (内容理解・主張理解・統合理解・情報検索・読解・文章の文法); Codex owns the **単語 types** (漢字読み・言い換え類義・文脈規定・用法). Before a big push, re-check the per-author / per-type breakdown via SQL since coverage shifts.
- The local qwen3:14b model is a **blind solver/grader only — never an author.** It re-solves a finished candidate to check correctness; it does not write or rewrite questions.
- When the DB has `favorite_songs.status='ready'` material, prefer those cached songs before pulling from the general song pool.
- Source priority is song-list-first, not token-first: favorite songs should be covered first, and only after the ready favorite-song list has already produced questions may the workflow fall back to non-favorite songs.
- Do not refetch the full lyric page or rerun morphology for a song that already has favorite-song cache rows unless the cache is missing/broken and the user approved repair work.
- Cache-first does **not** mean token-only. When better question quality needs wider context, read the cached full lyrics first; when interpretation/background matters, read cached commentary/appreciation material first if available.
- Prefer songs listed in `data/proseka_songs.json` (Project Sekai) as the current primary source pool, but Project Sekai is not the only allowed scope. The user clarified that non-Project-Sekai songs, such as `心拍数 #0822`, may also be used when source grounding is reliable and the item is good.
- Retain at most 3 単語 questions per song (the song-as-vocabulary-source quota). Reading/grammar types are passage/article-based, not song-vocabulary-based, so this per-song cap does not apply to them.
- **Active scope is all three type families** at `JLPT N1` (N2-fallback per below): the **単語** types (`漢字読み`, `言い換え類義`, `文脈規定`, `用法`), the **reading** types (`内容理解`, `主張理解`, `統合理解`, `情報検索`, `読解`, `文章の文法`), and the **grammar** types (`文法形式の判断`, `文の組み立て`). Author by the division of labour above (Claude: grammar + reading; Codex: 単語).
- **N1-preferred, N2 fallback (2026-06-07).** Genuine N1 vocabulary is the first
  choice. But N1 words are scarce per song, so requiring N1-only is too strict: when
  a song has no further suitable genuine-N1 word, you MAY author from a genuine **N2**
  word rather than skip the song or force a weak N1. Do NOT drop below N2 (no N3/N4
  basics). Order within a song: genuine N1 → genuine N2 → next song.
- **`tested_jlpt_level` = the ITEM's true difficulty, not the headword's auto-tag.**
  Pass `tested_jlpt_level="N1"` or `"N2"` to `insert_question(...)`. `level` stays
  `"JLPT N1"` (exam pool / review stream); `tested_jlpt_level` is the honest
  difficulty badge (cards show `〔難度 N2〕`). Judge it by type, with your own
  strong-model judgment:
  - **漢字読み**: question difficulty = the word's reading difficulty. An N2 word is
    an N2 item — cannot be dressed up as N1. Tag `N2`.
  - **言い換え類義 / 文脈規定**: difficulty can come from the options/context, not just
    the headword. If the surface headword is N2 but selecting the answer genuinely
    requires N1-level synonyms or N1-level context/語感, the item is N1 — tag `N1`
    even though the tested word is N2. If the discrimination is also only N2, tag `N2`.
  - Leave `tested_jlpt_level` unset (NULL) only for genuine N1 (NULL is treated as N1).
    Always set it explicitly to `N2` for fallback items so the badge renders.
- Do not treat existing DB quantity as quality. Stability requires self-review and feedback.
- Do not lock resources: no long-running daemon, no bot restart, no background generation loop, no long SQLite transaction, no held DB connection.

## Token-Efficient Authoring (cost discipline)

These three mechanisms cut cloud-model token cost **without** lowering quality. They
are safe because every correctness gate still runs at 100%; only mechanical work and
re-reading are removed.

1. **Stable spec is cached, not re-inlined.** The JLPT format, output schema, per-type
   rules, and worked examples live in `authoring_spec.md` (this skill's folder). Read
   it once as a fixed prefix; do not paraphrase those rules into each authoring turn
   (paraphrasing busts the prompt cache). Only the variable half — this song's
   candidate pack + the RAG lessons for this source — changes per call.

2. **Read the per-song candidate pack, not the full lyrics, by default.**
   `db.load_song_candidate_pack(song_id)` returns a compact JSON: the song's unused N1
   tokens grouped by their verbatim sentence (which doubles as `source_excerpt`). Built
   once from already-cached tables (no fetch, no morphology), then reused. This is a
   **soft cache**: when a question genuinely needs wider context (often 文脈規定 /
   言い換え / reading types), still read the cached full lyrics / commentary — the pack
   never forbids that, it just saves the common cheap case. Call with `rebuild=True`
   after marking tokens used.

3. **Mechanical checks are pure Python — never spend an LLM on them.** Use the
   `quiz_db.py` helpers: `options_have_duplicates`, `answer_leaks_into_stem`,
   `audit_kanji_reading_distractors` (additive advisory filter for 漢字読み distractors),
   and `questions_are_near_duplicate` / `db.find_duplicate_questions` for dedup. Run
   **dedup once at the final-output stage**, not on every step. `audit_…` flags obvious
   garbage cheaply but is NOT an oracle — an empty result does not certify distractor
   quality; that judgment stays with the strong model.

What stays on the strong model (do NOT offload to the local solver to save tokens):
difficulty / N1-level judgment, and distractor plausibility in the subtle band. The
local solver runs the blind correctness solve at **100%, never sampled**.

## Source And Quota Checks

Before accepting a generated question:

1. Confirm the current user-approved source priority. If a suitable favorite-song cache entry exists, use favorite songs first at the song-source level. Only after the ready favorite-song list has already been covered may non-favorite songs be considered. Otherwise, Project Sekai songs in `data/proseka_songs.json` are preferred first, but non-list songs may be used after the user's 2026-06-04 clarification.
2. Normalize obvious title punctuation when checking membership, such as terminal `。`, HTML entities, spacing, `＃/♯/#`, and common Project Sekai title variants.
3. Count retained Codex-authored questions for that song in `data/quiz.sqlite3`.
4. Reject/delete if the source cannot be grounded to a reliable text/media source.
5. Reject/delete if retaining it would exceed 3 Codex-authored questions for that song.

Source selection uses the candidate pack, not a hand-written token query:
`db.load_song_candidate_pack(song_id)` returns the song's unused N1 tokens already
grouped by their verbatim cached sentence (the sentence doubles as `source_excerpt`).
It is built from the cached tables (`favorite_songs`, `lyrics`, `sentences`,
`vocabulary_tokens`) with no fetch and no re-morphology. See mechanism #2 under
[Token-Efficient Authoring](#token-efficient-authoring-cost-discipline) for the
soft-cache rule and when to step up to full lyrics / commentary. Call with
`rebuild=True` after marking tokens used. Do not re-fetch the lyric site to get a
line the cache already holds.

## Author One Question

Codex writes the question content directly after selecting a valid source and real grounding text. Insert only after passing checks.

The JSON output schema, per-type rules, universal hard gates, and worked examples
live in `authoring_spec.md` (this skill's folder) — read it once as the stable
cached prefix; do not paraphrase it here. This section only covers the two
workflow-specific steps that the spec does not.

**Step 0 — Register vocab seed** (required for lexical types: 漢字読み, 言い換え類義, 文脈規定, 用法):

Before inserting, register the headword's reading and Chinese gloss in the `vocab_seed` table. Without this, `_backfill_vocab_cards()` will silently skip the headword and no card will be created.

```python
db.upsert_vocab_seed(
    headword="<tested_point surface form>",
    reading_hiragana="<hiragana reading>",
    zh_gloss_short="<short zh-TW gloss>",
)
```

`zh_gloss_short` is the **中文** field rendered on the card — it MUST be Chinese (Han-only). `upsert_vocab_seed` raises `ValueError` if the gloss contains any kana, so a Japanese paraphrase can never leak into the 中文 field (`_contains_kana` gate, `quiz_db.py:491` / `:921`). Write a real short zh-TW gloss, not a Japanese 言い換え.

`quiz_vocab_seed.py` no longer exists. The `vocab_seed` table in `data/quiz.sqlite3` is the sole source. Do not attempt to import or edit a Python seed file.

**Step 1 — Insert.** Use `db.insert_question(...)` with the fields from the
`authoring_spec.md` schema, plus the workflow constants: `level="JLPT N1"`,
`source_type="vocaloid_song"`, `verified=True`, and `author="codex"`. The inserted
row's `author` must be `codex`. Keep each DB operation short-lived.

## Local Model Solver Checks

Use the local model after Codex authors a candidate, before final retention:

- Non-reading/self-contained types: give the model only stem + options. It must choose the same `answer_index`.
- Reading types (`内容理解`, `主張理解`, `統合`, `情報検索`, `読解`): give the model passage + stem + options. It must choose the same `answer_index`.
- Reading leak probe: give the model stem + options without passage. A good reading item should return "cannot determine" / `-1`; if it can still choose the answer, reject the question.

Do not let the local model rewrite or author the question.

Important calibration:

- **qwen3:14b is BELOW N1.** Its blind answer disagreeing with the authored answer is **weak evidence** the item is wrong, NOT a veto — qwen may simply have failed a legitimately hard N1 item. Never silently drop on disagreement.
- The hard gate is **grounding** (`is_grounded`), never the grader's vote. Grounding is enforced in code and must always pass.
- On grader **agreement** → retain (high confidence). On grader **disagreement** → capture `{stem, options, my answer, qwen's answer_index, qwen's reason}` and have the **strong model (Claude) adjudicate**: if grounded and unambiguously correct, force-retain; if a distractor is genuinely also-valid, fix or drop.
- For objectively checkable `漢字読み` and grammar items, local-model disagreement is a false-negative signal to investigate, not automatic rejection.
- For reading items, the leak probe remains strict: if the model can answer correctly without the passage, reject the question.

## Review Checklist

Run the pure-Python mechanical checks first (they are free — never spend an LLM on
them): `options_have_duplicates`, `answer_leaks_into_stem`,
`audit_kanji_reading_distractors` for 漢字読み distractors, and
`db.find_duplicate_questions` / `questions_are_near_duplicate` for dedup at the
final-output stage. These flag obvious garbage cheaply but are NOT oracles — an
empty result does not certify quality. The judgment items below stay with the
strong model.

Keep a question only if all pass:

- Source song is Project Sekai and song quota remains <= 3.
- Self-contained: stem plus options are enough to answer.
- Correct answer is objectively unique and correct.
- Difficulty is truly N1: high-level vocabulary, kanji compounds, idioms, or subtle usage; not basic N3/N2 words.
- Options are same type and plausible: all readings, all vocabulary/phrases, or all fill-in choices.
- Distractors are wrong for concrete linguistic reasons, not just less likely.
- Explanation matches `answer_index` and quotes option text rather than relying on fragile option numbers.
- Explanation includes useful readings for kanji terms when relevant.
- Song/text/media links are present for song-sourced questions.

Common reject examples:

- `漢字読み` tests a basic word such as `最高` or `味方`.
- `漢字読み` treats a pure song title or full title phrase as a vocabulary item, such as `25時の情熱`.
- Vocabulary-card examples are not traceable to real lyric/article/commentary source text, reuse the same sentence across multiple cards, use the same template for similar words with only the target word swapped, or spill into a long paragraph / more than 3 sentences. Template examples such as `「X」という言葉が心に残った。`, `Xという言葉を覚えた。`, `Xの意味を調べた。`, or `Xについて考えた。` are rejected; the card's `example_ja` must be a short verbatim substring of `source_excerpt`.
- The correct reading is wrong or a real alternative reading appears as a distractor.
- `言い換え類義` uses the same word in kana/kanji as the answer.
- `文脈規定` is only answerable by remembering the lyric, not by language context.
- Multiple options are plausible in context.
- The explanation contradicts the displayed answer or uses stale option indexes.

## Reject, Reflect, Teach

For every rejected question:

1. Delete it from `quiz_questions`.
2. Distill the failure into a general transferable rule.
3. Write the rule into `quiz_authoring_knowledge` with `db.upsert_authoring_knowledge(...)`.
4. Do not write song-specific one-off advice unless the user asked for it.

Good categories:

- `vocabulary`
- `distractor_design`
- `level_calibration`
- `source_grounding`
- `reading`
- `grammar`

Rule quality:

- Abstract and reusable.
- Names the failure mode.
- Gives a concrete prevention rule.
- Includes keywords likely to match future retrieval, such as `単語`, `漢字読み`, `言い換え`, `文脈規定`, `N1`, `Project Sekai`, `プロセカ`, `distractor`.

## Handoff Record

Maintain a concise progress record when the user has approved quiz work. The record must let another agent resume without context.

Track:

- Work scope and user-approved constraints.
- Start/end timestamp for the batch.
- Generated question IDs.
- Song name, exam point, tested point, and decision.
- Delete reason for rejected questions.
- KB rules written or updated.
- Current counts: generated, accepted, rejected, remaining target.
- Next exact step.

Do not create or update a handoff file until the user confirms where to keep it. If no file is approved, report the handoff summary in chat.

## Resource Discipline

- Generate in small batches, ideally one question at a time.
- Open SQLite connections only for a single operation and close them immediately.
- Do not run `/quiz gen20` blindly.
- Do not restart launchd services for authoring-only work.
- Do not hold an interactive Python session open while waiting for user input.
- If Ollama or VocaDB is slow/unavailable, report it; do not silently fake a pass.

## Scope Note

All three type families are current authoring targets (see Hard Constraints): 単語, reading (`内容理解`, `主張理解`, `統合理解`, `情報検索`, `読解`, `文章の文法`), and grammar (`文法形式の判断`, `文の組み立て`). Author by the division of labour — Claude owns grammar + reading, Codex owns 単語. If the user narrows scope for a given batch, follow that for the batch.
