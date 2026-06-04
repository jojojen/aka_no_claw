---
name: quiz-authoring-workflow
description: Use when working on aka_no_claw /quiz JLPT question authoring, review, deletion, authoring-knowledge feedback, or handoff tracking. Enforces the user's workflow: confirm before starting, Codex authors questions directly, local model acts only as independent solver/checker, follow the current source scope (temporarily Project Sekai songs), keep at most 3 retained questions per song, write lessons into quiz_authoring_knowledge, and avoid locking DB/resources so other agents can work.
---

# Quiz Authoring Workflow

Use this skill for `/quiz` JLPT work in `aka_no_claw`.

## First Rule

Before generating questions, deleting questions, writing `quiz_authoring_knowledge`, or changing any quiz DB state, confirm the exact work scope with the user. Do not start just because the workflow is understood.

If the user asks only for analysis, explain findings without changing code or DB.

## Hard Constraints

- Do not modify code unless the user explicitly asks for code changes.
- Do not use `QuizGenerator.generate_one_question()` or `/quiz gen20` for this workflow. Codex authors the question directly.
- The local model is an independent solver/checker, not the author.
- When the DB has `favorite_songs.status='ready'` material, prefer those cached songs before pulling from the general song pool.
- Source priority is song-list-first, not token-first: favorite songs should be covered first, and only after the ready favorite-song list has already produced questions may the workflow fall back to non-favorite songs.
- Do not refetch the full lyric page or rerun morphology for a song that already has favorite-song cache rows unless the cache is missing/broken and the user approved repair work.
- Cache-first does **not** mean token-only. When better question quality needs wider context, read the cached full lyrics first; when interpretation/background matters, read cached commentary/appreciation material first if available.
- Prefer songs listed in `data/proseka_songs.json` (Project Sekai) as the current primary source pool, but Project Sekai is not the only allowed scope. The user clarified that non-Project-Sekai songs, such as `心拍数 #0822`, may also be used when source grounding is reliable and the item is good.
- Retain at most 3 Codex-authored questions per song. Existing qwen/Claude questions do not count against this quota.
- Prefer current scope `JLPT N1` and `question_type="単語"` unless the user explicitly changes scope.
- Current `単語` subtypes are `漢字読み`, `言い換え類義`, and `文脈規定`.
- Do not treat existing DB quantity as quality. Stability requires self-review and feedback.
- Do not lock resources: no long-running daemon, no bot restart, no background generation loop, no long SQLite transaction, no held DB connection.

## Source And Quota Checks

Before accepting a generated question:

1. Confirm the current user-approved source priority. If a suitable favorite-song cache entry exists, use favorite songs first at the song-source level. Only after the ready favorite-song list has already been covered may non-favorite songs be considered. Otherwise, Project Sekai songs in `data/proseka_songs.json` are preferred first, but non-list songs may be used after the user's 2026-06-04 clarification.
2. Normalize obvious title punctuation when checking membership, such as terminal `。`, HTML entities, spacing, `＃/♯/#`, and common Project Sekai title variants.
3. Count retained Codex-authored questions for that song in `data/quiz.sqlite3`.
4. Reject/delete if the source cannot be grounded to a reliable text/media source.
5. Reject/delete if retaining it would exceed 3 Codex-authored questions for that song.

For favorite-song sourced work, read from the cached tables in `data/quiz.sqlite3`:

- `favorite_songs`
- `lyrics`
- `sentences`
- `vocabulary_tokens`

After choosing a favorite song as the current source, prefer unused tokens within that song:

```sql
SELECT *
FROM vocabulary_tokens
WHERE jlpt_level = 'N1'
  AND used_quiz_count = 0
ORDER BY RANDOM()
LIMIT 1;
```

Then resolve `sentence_id` back to the cached original sentence. Do not re-fetch the lyric site just to get the same line again.

If a token-level view is too narrow for the current authoring task, step up in this order:

1. cached sentence
2. cached full lyrics
3. cached commentary/appreciation text
4. only then consider new external fetches if the cache genuinely lacks the needed context

## Author One Question

Codex writes the question content directly after selecting a valid source and real grounding text. Insert only after passing checks.

Use existing DB APIs for insertion and deletion. Keep each operation short-lived.

```python
q = db.insert_question(
    level="JLPT N1",
    exam_point="<fine-grained type>",
    stem="<question stem>",
    options=("<A>", "<B>", "<C>", "<D>"),
    answer_index=<0-based index>,
    explanation="<zh-TW explanation plus readings where relevant>",
    source_type="vocaloid_song",
    source_name="<song name>",
    source_text_url="<lyrics/commentary url>",
    source_media_url="<PV/audio url>",
    source_excerpt="<real grounding text shown or cited>",
    tested_point="<specific point>",
    verified=True,
    author="codex",
)
```

The inserted row's `author` must be `codex`.

## Local Model Solver Checks

Use the local model after Codex authors a candidate, before final retention:

- Non-reading/self-contained types: give the model only stem + options. It must choose the same `answer_index`.
- Reading types (`内容理解`, `主張理解`, `統合`, `情報検索`, `読解`): give the model passage + stem + options. It must choose the same `answer_index`.
- Reading leak probe: give the model stem + options without passage. A good reading item should return "cannot determine" / `-1`; if it can still choose the answer, reject the question.

Do not let the local model rewrite or author the question.

Important calibration:

- The local model is an advisory solver/checker, not the final judge.
- For objectively checkable `漢字読み` and grammar items, local-model disagreement is a false-negative signal to investigate, not automatic rejection.
- If Codex can verify the reading/grammar fact independently and the item is genuinely N1-level, the question may be retained despite local-model disagreement.
- For reading items, the leak probe remains strict: if the model can answer correctly without the passage, reject the question.

## Review Checklist

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

## Current DB Caveat

The DB may contain historical `exam_point` values outside the current authoring scope, including `内容理解`, `主張理解`, `文法形式の判断`, `用法`, `文章の文法`, `文の組み立て`, `情報検索`, and `統合理解`.

Those existing rows are historical data, not permission to generate those types. Follow the user's current scope unless explicitly changed.
