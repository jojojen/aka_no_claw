# /quiz cross-agent review log (`quiz_reviews` table)

Last reviewed: 2026-06-20
Status: Current
Owner area: quiz

This is how review feedback flows between the agents that author `/quiz`
questions (currently **codex**, **Claude**, **qwen3:14b**). One agent reviews
another agent's questions and records a verdict + comment per question in the
**`quiz_reviews`** table inside `data/quiz.sqlite3`. **If you authored questions,
read the reviews left on them and act on the ones marked `revise` / `reject`.**

## Table: `quiz_reviews`

| column | meaning |
|---|---|
| `review_id` | uuid (PK) |
| `question_id` | FK → `quiz_questions.question_id` (the question being reviewed) |
| `question_author` | who wrote that question (`codex` / `Claude` / `qwen3:14b`) — denormalised for easy filtering |
| `reviewer` | who wrote the review (e.g. `Claude`) |
| `verdict` | one of `pass` / `minor` / `revise` / `reject` (see below) |
| `comment` | the reviewer's opinion: what's right, what's wrong, what to change |
| `created_at` / `updated_at` | ISO-8601 UTC |

`UNIQUE(question_id, reviewer)` — one verdict per reviewer per question. A
reviewer re-running an updated opinion **updates** the existing row, it does not
duplicate.

## Verdict vocabulary (fixed enum — do not invent new values)

| verdict | meaning | what you (the author) should do |
|---|---|---|
| `pass` | Correct and usable as-is. | Nothing. |
| `minor` | Usable, but a small blemish (typo, slightly-off framing, a touch too easy, redundant wording). | Optional polish; not blocking. |
| `revise` | A real defect that is **fixable**: weak/duplicate distractor, explanation wrong or unconvincing, source_excerpt not a genuine lyric, answer technically right but stem ambiguous, difficulty far off level. | Fix the cited problem and update the question. |
| `reject` | **Disqualifying** defect: the marked answer is wrong, more than one option is correct, the answer leaks from the stem, or the passage doesn't support the answer. | Do not ship as-is. Rewrite or delete. |

The **comment** always names the specific problem and, for `revise`/`reject`,
what to change. Read it — don't act on the verdict label alone.

## How to read the reviews on your questions

`reviewer='Claude'` reviews on codex-authored questions, only the actionable ones:

```sql
SELECT q.exam_point, q.source_name, q.stem, v.verdict, v.comment
FROM quiz_reviews v
JOIN quiz_questions q ON q.question_id = v.question_id
WHERE q.author = 'codex'           -- your questions
  AND v.reviewer = 'Claude'
  AND v.verdict IN ('revise','reject')   -- the ones that need work
ORDER BY v.verdict DESC, q.exam_point;
```

Drop the `verdict IN (...)` filter to see every review (including `pass`).

Coverage / verdict tally per exam_point:

```
.venv/bin/python /tmp/quiz_review_tool.py stats
```

(The helper that builds this table and inserts reviews lives at
`/tmp/quiz_review_tool.py`: `init` | `dump <exam_point> [author]` | `insert` |
`stats`. It is a scratch tool, not committed; the **table and its data live in
the DB** and persist.)

## After you fix a `revise` / `reject`

Update the question in place (same `question_id`) so the review still points at
it, then ping the reviewer to re-check. The reviewer re-running `insert` on that
`question_id` overwrites the old verdict (thanks to `UNIQUE(question_id,
reviewer)`), so the log always reflects the latest state.
