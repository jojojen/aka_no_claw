# /quiz Favorite Songs

Last reviewed: 2026-06-20
Status: Current
Owner area: quiz

Last updated: 2026-06-05 JST

## Command

```text
/quiz like song <youtube_url>
```

Example:

```text
/quiz like song https://youtu.be/OIBODIPC_8Y?si=XzdzDFGtCRQoXH7T
```

## Purpose

Store a liked song once, prefetch its metadata and lyrics, run morphology once, and
reuse SQLite rows later for quiz generation and vocabulary work.

This is a cache-first design:

- collect once
- analyze once
- reuse many times

## Ingestion Flow

1. Parse the YouTube URL and normalize it to `youtube_short_url`.
2. Fetch YouTube metadata via oEmbed.
3. Extract `title` and `artist`.
4. Find full lyrics, in this order:
   - `VocaDB`
   - `歌ネット`
   - `UtaTen`
5. Store the **full lyrics text**, not only extracted tokens.
6. Split lyrics into reusable sentences/lines.
7. Run `SudachiPy + SudachiDict Full`.
8. Store tokens, readings, parts of speech, and rule-based JLPT tags.
9. When useful for authoring quality, also cache background/commentary/appreciation text as an additional source layer.
10. Mark the song `status='ready'`.

## SQLite Tables

- `favorite_songs`
  - one row per liked YouTube song
- `lyrics`
  - full lyrics text per song
- `sentences`
  - sentence/line cache linked to the song
- `vocabulary_tokens`
  - per-token morphology and reuse counters

Current implementation status:

- v1 already caches full lyrics and morphology
- commentary/appreciation caching is a documented source-priority rule and the next extension when that material is needed regularly

Important: token cache is not a replacement for full-text cache.

- `vocabulary_tokens` is for quick lexical selection
- `lyrics.full_text` is for whole-song context
- commentary/appreciation cache is for meaning, theme, and reading-comprehension style support when needed

## Source Priority Rule

When future `/quiz` authoring, flashcard work, or review work needs song material:

1. Prefer `favorite_songs.status='ready'` songs first.
2. At the **song-source level**, rotate through the favorite-song list before using non-favorite songs.
3. For question authoring, the default rule is:
   - use favorite songs first
   - make sure each ready favorite song has already contributed at least one retained question
   - only after the current ready favorite-song list has been covered may the workflow fall back to general song pools
4. Inside one favorite song, prefer `vocabulary_tokens.used_quiz_count = 0` for question authoring.
5. For flashcards, prefer `vocabulary_tokens.used_flashcard_count = 0`.
6. Only fall back to general song pools if the favorite-song cache has no suitable material or the ready favorite-song list has already been covered.

## Cost-Control Rule

After a song is in Favorite Songs:

- do not refetch the lyric page from the web for normal quiz/flashcard work
- do not rerun Sudachi morphology
- **do read the cached full lyrics from SQLite when whole-song context is needed**
- **do read cached background/commentary text when the task needs interpretation, theme, or賞析 support**

Use SQLite directly instead.

Example token pickup query:

```sql
SELECT *
FROM vocabulary_tokens
WHERE jlpt_level = 'N1'
  AND used_quiz_count = 0
ORDER BY RANDOM()
LIMIT 1;
```

Then use `sentence_id` to recover the original cached sentence.

For tasks that need broader context, read the cached `lyrics.full_text` for that song first, and if available also read cached commentary/appreciation text, instead of going back to the web.

## LLM Usage Rule

LLM is fallback-only here.

Allowed:

- generating quiz wording
- generating explanation wording
- rare JLPT-level fallback when rules/dictionaries cannot decide
- first-time processing of a newly liked song
- reading cached full lyrics when a whole-song question or better distractor design needs broader context
- reading cached commentary/appreciation text when interpretation/background is relevant

Not allowed as the default path:

- re-reading the whole lyrics page every time
- redoing morphology for an already cached favorite song
- using LLM to replace deterministic metadata/lyrics/token cache steps
