# /quiz vocabulary-card expansion progress

Last updated: 2026-06-07 JST

## Goal

- User target: continue authoring quiz-backed vocabulary until `/quiz vocab` reaches at least 800 `JLPT N1` Codex vocabulary cards.
- Current card source model: vocabulary cards are derived from `quiz_questions.tested_point` for Codex-authored lexical quiz rows.
- Lexical quiz types counted for vocab cards:
  - `漢字読み`
  - `言い換え類義`
  - `文脈規定`
  - `用法`

## Development Priority Order

The user has fixed the development priority order below. These are **not**
parallel goals; lower items must not override higher ones:

1. `正確性/品質`
2. `不被封鎖 IP`
3. `節省 token 消耗`
4. `速度`

Operational meaning:

- Do not take a faster path if it lowers question quality, grounding quality,
  or vocab-card example quality.
- Do not re-fetch or hit external lyric/commentary sources more than necessary
  just to save tokens or move faster.
- Use token-saving tactics only after quality and IP-safety are preserved.
- Treat speed as the final optimization, never the driver.

## Current Status

- Current clean `quiz_vocab_cards` count after source-excerpt example enforcement: 300.
- Current Codex lexical question-row count: 853.
- Current distinct `tested_point` count: 827.
- Current `vocab_seed` table count: 666 (DB-backed, `quiz_vocab_seed.py` has been deleted).
- Current ready `favorite_songs` count: 28.
- Current target floor: 800 cards.
- Remaining clean vocabulary cards needed from the current baseline: 500.
- Status: in progress.

## Reverse Plan

- One new unique lexical `tested_point` produces one vocabulary card.
- Therefore, from the current clean baseline, minimum new unique lexical quiz rows required: 546.
- Per-source quota remains: at most 3 Codex-authored questions per song/source.
- If every new source yields the full 3 retained lexical rows, the theoretical minimum fresh source requirement is `ceil(546 / 3) = 182`.
- Practical batch target for safety:
  - 205 song/commentary sources in total across the rest of the run.
  - 615 new lexical quiz rows.
  - Expected duplicate / borderline / reject buffer: about 46 rows.
  - Expected final vocab-card count after the full clean run: `254 + 615 - buffer ≈ 800+`.

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
  - one memory-helpful Japanese example sentence
  - related song/source URL
- Card examples must come from a real, traceable lyric/article/commentary `source_excerpt`. Do not count a card if the example sentence cannot be tied back to actual source text.
- Card examples must stay short: at most 3 sentences, and in practice usually a single short line/clause that helps memorization. Do not dump an entire commentary paragraph or multi-line passage into one card.
- Do not reuse the same example sentence across multiple cards. Even for similar words, do not use the same sentence template with only the target word swapped. Choose real source excerpts that distinguish each word's actual usage, nuance, or context.
- Do not accept low-value examples that merely say the learner remembered, heard, searched, studied, or thought about the word, such as `「X」という言葉が心に残った。`, `Xという言葉を覚えた。`, `Xの意味を調べた。`, or `Xについて考えた。`. That blocked list is the FLOOR, not the bar — judge memorability yourself.
- **The example must teach REAL USAGE — no fragmentary, context-broken (文意破碎), memory-worthless sentences (user directive 2026-06-08).** Quality/correctness is NOT only the question being right — the example itself is part of the bar: it must actively AID MEMORY, and ideally pin down the word's 語感 so the learner can distinguish it from its 類義語 (e.g. 妥協 vs 譲歩, 凶暴 vs 乱暴, 偽装 vs 偽造). A bland "X happened" sentence is a MISS even when grounded and grammatical. Concretely:
  - DEFAULT: use the FULL lyric line / couplet as `source_excerpt`, never a 3–4 char bare word fragment. BAD: `「妥協大好き」`, `「此処は宴」`, `「生真面目そうな」`. GOOD: `「此処は宴　鋼の檻　その断頭台で見下ろして」`.
  - CRYPTIC FALLBACK: when even the full lyric line is too cryptic to show standard usage, author a natural N1 sentence with concrete, memorable context (e.g. `「双方が一歩も妥協せず、交渉は決裂した。」`), kept to a single clause; `source_name` still cites the song.
  - `example_ja` is auto-derived from the primary question's `source_excerpt` (shortest usable line) and OVERWRITTEN every bootstrap, so fix the example by fixing the excerpt, not the card row.
  - Full rationale + worked examples: see `ai/skills/quiz-authoring-workflow/authoring_spec.md` → "Vocab-card example QUALITY".
- Local model and web/API tools may help generate candidates, but final acceptance is based on Codex review plus DB validation.
- qwen solver-check stability rule:
  - Prefer local `qwen3:14b` as the independent solver for objective `漢字読み` items.
  - If multi-question batches return empty output, malformed JSON, or otherwise unstable answers, do not accept the whole batch.
  - Retry by sending one question at a time to qwen. This is slower, but it improves answer/format stability and is the approved fallback for high-confidence retention.
  - Faster local models such as `qwen2.5:0.5b`, `qwen2.5-coder:7b`, and `gemma3:12b` were spot-checked on basic readings and made mistakes, so they should not replace `qwen3:14b` as the gate without a new calibration pass.

## Implementation Notes

- Use `漢字読み` title-grounded rows only when the song title contains a natural standalone vocabulary item that a learner could use outside the song title.
- Do not use mechanical title slices merely to increase the count. Reject pure song titles, full title phrases, kana-particle fragments, connective fragments, basic words, and title chunks that are not natural standalone vocabulary items. If the song title or lyric line does not provide enough good vocabulary, switch to cached or reliable background/commentary/appreciation material and use a question type that can be legally grounded by that source.
- Before inserting a question for a new headword, register it in the `vocab_seed` table:
  ```python
  db.upsert_vocab_seed(headword, reading_hiragana, zh_gloss_short)
  ```
  `quiz_vocab_seed.py` has been deleted; the `vocab_seed` table is the sole source of reading/gloss for backfill. Without a seed row, no vocab card will be created for that headword.
- Current DB backfill requires the primary quiz row's real `source_excerpt` as `quiz_vocab_cards.example_ja` and marks it `example_source_kind='source_excerpt'`. Adapted/template examples are not counted.
- Do not create placeholder Chinese glosses or readings. Placeholder seed rows (empty gloss, empty reading) will cause backfill to skip the headword silently.
- Use lyric-grounded rows when a reliable lyric URL and excerpt are available.
- Avoid adding vocabulary-card rows without a corresponding quiz row; the vocabulary book must remain quiz-backed.
- The backfill fallback (when no seed row exists) reads reading/gloss from the existing card in `quiz_vocab_cards`, so existing cards are preserved across re-backfills even without a seed row.
- Favorite-song cache is now the first source layer. While `favorite_songs.status='ready'` still has under-quota items, author from that pool first before falling back to the general song pool.

## Progress Log

- 2026-06-04 JST — Planning pass:
  - Baseline confirmed: 97 cards.
  - Need at least 203 new unique lexical quiz points.
  - Minimum source requirement: 68 fresh song sources at 3 questions each.
  - Working batch target set to 70 sources / 210 quiz rows / final 307 cards.
- 2026-06-04 JST — Title-grounded expansion batch:
  - Inserted a large Codex `漢字読み` batch using song-title grounding.
  - Source pool used two layers:
    - under-quota songs already present in `data/proseka_songs.json`
    - additional reliable Vocaloid song sources from VocaDB ranking pages when the Proseka-only pool was insufficient
  - Fast-path vocabulary cards were written directly into `quiz_vocab_cards` immediately after each accepted quiz row so backfill could preserve them on later inserts.
  - End state confirmed in DB:
    - `quiz_vocab_cards = 300`
    - `distinct codex lexical tested_point = 302`
    - `codex lexical question rows = 325`
  - During the same pass, several obviously too-basic qwen lexical rows were removed from the pool:
    - `味方`
    - `最高`
    - `全て`
    - `嫌`
    - `数センチ`
  - Added/updated authoring knowledge:
    - `漢字読みは単漢字や基本語を避ける`
- 2026-06-05 JST 03:18 — Favorite-song-first lexical batch:
  - User raised the next target from 300 to 800 cards.
  - Recomputed the run plan from the live DB baseline:
    - `quiz_vocab_cards = 312`
    - `distinct codex lexical tested_point = 314`
    - `codex lexical question rows = 338`
    - `ready favorite_songs = 20`
    - `favorite remaining slots = 44`
    - remaining unique lexical points to 800 = `488`
  - Inserted 13 new Codex lexical quiz rows from cached favorite-song material, of which 12 became net-new vocabulary cards because `彗星` already existed:
    - `898ecb30f057d434bb8c87ab103a083bdd0dae2b` — `反逆者の僕ら` / `反逆者` / `漢字読み`
    - `959aa106d99ca4922211cfbf01f632ca53da248f` — `反逆者の僕ら` / `囚われる` / `言い換え類義`
    - `1d7410b02eb4a1aa065825cbdd7ee9218a0f074a` — `反逆者の僕ら` / `解き放つ` / `言い換え類義`
    - `5c91f5f13ada5994d48828589e2d50a548a3f198` — `芽吹くとき` / `芽吹く` / `漢字読み`
    - `4b7c574f9e34bd10b099369b6aa506ba8f4e0494` — `芽吹くとき` / `勘繰る` / `言い換え類義`
    - `e04008f3fb8380f7859f130069994d9b4e23e37b` — `Ray` / `彗星` / `漢字読み`
    - `8fa165f11be854270432a1f96e1a1f95b4ee5990` — `ただ君に晴れ` / `爆ぜる` / `言い換え類義`
    - `941526d8505d67ed714099eec99416aadca265a0` — `ただ君に晴れ` / `噤む` / `言い換え類義`
    - `6b52b7e097a4ff5963b74519217f30667f819d7b` — `ただ君に晴れ` / `俯く` / `言い換え類義`
    - `965b20858e37f7fc43bc2bb88c7bf4caec1fac52` — `モニタリング` / `涸れる` / `言い換え類義`
    - `c21abadf998a1fd2566943774241848d28507f97` — `モニタリング` / `舐め取る` / `言い換え類義`
    - `b470a55b11eae0f0d5127654df1b0550db5113df` — `モニタリング` / `飲み干す` / `言い換え類義`
    - `437166a2449e5a3b0ec99063f65ccd3ae5277961` — `One Last Kiss` / `喪失` / `言い換え類義`
  - The following new headwords were added to `quiz_vocab_cards` in this batch:
    - `反逆者`
    - `囚われる`
    - `解き放つ`
    - `芽吹く`
    - `勘繰る`
    - `爆ぜる`
    - `噤む`
    - `俯く`
    - `涸れる`
    - `舐め取る`
    - `飲み干す`
    - `喪失`
  - Marked the corresponding cached favorite-song tokens as `used_quiz_count += 1` for all lyric-grounded rows.
  - End state after this batch:
    - `quiz_vocab_cards = 312`
    - `distinct codex lexical tested_point = 314`
    - `codex lexical question rows = 338`
  - Next exact step:
    - continue exhausting under-quota ready favorite songs first, prioritizing unused high-quality lexical items such as `封じ込める`, `塗り替える`, `喪失`, `軌跡`, and comparable cache-grounded words before switching back to the general song pool.
- 2026-06-05 JST — Favorite-song-first solver-check batches:
  - Restored the required local-model check: every candidate in this pass was independently solved by local `qwen3:14b` before retention, and only rows with matching answers were kept.
  - Inserted 11 retained rows from cached favorite songs:
    - `5b83586cd80c0d667707bce6cf9bb49d57e604ab` — `だから僕は音楽を辞めた` / `防衛本能` / `言い換え類義`
    - `eadf226dd43819dd12003b59284fe56890554d66` — `だから僕は音楽を辞めた` / `劣等感` / `言い換え類義`
    - `bd8a1f205d860c18f545ccea09b6d4c4c5f0d420` — `ビターチョコデコレーション` / `偽善` / `言い換え類義`
    - `210de12126844ad1873eedcb79793b79f93b06a0` — `ビターチョコデコレーション` / `晒す` / `漢字読み`
    - `271d46c68f3c90e8205617a878a7c0b1e1e6e60e` — `ビターチョコデコレーション` / `讃える` / `言い換え類義`
    - `45067f5dd783601e0ee75869267acd42175d8fc1` — `勇者` / `御伽` / `漢字読み`
    - `a6705e813c1c15845e3dd10699eef62da8bb62cd` — `勇者` / `無情` / `言い換え類義`
    - `e829883368764247dfac3fded3573ad9039ef653` — `勇者` / `錆び付く` / `言い換え類義`
    - `974a3dbac467da32fd7949cac15e601b43aa448d` — `スピカ` / `仄か` / `漢字読み`
    - `c3686061e96b19968eeeae4ff65483606948b8e9` — `スピカ` / `酩酊` / `漢字読み`
    - `1cebc140aee594f38992918a0ec5db45669c8383` — `君はロックを聴かない` / `焦がれる` / `言い換え類義`
  - Inserted a second solver-checked batch from additional untouched favorite-song sources:
    - `df60f6f068a249d0845262a047fc59994342ae28` — `バグ` / `退路` / `言い換え類義`
    - `1e75c503a7f63452a00e60a3f712e49adf7c717c` — `バグ` / `絶体絶命` / `言い換え類義`
    - `78a13031d53503b041fbcd5aa8b5ffce5d3213cf` — `バグ` / `悲哀` / `言い換え類義`
    - `1fbbad6e90deff6ded2af431f92c848c8261c8e5` — `シリウス` / `絶望` / `言い換え類義`
    - `fd2bcd1c88b9199820acbf5a33b24d9df35ad1c6` — `帰り道は遠回りしたくなる` / `黄昏る` / `漢字読み`
  - One solver-checked candidate was explicitly rejected after self-review and removed:
    - deleted `42d0df1994ac058c193056eac2f3f401807a1caf` — `バグ` / `剪定` / `漢字読み`
    - reason: the lyric excerpt itself already included `(せんてい)`, so the reading item leaked the answer and also would have pushed `バグ` past the 3-question source quota.
  - Added authoring knowledge from that rejection:
    - `漢字読み題不能直接引用原文自帶的讀音標註`
  - For every retained row in this pass:
    - corresponding `quiz_vocab_cards` entries were written directly, including hiragana reading, short zh-TW gloss, lyric example sentence, song URL, and linked primary question ID
    - cached `vocabulary_tokens.used_quiz_count` was incremented for the selected lyric-grounded token
  - End state after these solver-check batches:
    - `quiz_vocab_cards = 332`
    - `distinct codex lexical tested_point = 316`
    - `codex lexical question rows = 328`
  - Current favorite-song source coverage snapshot:
    - `だから僕は音楽を辞めた = 2`
    - `シリウス = 1`
    - `スピカ = 2`
    - `バグ = 3`
    - `ビターチョコデコレーション = 3`
    - `勇者 = 3`
    - `君はロックを聴かない = 1`
    - `帰り道は遠回りしたくなる = 1`
  - Next exact step:
    - continue with untouched or still-under-quota ready favorite songs first, preferring reliable lyric URLs and strong N1 candidates from `例えば`, `そんなもんね`, `君はロックを聴かない`, `スピカ`, `だから僕は音楽を辞めた`, and other remaining cached songs before returning to the general pool.
- 2026-06-07 JST — Under-quota favorite-song fill + vocab_seed design fix:
  - Filled under-quota ready favorite songs: `恋するフォーチュンクッキー`, `ray`, `MARIGOLD`, `携帯恋話`, `例えば` — 3 questions each (12 net new questions).
  - All 12 passed independent `qwen3:14b` solver check.
  - 12 new vocab cards created via backfill with source_excerpt examples.
  - Skipped `アイドル` (wrong lyrics in DB — parody, 137 chars), `私は、わたしの事が好き。` (inline furigana format).
  - **Architecture change**: `quiz_vocab_seed.py` deleted. All 630 seed entries migrated to `vocab_seed` DB table. `_backfill_vocab_cards()` now queries `vocab_seed` instead of the Python dict. New public API: `db.upsert_vocab_seed(headword, reading_hiragana, zh_gloss_short)`. Tests and SKILL updated accordingly.
  - End state:
    - `quiz_vocab_cards = 271`
    - `distinct codex lexical tested_point = 827`
    - `codex lexical question rows = 853`
    - `vocab_seed table = 630`
  - **Next exact step**: Continue authoring new lexical questions and registering seeds (via `db.upsert_vocab_seed`) until 800 cards. Current gap: 500 cards. Sources: under-quota ready favorite songs first, then general `data/proseka_songs.json` pool. Existing 386 title-type 漢字読み questions cannot generate vocab cards (source_excerpt_type='title'); those headwords need fresh questions with lyric/article grounding to get cards.
- 2026-06-07 JST — Seed batch fill to 300 cards:
  - Added 36 seed rows to `vocab_seed` table for existing questions that had no seed.
  - All 36 entries verified to have non-title source_excerpt with headword verbatim AND memory-helpful examples.
  - Criteria: N2+ vocabulary only; excluded N3/N4 basics; excluded meta-commentary article examples longer than useful; preferred short lyric-line examples.
  - Backfill ran once after batch insert. Result: `quiz_vocab_cards = 300`.
  - Examples: つま先, 一筋縄, 交わす, 共鳴, 尽きる, 彗星, 得体, 憧れ, 来世, 盲動, 陽炎, 炎天, etc.
