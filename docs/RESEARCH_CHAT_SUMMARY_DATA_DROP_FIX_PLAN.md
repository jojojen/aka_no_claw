# Research Chat Compact-Summary Data-Drop Fix Plan (lightweight scope)

Last reviewed: 2026-07-05
Status: Implemented (unit-tested; manual chat verification per §5 not yet run)
Owner area: research
Created: 2026-07-05
Issue: none filed yet — file one when picked up if the repo's convention requires it

Goal: chat mode's default `/research` reply (the "compact" text summary) currently
throws away the seller-reputation statistics (total review count, listing count,
positive/negative counts, positive rate) that the backend already computed —
it keeps only the one-line risk verdict. This doc specifies a **backend-only,
text-formatting fix**: make the compact seller bullet include those stats. It
deliberately does **not** touch job/button wiring, the frontend, or the
market-price search-recall gap (see §7 Out of scope) — that broader "full" fix
was considered and explicitly declined by the user in favor of this narrower one.

This doc is written to be implementable by a cold agent: every touch point
cites file + line as of 2026-07-05 (`research_command.py`, 2932 lines).

---

## 1. References

### 1.1 How this bug was found

- User's original complaint (this session): asked a chat-mode investment
  question about a Mercari PSA10 card
  (`https://jp.mercari.com/item/m55696205657`) and the answer read like it was
  missing seller/comp information.
- E2E proof captured at `/tmp/e2e_progress_turn1.log` (445s full run, staged
  progress + goal-loop narration — this run itself is *evidence the
  progress-visibility fix works*, not evidence of this bug; the compact-summary
  bug is a separate, still-open issue found by reading the code path it
  exercises). The run's final synthesized answer includes:
  `"賣家風險方面，seller_snapshot擷取失敗，無法評估賣家信譽"` — in that
  specific run the snapshot fetch itself failed (a different, transient
  problem: sample data unavailable, not a formatting bug). The formatting bug
  described here is broader and applies to **every** chat-mode `/research`
  reply, including successful snapshots: even when the seller snapshot
  *succeeds* and the backend has full stats, the compact summary text sent to
  chat only shows the risk verdict sentence and silently drops the stats.
- Root-cause trace (this session, by reading code, not by re-running):
  `format_research_compact_report` (used by chat's default `/research` reply)
  → `_build_compact_report_bullets` → `_compact_seller_summary` — the last
  function only keeps the risk-verdict part of `result.summary` and discards
  the rest. Confirmed by reading `_build_seller_snapshot_section_result`,
  which shows the discarded parts are real, already-computed data (not
  something that needs new fetching).

### 1.2 Two independent problems this session traced through; only one is this doc's scope

Both were raised by the same user question, but they are structurally
unrelated bugs at different points in the pipeline:

1. **Chat's `/research` call discards the job/markup channel entirely**
   (`_exec_registered_command_chat_tool`'s `CHAT_TOOL_RESEARCH` entry calls
   `self._run_command("/research", text)`, which drops `(text, markup)`'s
   markup half — see `_run_command` vs `_run_command_raw`,
   `command_bridge.py:774-790`). This means chat mode can never get follow-up
   detail buttons (賣家細節 / 市價細節 / etc.) the way Telegram or the
   research tab's job-based flow can. **Out of scope for this doc** — the
   user explicitly chose the lightweight (text-only) fix over this "full"
   job/button-wiring fix when asked to pick a scope.
2. **Even the plain text chat DOES get is thinner than it needs to be**,
   because `_compact_seller_summary` throws away data that's already sitting
   in `result.summary`. **This is this doc's scope.** No new data collection,
   no job wiring, no frontend change — purely a formatting fix to how an
   already-assembled string is trimmed for the compact reply.

---

## 2. Current-state map (all verified 2026-07-05, `research_command.py`)

| Concern | Where | Facts |
|---|---|---|
| Chat's compact reply entry point | `format_research_compact_report` (:1785-1807) | Called for the default (non-detail-view) `/research` reply; builds header lines + `_build_compact_report_bullets(report)` |
| Detail views (unaffected by this bug) | `format_research_detail_report` (:1810-1820) → `_format_research_seller_detail` (:1941+) | The rich "賣家細節" callback view already renders full section content via `_render_detail_section`; **not touched by this fix**, already correct |
| Bullet assembly | `_build_compact_report_bullets` (:1823-1842) | Builds ≤5 bullets: 市價 / 流動性 / 賣家 / 增值 / 注意；calls one `_compact_*_summary` helper per section |
| **The bug** | `_compact_seller_summary` (:1897-1904) | Splits `result.summary` on `"；"`, returns **only** the first part matching `快照顯示` / `快照資料不足` / `snapshot 失敗` (i.e. just the risk-verdict sentence), truncated to 76 chars. All other parts (stats) are read from `parts` but never appended. |
| Sibling helpers (reviewed, NOT bugged the same way) | `_compact_price_summary` (:1871-1886), `_compact_liquidity_summary` (:1889-1894) | `_compact_price_summary` already accumulates multiple matching parts (verdict + sold stats + active stats + 遊々亭 note) — the pattern this fix should copy. `_compact_liquidity_summary` takes `parts[:2]` (verdict + sample counts), dropping the ratio/sold-avg-reference lines — a much smaller, lower-value drop (the dropped lines are near-duplicates of what price/liquidity bullets already show elsewhere); **not fixed by this doc**, noted here only so a future reviewer doesn't rediscover it as a surprise (see §7). |
| Upstream data source (already computed, nothing new to fetch) | `_build_seller_snapshot_section_result` (:2073-2159) | Builds `summary_parts = [risk_text, negative_review_summary?, meta_bits_joined, seller_bits_joined, captured_at]` joined by `"；"` — see exact fields below |
| `meta_bits` construction | `_build_seller_snapshot_section_result` (:2084-2090) | `["賣家 {display_name}"]` + `["總評價 {total_reviews}"]` + `["刊登 {listing_count}"]`, each only if the source field is non-`None`; joined with `" / "` |
| `seller_bits` construction | `_build_seller_snapshot_section_result` (:2092-2098) | `["好評 {seller_positive}"]` + `["差評 {seller_negative}"]` + `["好評率 {seller_rate:.1f}%"]`, each only if non-`None`; joined with `" / "`, prefixed `"身為賣家："` at assembly time (:2146) |
| Truncation helper | `_truncate_research_text(text, limit)` (:2045-2049) | Generic; used per-bullet with a hardcoded `76` today. This fix widens the limit for the seller bullet only — no shared constant to touch. |
| Existing test precedent | `tests/test_research_command.py:1966-1983` | `test_compact_price_summary_includes_yuyu_note` — the style template: construct a `ResearchSectionResult` directly, call the compact helper, assert on substring presence. No test currently exists for `_compact_seller_summary`. |

### 2.1 Exact current buggy function

```python
# research_command.py:1897-1904 (current)
def _compact_seller_summary(result: ResearchSectionResult) -> str:
    parts = [_compact_whitespace(part) for part in result.summary.split("；") if part]
    for part in parts:
        if "快照顯示" in part or "快照資料不足" in part or "snapshot 失敗" in part:
            return _truncate_research_text(part, 76)
    if parts:
        return _truncate_research_text(parts[0], 76)  # verdict is always first
    return _truncate_research_text(result.summary, 68)
```

### 2.2 What `result.summary` actually contains when a snapshot succeeds

From `_build_seller_snapshot_section_result` (:2140-2148), `summary_parts` is
(in order): `[risk_text, negative_review_summary?, meta_bits_joined,
seller_bits_joined, captured_at_line]`, joined with `"；"`. A realistic example
string:

```
快照顯示賣家風險偏低；賣家 abc_seller_123 / 總評價 358 / 刊登 12；身為賣家：好評 350 / 差評 3 / 好評率 99.2%；快照時間 2026-07-05T10:00:00
```

`_compact_seller_summary` today returns **only**:

```
快照顯示賣家風險偏低。
```

— dropping the seller's total review count, listing count, and the
positive/negative/rate breakdown entirely, even though all of it was already
computed and is sitting right there in `parts`.

---

## 3. Fix specification

### 3.1 New `_compact_seller_summary`

Design intent: keep the verdict sentence first (unchanged behavior for the
"no stats available" / snapshot-failed case), then append the `meta_bits` part
and the `seller_bits` part **if present**, mirroring how
`_compact_price_summary` already accumulates multiple matching parts instead
of returning only the first one. Drop `negative_review_summary` and the
`captured_at` line from the compact bullet — those two are lower-value in a
5-bullet-budget compact reply and remain fully available in the "賣家細節"
detail view (`_format_research_seller_detail`, unaffected by this fix).

```python
# research_command.py:1897-1904 (proposed replacement)
def _compact_seller_summary(result: ResearchSectionResult) -> str:
    parts = [_compact_whitespace(part) for part in result.summary.split("；") if part]
    selected: list[str] = []
    for part in parts:
        if "快照顯示" in part or "快照資料不足" in part or "snapshot 失敗" in part:
            selected.append(part)
            break
    if not selected and parts:
        selected.append(parts[0])  # verdict is always first when present
    for part in parts:
        if part in selected:
            continue
        if part.startswith("身為賣家：") or "總評價" in part or "刊登" in part:
            selected.append(part)
    if not selected:
        return _truncate_research_text(result.summary, 68)
    return _truncate_research_text("；".join(selected), 140)
```

Notes on the exact matching logic (so the implementer doesn't have to
re-derive it):

- The `meta_bits` joined string looks like `"賣家 X / 總評價 Y / 刊登 Z"` (any
  of the three sub-parts may be missing depending on which snapshot fields
  were `None`) — matched by `"總評價" in part or "刊登" in part`. Using an
  OR of both substrings (rather than requiring both) is deliberate: a
  snapshot might have `listing_count` but not `total_reviews`, or vice versa;
  either alone still means "this part is the meta_bits line, not the verdict
  or the captured_at line."
  - Edge case: if `display_name` is the *only* populated meta field (no
    `total_reviews`, no `listing_count`), `meta_bits_joined` would be just
    `"賣家 X"` — this would NOT match either substring and would be silently
    skipped. This is an accepted, intentional trade-off: a bare seller-name
    fragment adds little value to a compact bullet the reader already sees
    labeled "賣家：", and reaching for it would require a broader match
    (`part.startswith("賣家 ")`) that risks false-positive matching the
    unrelated verdict-adjacent text. Do not "fix" this edge case without
    checking with whoever picks this up — it's a judgment call, not an
    oversight.
- The `seller_bits` joined string always starts with the literal prefix
  `"身為賣家："` (`_build_seller_snapshot_section_result:2146`), so
  `part.startswith("身為賣家：")` is an exact, unambiguous match — no
  substring-overlap risk with any other part.
- `negative_review_summary` (when present) sits between the verdict and
  `meta_bits` in the joined string but is never selected by either loop —
  it is intentionally dropped from the compact bullet (available in detail
  view only). No special-casing needed; the two matching loops simply don't
  match it.
- `captured_at` (e.g. `"快照時間 2026-07-05T10:00:00"`) is likewise never
  matched by either loop and is intentionally dropped from the compact
  bullet.
- Truncation limit raised from `76` to `140`: the combined verdict + meta +
  seller-bits string is meaningfully longer than a bare verdict sentence.
  `140` is a judgment call, not derived from a hard constraint — pick a
  number that keeps the bullet readable in a chat UI without cutting off
  the seller-rate figure, which is the single most decision-relevant number
  in the bullet. If `display_name` values in practice run very long (e.g. a
  raw marketplace seller ID), consider lowering this or dropping the
  `display_name` sub-fragment specifically — but do not add a keyword list
  or per-provider special case to do so (Rule G, `[[feedback_no_hardcode_use_llm_rag]]`
  does not directly apply to this pure-formatting code, but the spirit of
  "no hardcoded provider branching" still does — any length adjustment
  should be a single generic constant, not conditional on which marketplace
  the snapshot came from).

### 3.2 What does NOT change

- `_build_seller_snapshot_section_result` (:2073-2159) — untouched; it
  already computes and assembles everything needed. This is a pure
  presentation-layer fix at the compact-bullet boundary.
- `_format_research_seller_detail` (:1941+) and the "賣家細節" callback path
  — untouched; already shows full stats today.
- `_compact_price_summary` / `_compact_liquidity_summary` / `_compact_appreciation_summary`
  — untouched (see §7 for why `_compact_liquidity_summary`'s smaller drop is
  explicitly deferred, not silently ignored).
- No changes to `command_bridge.py`, no changes to the frontend, no changes
  to job/markup wiring, no changes to how `/research` acquires seller or
  price data.

---

## 4. Test plan

Add to `tests/test_research_command.py`, following the exact style of the
existing `test_compact_price_summary_includes_yuyu_note` (:1966-1983) —
construct a `ResearchSectionResult` directly (no need to run the full
research pipeline), call `_compact_seller_summary`, assert on substrings.

1. **`test_compact_seller_summary_includes_meta_and_seller_stats`** — build a
   `ResearchSectionResult` with
   `summary="快照顯示賣家風險偏低；賣家 abc123 / 總評價 358 / 刊登 12；身為賣家：好評 350 / 差評 3 / 好評率 99.2%；快照時間 2026-07-05T10:00:00"`;
   assert the compact output contains `"快照顯示賣家風險偏低"`, `"總評價 358"`,
   and `"好評率 99.2%"`; assert it does **not** contain `"快照時間"` (the
   captured_at line stays dropped, confirming the fix is additive/targeted,
   not "just show everything").
2. **`test_compact_seller_summary_falls_back_to_verdict_only_when_no_stats`**
   — regression guard for the pre-existing behavior: build a result with
   `summary="快照資料不足，需人工檢查 proof。"` (no meta_bits/seller_bits parts
   at all — the real shape when a snapshot fetch fails, matching the E2E
   proof log's `seller_snapshot擷取失敗` case); assert the output equals the
   truncated verdict sentence, unchanged from today's behavior.
3. **`test_compact_seller_summary_truncates_long_combined_stats`** — build a
   result whose combined verdict+meta+seller-bits string exceeds 140 chars
   (e.g. a long `display_name`); assert the output ends with `"…"` and is
   `<= 140` chars, confirming the widened truncation limit still bounds
   output length.

Run: `.venv/bin/python -m pytest tests/test_research_command.py -k compact_seller -v`
(single invocation, no heredoc, no env-var prefix — per collab rules).

---

## 5. Manual verification (optional but recommended)

If picking this up with bridge access:
1. Do not touch the manually-run bridge on port 8781 (`local.openclaw.telegram`
   stack) — verify on a throwaway port instead (e.g. 8799), per collab rules.
2. Re-run the same chat question from this session's E2E case
   (`投資角度分析這張卡要買嗎？ https://jp.mercari.com/item/m55696205657`) — or
   any `/research` query whose target has a real, successful seller
   snapshot — and confirm the compact chat reply's "賣家：" bullet now shows
   the total-review-count / rate figures, not just the verdict sentence.
   (Note: this specific card's snapshot failed in the earlier E2E run, so it
   won't demonstrate the fix — pick a target known to have a working
   snapshot, or mock one via the unit tests in §4 instead.)

---

## 6. Acceptance criteria

1. `_compact_seller_summary` includes seller total-review-count and
   positive-rate figures in its output whenever the underlying
   `ResearchSectionResult.summary` contains them.
2. The verdict-only fallback behavior (no stats available, e.g. a failed
   snapshot) is unchanged — same output as before this fix.
3. `negative_review_summary` and `captured_at` remain excluded from the
   compact bullet (unchanged — those stay detail-view-only, by design, not
   by omission).
4. New unit tests (§4) pass; no existing test in
   `tests/test_research_command.py` regresses.
5. No changes outside `research_command.py` (+ its test file) — confirms the
   fix stayed within the declared lightweight scope.

---

## 7. Out of scope (explicitly deferred, not forgotten)

- **Chat markup/job wiring** (§1.2 item 1) — chat mode's `/research` tool
  call still cannot carry follow-up detail buttons; would require adopting
  the existing job-backed pattern (`start_async`, `command_bridge.py:3062-3125`)
  for the chat path, plus a `StreamEvent` "done" variant schema change on the
  frontend (`types/command.ts:147`, currently `{type:"done", message, model_metadata?}`
  with no `actions`/`job_id` field) and `MessageBubble.tsx` consumer changes.
  This is the "full" fix option the user was offered and declined in favor
  of this lightweight one. If picked up later, treat it as a separate,
  larger plan doc — do not fold it into this one.
- **Market-price / comp-data recall gap** — the E2E proof log's
  `"market_prices未提供該SSP版本的任何價格資訊"` is **not** a formatting bug
  like the seller one; it traces to `_build_price_section_result`
  (research_command.py:2793-2800): when both `sold_average_jpy` is `None`
  and `active_prices` is empty, the section legitimately has no data to
  report (`"查詢「{query}」未取得可用的 sold 或 active 樣本。"`) — a genuine
  external-search-recall gap for that specific rare SSP card variant, not a
  compact-summary truncation bug. Fixing this would mean improving search
  coverage/query construction for rare variants, which is a materially
  different (and riskier — touches live external search behavior) piece of
  work. Do not conflate it with this doc's fix.
- **`_compact_liquidity_summary`'s smaller data drop** (§2, sibling-helpers
  row) — takes only `parts[:2]`, dropping the sold/active ratio line and the
  sold-average reference line. Lower value than the seller drop (the
  reference-price line duplicates information already shown in the 市價
  bullet), so left alone here. Worth a one-line fix later using the same
  "accumulate matching parts" pattern from §3.1 if someone wants to also
  address it — but it was not part of what the user asked to fix this round.

## 8. Progress log

- 2026-07-05: plan written after root-causing the bug via code reading (not
  a fresh repro run) during this session; user explicitly chose this
  lightweight (backend text-formatting only) scope over a broader
  job/button-wiring fix, and asked for a plan doc instead of direct
  implementation, to conserve the requesting agent's own token budget.
- 2026-07-05: user then pointed out the diagnosis was already the expensive
  part and the fix itself was small, so had the same agent apply it directly
  instead of handing it off. `_compact_seller_summary` changed exactly per
  §3.1; 3 new tests added per §4 (all pass); full `tests/test_research_command.py`
  re-run clean (90 passed, no regressions). §5 manual chat verification not
  run this round (would need a target card with a live, successful seller
  snapshot — this session's original card's snapshot itself had failed, a
  separate transient issue, not this bug).
