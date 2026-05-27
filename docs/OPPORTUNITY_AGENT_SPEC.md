# OpenClaw Opportunity Agent Spec

Status: production. Last updated 2026-05-16.
Owner layer: `aka_no_claw` integration runtime
Runtime command: `python -m openclaw_adapter opportunity-agent`
Status command: `python -m openclaw_adapter opportunity-status`
Telegram status: `/hunt status`
Telegram dismiss: `/hunt remove <number-or-name>`

## Goal

Continuously find buying opportunities by combining several signals:

1. **Three candidate providers** (chained, deduped by candidate_id):
   - `SnsLlmCandidateProvider` — domain-filtered SNS tweets (only rules whose `domains` intersect `{pokemon, yugioh, ws, union_arena, tcg}`).
   - `HotCardBoardCandidateProvider` — reuses the `/trend` hot-card boards.
   - `ScheduledWebSearchCandidateProvider` — periodic DuckDuckGo TCG-trend queries.
2. `price_monitor_bot` estimates fair value and searches Mercari listings at or below the target price.
3. `reputation_snapshot` verifies seller reputation for each listing.
4. OpenClaw Telegram sends only qualified recommendations to the user.

The agent never buys automatically.

## Runtime Shape

The MVP lives inside `aka_no_claw` because it is the current integration layer for Telegram, SNS, price, and reputation services.

Main files:

- `src/openclaw_adapter/opportunity_models.py`
- `src/openclaw_adapter/opportunity_store.py`
- `src/openclaw_adapter/opportunity_scoring.py`
- `src/openclaw_adapter/opportunity_pipeline.py`
- `src/openclaw_adapter/opportunity_agent.py`

The Mac launcher starts it as a separate launchctl job:

- `local.openclaw.opportunity`

Logs:

- `logs/opportunity_agent.log`

State:

- `data/opportunities.sqlite3`

## Candidate Data Model (three-level hierarchy)

`OpportunityCandidate` (`opportunity_models.py`):

- `game` — Layer 1 IP: `pokemon / ws / yugioh / union_arena`.
- `product_type` — Layer 2 constrained enum: `single_card / booster_pack / sealed_box / starter_deck / promo / other`. Free-text alias inputs are normalised via `normalize_product_type`.
- `title` — Layer 3 specific product name.
- `product_identifier` — Layer 3 detail: card_number (e.g. `201/165`, `QCCP-JP001`) for single_card; set_code (e.g. `sv-p`) for sealed_box / booster_pack; `None` otherwise.

`build_candidate_id` hashes all four levels plus `search_query`, so two records with identical names but different product_types stay distinct.

## SNS Rule Domain Mechanism

Every `AccountWatch / KeywordWatch / TrendWatch` in `sns_monitor_bot` now carries `domains: tuple[str, ...]`. Topic-specific agents filter by intersection:

- `TCG_DOMAINS = {pokemon, yugioh, ws, union_arena, tcg}` — TCG opportunity agent reads only rules whose `domains` intersect this set.
- Untagged rules (`domains=()`) are invisible to the TCG agent until backfill tags them.
- The opportunity agent's preflight runs domain backfill (one rule per tick, LLM-driven) and account auto-discovery (every 6 h, capped 2 per run).

## Pipeline

Each tick performs:

1. **Preflight** — `opportunity_sns_domain_backfill` (one untagged rule) + `opportunity_sns_discovery` (every 6 h).
2. **Candidate discovery** via `ChainedCandidateProvider`:
   - SNS LLM extraction (domain-filtered).
   - Hot card board snapshot (per-game top-N).
   - Scheduled web-trend search + LLM extraction.
   - Optional `WebResearchCandidateProvider` enrichment on top.
3. **Title normalisation**: strip `抽選情報 / 予約情報 / 発売情報 / Mercari` noise; convert `セット名収録 カード名` to the card name when it's clearly a single-card target; reject unsupported franchises (`デュエルマスターズ`, `ONE PIECE CARD GAME`, etc.).
4. **Multi-product split**: the LLM prompt instructs splitting `インフェルノX・スタートデッキ100`-style multi-product mentions into separate candidates; product-internal `・` (e.g. card with multiple Pokemon names) preserved.
5. **Save/upsert** into `opportunity_candidates` (rebuilt schema with `product_type` + `product_identifier`).
6. For due candidates, run fair-value lookup via `price_monitor_bot`.
7. Search Mercari for listings ≤ target price.
8. For each unseen listing, request a reputation snapshot.
9. Score the full opportunity.
10. Send Telegram recommendation only if all thresholds pass.

## Default Thresholds

- SNS heat score: `>= 70`
- Listing price ratio: `<= 0.85` of fair value
- Price confidence: `>= 0.60`
- Seller reviews: `>= 30`
- Seller positive rate: `>= 97%`

Environment variables:

Core agent:
- `OPENCLAW_OPPORTUNITY_AGENT_ENABLED`
- `OPENCLAW_OPPORTUNITY_DB_PATH`
- `OPENCLAW_OPPORTUNITY_INTERVAL_SECONDS`
- `OPENCLAW_OPPORTUNITY_LLM_TIMEOUT_SECONDS`
- `OPENCLAW_OPPORTUNITY_SNS_LOOKBACK_HOURS`
- `OPENCLAW_OPPORTUNITY_CANDIDATE_LIMIT`
- `OPENCLAW_OPPORTUNITY_LISTING_LIMIT`
- `OPENCLAW_OPPORTUNITY_CANDIDATE_CHECK_INTERVAL_SECONDS`
- `OPENCLAW_OPPORTUNITY_MIN_HEAT_SCORE`
- `OPENCLAW_OPPORTUNITY_MAX_PRICE_RATIO`
- `OPENCLAW_OPPORTUNITY_MIN_PRICE_CONFIDENCE`
- `OPENCLAW_OPPORTUNITY_MIN_TOTAL_REVIEWS`
- `OPENCLAW_OPPORTUNITY_MIN_POSITIVE_RATE`

Hot-card board provider:
- `OPENCLAW_OPPORTUNITY_HOT_CARD_PROVIDER_ENABLED` (default `true`)
- `OPENCLAW_OPPORTUNITY_HOT_CARD_PER_GAME_LIMIT` (default `3`)
- `OPENCLAW_OPPORTUNITY_HOT_CARD_MIN_SCORE` (default `60.0`)

Web-trend search provider:
- `OPENCLAW_OPPORTUNITY_WEB_TREND_PROVIDER_ENABLED` (default `true`)
- `OPENCLAW_OPPORTUNITY_WEB_TREND_QUERIES` (CSV; defaults to the five built-in TCG queries)
- `OPENCLAW_OPPORTUNITY_WEB_TREND_RESULTS_PER_QUERY` (default `5`)

SNS domain backfill / auto-discovery:
- `OPENCLAW_OPPORTUNITY_SNS_DOMAIN_BACKFILL_ENABLED` (default `true`)
- `OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_ENABLED` (default `true`)
- `OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_INTERVAL_HOURS` (default `6`)
- `OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_MAX_NEW_PER_RUN` (default `2`)
- `OPENCLAW_OPPORTUNITY_SNS_AUTO_DISCOVERY_MIN_CONFIDENCE` (default `0.7`)

## Telegram Recommendation Format

The message includes:

- Product title
- SNS heat and reason
- Web research source URLs when available
- Fair value
- Listing price
- Discount percentage
- Opportunity score
- Seller review rate and total review count
- Reputation snapshot URL
- Mercari listing URL

## Visibility

The current candidate targets can be inspected without opening SQLite:

- Telegram: `/hunt status`
- CLI: `python -m openclaw_adapter opportunity-status --limit 10`

The status view shows recent candidates, heat score, Mercari search query, last checked time, and recent accepted/rejected recommendation records.

Targets can be dismissed from Telegram after reading `/hunt status`:

- `/hunt remove 2`
- `/hunt remove Umbreon ex SAR`
- Natural language such as `remove target 2 from the opportunity list`

Dismissed targets are marked inactive in SQLite and are not reactivated by later SNS discovery for the same candidate ID.

## Current Limitations

- Candidate sources are SNS (domain-filtered), hot-card board, and DuckDuckGo. Per-vendor restock-page scrapers (Yuyutei / Cardrush / Magi) are not yet wired.
- Supported IPs: `pokemon`, `ws`, `yugioh`, `union_arena` (matches the current TCG price modules and the `RECOMMENDED_DOMAINS` enum).
- It only recommends Mercari listings.
- Reputation verification waits for `reputation_snapshot` job completion, so a first-time seller check can take several minutes (default 240 s timeout).
- Providers run sequentially in the chain; a slow web-search query stretches the tick.
- There is no `/hunt approve` or `/hunt reject` feedback loop yet.

## Next Useful Upgrade

In priority order:

1. **Per-vendor restock scrapers** (Yuyutei / Cardrush / Magi). Most structured signal still untapped. Add as a fourth provider beside `HotCardBoardCandidateProvider` and chain in `build_opportunity_agent()`.
2. Telegram control commands: `/hunt pause / resume / thresholds / summary / reject <recommendation_id>`. Feed accept/reject decisions back into scoring.
3. Provider concurrency (thread-pool / asyncio).
4. Auto-cleanup of low-yield SNS accounts (observe first, then automate).
5. Second-pass LLM verifier for separator-containing candidate titles, if the prompt fix in Phase 12 isn't enough in practice.
