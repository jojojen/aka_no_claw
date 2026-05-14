# OpenClaw Opportunity Agent Spec

Status: MVP implementation in progress  
Owner layer: `aka_no_claw` integration runtime  
Runtime command: `python -m openclaw_adapter opportunity-agent`
Status command: `python -m openclaw_adapter opportunity-status`
Telegram status: `/hunt status`

## Goal

Continuously find buying opportunities by combining three existing systems:

1. `sns_monitor_bot` discovers social buzz and promising product targets.
2. `price_monitor_bot` estimates fair value and searches Mercari listings at or below the target price.
3. `reputation_snapshot` verifies the seller reputation for each listing.
4. OpenClaw Telegram sends only qualified recommendations to the user for final human judgment.

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

## Pipeline

One tick performs:

1. Read recent SNS posts from `SNS_DB_PATH`.
2. Ask the configured local text LLM to extract candidate products as JSON.
3. Normalize extracted product names before saving:
   - remove non-product words such as `抽選情報`, `予約情報`, `発売情報`, and `Mercari`
   - convert `セット名収録 カード名` into the individual card name when it is clearly a card target
4. Reject obvious unsupported franchises such as `遊☆戯☆王`, `デュエルマスターズ`, and `ONE PIECE CARD GAME`.
5. Use the web search tool to gather outside-market context for each candidate.
6. Ask the configured local text LLM to judge whether those web sources support real demand, then store source URLs in candidate metadata.
7. Save/update candidates in `opportunity_candidates`.
8. For due candidates, run fair-value lookup using `price_monitor_bot`.
9. Search Mercari for listings below the calculated target price.
10. For each unseen listing, request a reputation snapshot.
11. Score the full opportunity.
12. Send Telegram recommendation only if all thresholds pass.

## Default Thresholds

- SNS heat score: `>= 70`
- Listing price ratio: `<= 0.85` of fair value
- Price confidence: `>= 0.60`
- Seller reviews: `>= 30`
- Seller positive rate: `>= 97%`

Environment variables:

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

## Current Limitations

- Product extraction depends on recent rows in the SNS SQLite database.
- MVP supports `pokemon` and `ws`, matching the current TCG price modules.
- It only recommends Mercari listings.
- Reputation verification waits for `reputation_snapshot` job completion, so a first-time seller check can take several minutes.
- There is no `/hunt approve` or `/hunt reject` feedback loop yet.

## Next Useful Upgrade

Add more Telegram control commands:

- `/hunt pause`
- `/hunt resume`
- `/hunt thresholds`
- `/hunt reject <recommendation_id>`
- `/hunt summary`

Then use those decisions as feedback for future scoring.
