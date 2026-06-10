# Opportunity Agent Handoff

Last updated: 2026-05-16

## Phase Log

### Phase 1 - Architecture

Decision: keep `opportunity_agent` inside `aka_no_claw` instead of creating a new repo.

Reason:

- `aka_no_claw` already owns Telegram runtime, `.env`, launchctl startup, SNS integration, price integration, and reputation integration.
- A new repo would add dependency and startup complexity before the opportunity loop is proven.
- Files are modular enough to split later.

### Phase 2 - Core MVP

Implemented modules:

- `opportunity_models.py`: dataclasses and deterministic IDs.
- `opportunity_store.py`: SQLite schema for candidates, price checks, and recommendations.
- `opportunity_scoring.py`: threshold and score rules.
- `opportunity_pipeline.py`: dependency-injected pipeline.
- `opportunity_agent.py`: live adapters for SNS LLM extraction, price lookup, Mercari search, reputation snapshot, and Telegram notification.

### Phase 3 - Runtime Wiring

Implemented:

- CLI tool: `python -m openclaw_adapter opportunity-agent`
- One-shot mode: `python -m openclaw_adapter opportunity-agent --once`
- CLI status: `python -m openclaw_adapter opportunity-status`
- Telegram status: `/hunt status`
- Mac launchctl job: `local.openclaw.opportunity`
- Stop script support for the new job.
- `.env.example` opportunity settings.

### Phase 4 - Verification

Completed on 2026-05-13:

- Focused opportunity tests: `7 passed`
- Expanded OpenClaw tests: `45 passed`
- Full `aka_no_claw` tests: `191 passed, 7 skipped`
- Price monitor focused tests after natural-language fallback fix: `28 passed`
- CLI smoke: `opportunity-agent --once` exits cleanly with an empty SNS database.
- Shell syntax: `start-mac-mini-stack.command` and `stop-mac-mini-stack.command` pass `bash -n`.
- Live launchctl smoke: `local.openclaw.opportunity` starts and remains running.
- First live tick: read SNS data, extracted 4 candidates, rejected all before notification because no reliable fair value was found.
- Visibility check: `opportunity-status --limit 4` shows the current 4 monitored candidates.

One adjacent fix was made in `price_monitor_bot/src/price_monitor_bot/natural_language.py`:

- Generic "最近什麼熱門排行" now stays in the TCG trend path and returns `None` without a game instead of being misrouted to SNS buzz.

### Phase 5 - Candidate Name Cleanup

Completed on 2026-05-13:

- The SNS extraction prompt now explicitly tells the local text model to store only the tradable product name.
- Candidate parsing now strips non-product terms such as `抽選情報`, `予約情報`, `発売情報`, `Mercari`, and `メルカリ`.
- `セット名収録 カード名` patterns are normalized to the individual card name, for example `アビスアイ収録 ホエルオーex` becomes `ホエルオーex`.
- Obvious unsupported franchises such as `遊☆戯☆王`, `デュエルマスターズ`, and `ONE PIECE CARD GAME` are rejected even if the LLM mislabels them as pokemon/ws.
- Existing local candidate rows were normalized so `/hunt status` no longer shows `アビスアイ 抽選情報` as a product.
- Focused opportunity tests after this fix: `7 passed`.

### Phase 6 - Web Research Enrichment

Completed on 2026-05-14:

- The opportunity agent now wraps SNS candidate discovery with web research when the local Ollama text model is configured.
- For each SNS candidate, it searches Yahoo Japan via the shared Playwright web search helper (`search_yahoo_japan_playwright`) and asks the text model for a JSON relevance/demand assessment.
- Web research can boost candidate heat when outside sources support demand, or lower heat when sources look unrelated.
- Candidate metadata now stores the research query, assessment, and source URLs.
- Telegram opportunity recommendations include a `市場佐證` section with web reference URLs when available.
- Focused opportunity/web-search tests after this change: `20 passed`.
- Full `aka_no_claw` tests after this change: `220 passed, 7 skipped`.

### Phase 7 - Target Dismissal

Completed on 2026-05-14:

- Telegram now supports `/hunt remove <number-or-name>` for dismissing targets from `/hunt status`.
- Natural language routing supports requests such as `remove target 2 from the opportunity list` and `I am not interested in Umbreon ex SAR`.
- Dismissed candidates are marked `status='dismissed'` and remain hidden if the same candidate ID appears again in later SNS discovery.

### Phase 8 - Text-Side "Don't Guess Silently"

Completed on 2026-05-14:

- Mirrored the photo-side clarification flow for plain Telegram text. When the natural-language router returns `intent="unknown"` / `None`, or confidence < `TEXT_AMBIGUITY_CONFIDENCE_THRESHOLD` (0.55), the bot now stashes a `PendingTelegramTextClarification` and replies with numbered options (router top-guess + `/search` + `/help` etc.) instead of silently best-guessing.
- New dataclasses & methods in `price_monitor_bot/bot.py`: `TelegramTextIntentOption`, `PendingTelegramTextClarification`, `build_pending_text_reply_plan`, `_build_text_intent_candidates`, `_is_text_intent_ambiguous`. Override via `否，[your intent]` re-enters the router (nested-clarify allowed once).
- 5 new tests in `tests/test_telegram_bot.py` cover unknown→options, low-conf→options, numeric selection executes intent, `都不是` sentinel, override re-routing.

### Phase 9 - trend_board Misroute Fix & Fallback Keyword Slim

Completed on 2026-05-15:

- Bug: 「幫我查寶可夢卡現在是不是在跌」 routed to `/trend pokemon 5` because the LLM read 在跌 as "trending."
- LLM prompt rewritten: `trend_board` now restricted to explicit ranking/top-N requests; `web_research` explicitly covers price-direction / market-sentiment / why-how questions. New global "honest uncertainty beats a confident wrong guess" instruction tells the model to drop confidence below 0.5 when ambiguous.
- `_TREND_KEYWORDS` removes the misleading `"趨勢"` (means *direction* in Chinese, not *ranking*).
- Slimmed six bloated fallback keyword tables (`_TREND_KEYWORDS`, `_SNS_BUZZ_KEYWORDS`, `_STATUS_KEYWORDS`, `_TOOLS_KEYWORDS`, `_WEB_RESEARCH_QUESTION_KEYWORDS`, `_WEB_RESEARCH_SUBJECT_KEYWORDS`, `_OPPORTUNITY_REMOVE_KEYWORDS`) from ~100 entries to ~50, removing synonym sprawl / simp-trad duplicates. Each cut validated by a per-intent before/after probe; canonical phrasings still route correctly, non-canonical synonyms now fall through to the LLM.
- New `test_fallback_router_canonical_phrases` parametrised test locks in the canonical set; `test_fallback_router_no_longer_overeats_with_sns_buzz` confirms `遊戲王最近熱門排行` is no longer eaten by sns_buzz.

### Phase 10 - Immediate Intake Ack

Completed on 2026-05-15:

- Every incoming Telegram message now gets an immediate `已收到圖片，開始解讀使用者意圖` / `已收到訊息，開始解讀使用者意圖` ack before the downstream pipeline runs. Sent in `handle_telegram_message` right after the authorization check, so the user knows the bot is on the case even when vision/LLM chains take several seconds.
- All existing reply-sequence test assertions updated to include the intake ack as the first entry; no other behaviour changed.

### Phase 11 - Photo-Lookup Four-Layer Defence (Pikachu→Charizard fix)

Completed on 2026-05-15:

- Bug: a clear Pikachu photo (`/scan pokemon`) confidently returned a Mega Charizard X EX price.
- Root cause: the vision text-focus prompt literally contained `110/080` as a few-shot example collector number; the model regurgitated it whenever it could not read the actual digits. The hallucinated number matched a real Mega Charizard in the catalog and the pipeline trusted it without sanity checks.
- Layer 1 — prompt hardening: removed every literal collector-number example from `local_vision.py`'s `_build_prompt` / `_build_text_focus_prompt` / `_build_sealed_box_title_prompt`; added explicit "set the field to null and confidence ≤ 0.4 if you cannot directly read it" instructions; added a snapshot test asserting no `110/080` / `201/165` / `085/SV-P` / `020/M-P` / `UAPR/EVA-1-71` strings leak back in.
- Layer 2 — two-tier confidence floor in `image_lookup.py`: `IMAGE_HARD_FLOOR=0.4` skips the catalog lookup entirely; `IMAGE_SANITY_FLOOR=0.55` runs the lookup but requires the sanity check to pass. `(title=None AND card_number=None)` is treated as below the hard floor regardless of self-reported confidence (VLMs over-report on exactly this failure mode).
- Layer 3 — post-lookup sanity check: new `verify_card_identity` method on `OllamaLocalVisionClient` uses a forced-disconfirmation prompt ("list three features that must be present, then check"); structured JSON output with `match / evidence / mismatch_reasons / confidence`. A `match=yes` with empty `evidence` auto-demotes to `uncertain`. The matched `card_number` is only echoed as a question, never as an assertion, so we don't re-introduce the few-shot leak we just fixed.
- Layer 4 — web_research-assisted clarification: when status is `unresolved` / `rejected_sanity`, `default_photo_renderer` routes the parsed visual cues through the existing `/search` pipeline and returns a `PhotoLookupReply` that installs `PendingTelegramPhotoClarification`, so the user can steer via the standard `否，[your intent]` override.
- 7 new tests cover the prompt snapshot, both hard-floor branches, sanity demotion of evidence-less yes, verify prompt non-assertion of card_number, and a full sanity-rejection E2E.

### Phase 12 - Three-Level Candidate Restructure (IP / product_type / specific product)

Completed on 2026-05-15:

- Bug: `/hunt status` showed `[pokemon] インフェルノX・スタートデッキ100` as one candidate because two distinct products got merged.
- Root cause: `OpportunityCandidate` only had `game` + `title`; same IP + free-text title gave the LLM no structural reason to split.
- `OpportunityCandidate` now carries three layers: `game` (IP), `product_type` (constrained enum: `single_card / booster_pack / sealed_box / starter_deck / promo / other` with `normalize_product_type` alias mapper), and `title` + optional `product_identifier` (card number / set code). `build_candidate_id` hashes all of them, so two records with the same name but different `product_type` get distinct IDs and can never collapse.
- SQLite schema gained `product_type` (NOT NULL DEFAULT 'other') and `product_identifier` columns. `bootstrap()` detects legacy schemas (no `product_type` column) and drops + rebuilds the three opportunity tables so prod migrates cleanly on next boot — cron tick repopulates within ~1 hour.
- SNS LLM extraction prompt rewritten to require the three-level output with explicit split rule (`インフェルノX・スタートデッキ100 抽選情報` → two candidates) and a counter-example to preserve product-internal `・` (`ピカチュウ・カビゴンex`). Parser logs telemetry when a candidate title still contains a multi-product separator after extraction.
- `format_opportunity_status` renders `[game / product_type] title (identifier)` with `(XXX)` for single cards and `[set-code]` for sealed boxes / booster packs.
- 7 new tests cover the three-level extraction, multi-product split, alias normalisation, id divergence per product_type, status rendering, prompt snapshot guard, and the legacy-schema drop-and-rebuild migration.

### Phase 13 - Multi-Source Candidate Providers + SNS Domain Tags + Auto-Discovery

Completed on 2026-05-16:

- Goal: stop relying solely on whatever the user happens to follow on X. Add structured sources, and let the bot self-onboard TCG accounts.
- **`HotCardBoardCandidateProvider`** — reuses `TcgHotCardService.load_boards()` (the data behind `/trend`) to emit `single_card` candidates per game with real card_number / rarity / set_code / hot_score. `source_kind="hot_card_board"`. Zero new external APIs.
- **`ScheduledWebSearchCandidateProvider`** — runs a small batch of TCG-trend queries via Yahoo Japan Playwright search (`search_yahoo_japan_playwright`), feeds snippets to the same LLM extraction path via a new snippet-flavoured prompt (`_build_web_trend_candidate_prompt`), and emits sealed_box / starter_deck / booster_pack signals the hot-card board doesn't expose. `source_kind="web_trend_search"`. Default queries cover pokemon / yugioh / ws / union_arena and are env-overridable via `OPENCLAW_OPPORTUNITY_WEB_TREND_QUERIES`.
- **`ChainedCandidateProvider`** composes the providers, dedupes by `candidate_id` (higher heat wins on collision), ranks by `heat_score`, then truncates to `limit`.
- **SNS rule `domains` field** — replaces the deny-list design. `AccountWatch / KeywordWatch / TrendWatch` in `sns_monitor_bot` now carry `domains: tuple[str, ...]`, persisted into `watch_rules.query_json`. The TCG opportunity agent's `SnsLlmCandidateProvider._read_recent_posts` JOINs `watch_rules` and filters to rows where `domains ∩ TCG_DOMAINS` ({pokemon, yugioh, ws, union_arena, tcg}) is non-empty. User can keep following `@realDonaldTrump` for `[politic, stock]` without polluting TCG candidates.
- **`/snsadd` accepts labelled brackets** — `filter[抽選] domain[pokemon, ws]` for account, keyword, and trend rules. Re-running `/snsadd @existing_handle domain[…]` becomes an upsert (preserves filter when not re-specified, replaces domain). Legacy `["buy","sell"]` JSON-array filter form still works. `/snslist` now shows each rule's `filter[…]` and `domain[…]` tags; rules still awaiting backfill display `domain[?]`.
- **`opportunity_sns_domain_backfill`** — new module called from the opportunity agent's preflight (one rule per cron tick). For each `domains=()` rule, peeks at the rule's recent tweets, asks the LLM to pick 1–3 tags from `RECOMMENDED_DOMAINS`, saves the updated rule, and sends a Telegram heads-up `🏷 自動標記 @X 領域：…`. Legacy six rules in prod were tagged within a few ticks.
- **`opportunity_sns_discovery`** — new module called from the same preflight on a 6-hour interval. Runs `site:twitter.com 抽選 / 新弾 / Pokemon TCG restock` etc. queries → handle regex (skips protected paths `status / i/ / hashtag/ / search`) → LLM relevance + domain probe → `save_watch_rule(AccountWatch(... , domains=...))` when `is_tcg=true && confidence ≥ 0.7 && domains ∩ TCG_DOMAINS != ∅`. Cap `OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_MAX_NEW_PER_RUN` (default 2) and Telegram notification on each save.
- 12 new tests cover provider behaviour, chain dedup, domain intersection filter, AccountWatch round-trip, labelled-bracket parser, legacy JSON filter compat, domain backfill, handle-regex protected paths, auto-discovery confidence cap.
- First live tick (after restart 2026-05-16) populated 16 candidates: 7 `hot_card_board` (Pokemon SAR / Union Arena / WS sign cards), 3 `web_trend_search` (Pokemon Start Deck / Yu-Gi-Oh QCCP / Union Arena 天元突破), plus 5 demo seeds and earlier SNS-LLM rows. Backfill has already tagged 5/6 existing watch rules; `@realDonaldTrump` correctly classified as `[stocks, politics]` and is excluded from TCG processing.

## Important Runtime Notes

- The agent is recommendation-only. It does not buy anything.
- It needs the local text model through Ollama for SNS product extraction, web-trend candidate extraction, domain backfill, and SNS account auto-discovery.
- It reads `seen_tweets` from `SNS_DB_PATH` and **filters by `watch_rules.domains` intersection with `TCG_DOMAINS`** (`pokemon / yugioh / ws / union_arena / tcg`). Rules with no `domains` are invisible to the TCG agent until backfill tags them.
- Candidates come from three providers chained behind a `ChainedCandidateProvider`:
  1. `SnsLlmCandidateProvider` — domain-filtered SNS tweets → LLM extraction.
  2. `HotCardBoardCandidateProvider` — `/trend` hot-card boards → single_card candidates.
  3. `ScheduledWebSearchCandidateProvider` — periodic TCG-trend Yahoo Japan (Playwright) queries → LLM extraction for sealed_box / starter_deck / booster_pack.
- A preflight step runs before every tick: one-rule-per-tick domain backfill, plus a 6-hour-interval auto-discovery that may add new TCG accounts (max 2 / run, confidence ≥ 0.7, domains must intersect TCG_DOMAINS). Both flows notify the user via Telegram on each change.
- `reputation_snapshot` is still required for seller checks. Snapshot timeouts (240 s) per listing are the slowest part of a tick.
- `/hunt status` rows are formatted as `[{game} / {product_type}] {title}{identifier}`; `/snslist` rows include `filter[…] domain[…]`.
- Remove unwanted targets with `/hunt remove <number-or-name>`. Adjust rules with `/snsadd @X filter[…] domain[…]` (upsert) or `/snsdelete @X`.

## Useful Commands

Run one tick:

```bash
cd /Users/jen/ai_work_space/related_to_claw/aka_no_claw
PYTHONPATH=src ./.venv/bin/python -m openclaw_adapter opportunity-agent --once
```

Run focused tests:

```bash
cd /Users/jen/ai_work_space/related_to_claw/aka_no_claw
PYTHONPATH=src ./.venv/bin/pytest tests/test_opportunity_agent.py -q
```

Start the full Mac stack:

```bash
cd /Users/jen/ai_work_space/related_to_claw/aka_no_claw
./launchers/start-mac-mini-stack.command
```

## Next Handoff Target

In priority order:

1. **Per-vendor restock-page scrapers** (Yuyutei / Cardrush / Magi "新商品" + "再販" pages). Most structured data source we don't tap yet. Each vendor needs its own parser; add as a fourth provider that returns sealed_box / booster_pack candidates. Wiring point: a new class alongside `HotCardBoardCandidateProvider` and `ScheduledWebSearchCandidateProvider` in `opportunity_agent.py`, then chain it in `build_opportunity_agent()`.
2. **Telegram control commands** for the opportunity loop: `/hunt pause`, `/hunt resume`, `/hunt summary`. Parsing in `price_monitor_bot/src/price_monitor_bot/bot.py`; runtime in `aka_no_claw/src/openclaw_adapter/telegram_bot.py`.
3. **Provider concurrency** — chain provider currently runs sequentially. Web search + LLM extraction per provider can take 30+ seconds; thread-pool / asyncio would shorten the tick.
4. **Auto-cleanup of low-yield SNS accounts** — accounts that never produce candidates after a long lookback could be disabled or domain-tagged differently. Observe first.
5. **Second-pass LLM verifier for separator-containing candidate titles** — telemetry log added in Phase 12 will tell us if the prompt fix in Phase 12 is enough; if not, wire a per-candidate "is this one product or N?" probe.

## Live State Snapshot (2026-05-16)

For someone reading cold:

- Active candidate count: 16 (real + 5 demo). Demo rows have `source_kind="demo"` and can be `/hunt remove`-d.
- Six SNS rules: five tagged with domains (one of them backfilled this session). Trump correctly excluded from TCG agent via `[politic, stock]` tags.
- All three repos pushed to remotes: `aka_no_claw@macminim4 ccbb56c`, `price_monitor_bot@master 42cbf16`, `sns_monitor_bot@main 774b0a8`.
- Test counts: `aka_no_claw 256 passed / 7 skipped`, `price_monitor_bot 153 passed / 7 skipped`, `sns_monitor_bot 19 passed / 5 pre-existing asyncio-runner failures unrelated to recent work`.
