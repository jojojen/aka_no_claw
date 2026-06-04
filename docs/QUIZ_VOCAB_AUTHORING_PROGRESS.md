# /quiz vocabulary-card expansion progress

Last updated: 2026-06-04 JST

## Goal

- User target: continue authoring quiz-backed vocabulary until `/quiz vocab` has more than 300 `JLPT N1` Codex vocabulary cards.
- Current card source model: vocabulary cards are derived from `quiz_questions.tested_point` for Codex-authored lexical quiz rows.
- Lexical quiz types counted for vocab cards:
  - `漢字読み`
  - `言い換え類義`
  - `文脈規定`
  - `用法`

## Current Baseline

- Current `quiz_vocab_cards` count: 97.
- Current distinct Codex lexical `tested_point` count: 97.
- Target floor: 300 cards.
- Required new distinct lexical points: `300 - 97 = 203`.

## Reverse Plan

- One new unique lexical `tested_point` produces one vocabulary card.
- Therefore, minimum new quiz rows required: 203.
- Per-source quota remains: at most 3 Codex-authored questions per song/source.
- Minimum new song sources if using fresh sources only: `ceil(203 / 3) = 68`.
- Practical batch target:
  - 70 song sources.
  - 3 lexical quiz rows per source.
  - 210 new unique lexical points.
  - Expected final vocab card count: `97 + 210 = 307`.

## Source And QA Rules

- Prefer songs in `data/proseka_songs.json`, but non-list sources are allowed only when grounding is reliable.
- Keep at most 3 Codex-authored quiz rows per source.
- Each new quiz row must have:
  - `author='codex'`
  - `level='JLPT N1'`
  - non-empty `tested_point`
  - real `source_name`
  - source URL
  - grounded `source_excerpt`
- Each new vocabulary card must have:
  - `headword`
  - `reading_hiragana`
  - short Chinese gloss
  - one simple Japanese example sentence
  - related song/source URL
- Local model and web/API tools may help generate candidates, but final acceptance is based on Codex review plus DB validation.

## Implementation Notes

- Use `漢字読み` title-grounded rows when the song title itself contains a strong lexical item; this is the safest high-volume path because title grounding is explicitly supported for `漢字読み`.
- Use lyric-grounded rows when a reliable lyric URL and excerpt are available.
- Avoid adding vocabulary-card rows without a corresponding quiz row; the vocabulary book must remain quiz-backed.
- Current DB backfill behavior should preserve existing quiz-backed vocab-card rows even when their card metadata is stored in `quiz_vocab_cards` rather than in code seed data.

## Progress Log

- 2026-06-04 JST — Planning pass:
  - Baseline confirmed: 97 cards.
  - Need at least 203 new unique lexical quiz points.
  - Minimum source requirement: 68 fresh song sources at 3 questions each.
  - Working batch target set to 70 sources / 210 quiz rows / final 307 cards.
