# OpenClaw Telegram Tool Spec

The Telegram assistant can reach these tools:

- `/help`
  - Show the supported commands and short examples.
  - Use when the user asks what the bot can do or how to use it.

- `/status`
  - Show runtime status, active models, OCR paths, and reputation agent status.
  - Use when the user asks about current bot state, model usage, or service health.

- `/tools`
  - Show the internal tool catalog.
  - Use when the user explicitly asks for all available tools or capabilities in catalog form.

- `/price <game> <name>`
- `/price <game> | <name> | <card_number> | <rarity> | <set_code>`
  - Look up the price/value of one card.
  - Games: `pokemon`, `ws`.

- `/trend <game> [limit]`
- `/hot <game> [limit]`
- `/liquidity <game> [limit]`
  - Show the static hot / ranking / liquidity leaderboard for a game.
  - Games: `pokemon`, `ws`.
  - Limit should usually be 1-10, default 5.
  - Use ONLY for explicit leaderboard / top-N / ranking requests. Does NOT analyse price direction or market sentiment — those go to `/search`.

- `/snapshot <url>`
  - Build or reuse a reputation snapshot for a Mercari item/profile URL.

- `/search <question>`
- `/research <question>`
  - Search the web with DuckDuckGo, then summarize the result with the configured local LLM.
  - Include source URLs / references in the final reply.
  - Use for price-direction / market-sentiment / recent-news / why-how questions, e.g. "寶可夢卡是不是在跌", "why are pokemon cards popular", "pokemon 市場最近怎麼了", "遊戲王最近暴跌".

- Photo scan flow
  - Send a photo with caption `/scan pokemon` or `/scan ws`.
  - The bot can also handle a plain card photo without a strict caption and try OCR/vision lookup.
  - Use this path when the user is asking to identify or price a card from an image.

- `/watch <query> on <price>`
  - Add a Mercari watch.

- `/watchlist`
  - Show the current watch list.

- `/unwatch <watch_id>`
  - Remove a watch.

- `/setprice <watch_id> <price>`
  - Update the price threshold of an existing watch.

- `/snsadd @username`
  - Add (or upsert) an X (Twitter) account watch rule.
  - Usage: `/snsadd @akanoclaw`
  - Optional **filter** keywords (tweet must contain any of them to notify): `/snsadd @elonmusk filter[buy, sell]` or legacy `/snsadd @elonmusk ["buy", "sell"]`.
  - Optional **domain** tags so the right topic agent can pick up the account: `/snsadd @Laurier_News filter[抽選] domain[pokemon, yugioh]`.
  - Re-running `/snsadd @same_handle` becomes an upsert: filter and domain replace if explicitly provided; otherwise existing values are preserved.

- `/snsadd keyword:<search_term> domain[<tags>]`
  - Add (or upsert) a keyword search watch rule.
  - Usage: `/snsadd keyword:機動戰士 domain[gundam, anime]`

- `/snsadd trend:<category> domain[<tags>]`
  - Add a trend category to the watch list.
  - Categories: `trending`, `for-you`, `news`, `sports`, `entertainment`
  - Usage: `/snsadd trend:trending domain[news]`

- `/snslist`
  - List all X (Twitter) watch rules. Each row shows `filter[…]` and `domain[…]`; rules awaiting LLM backfill display `domain[?]`.

- `/snsdelete <target>`
  - Remove an X watch rule. `<target>` may be a rule ID prefix, an @handle, or `keyword:xxx`.
  - Examples: `/snsdelete @elonmusk`, `/snsdelete abc12345`, `/snsdelete keyword:機動戰士`

### Domain tags

Each watch rule carries a `domains` tuple. Topic-specific agents filter by intersection (TCG agent reads only rules whose `domains` intersect `{pokemon, yugioh, ws, union_arena, tcg}`). Recommended values:

- TCG: `pokemon`, `yugioh`, `ws`, `union_arena`, `tcg`
- Non-TCG: `politic`, `stock`, `news`, `gaming`, `entertainment`, `anime`, `gundam`, `other`

Free-text values are accepted (normalised to lowercase). Untagged rules are auto-tagged by the opportunity agent's LLM backfill, one rule per cron tick — the user receives a Telegram heads-up `🏷 自動標記 @X 領域：…` and can override with `/snsadd @X domain[…]`.

- `/snsbuzz <keyword>`
  - Summarise Reddit's top discussion on a topic via LLM.
  - Usage: `/snsbuzz amd`

- `/hunt status`
  - Show recent opportunity candidates and recommendation decisions.

- `/hunt remove <number-or-name>`
  - Remove/dismiss an opportunity target from the active hunt list.
  - Use when the user says they are not interested in a target from `/hunt status`.
  - `<number-or-name>` may be the visible list number from `/hunt status` or part of the product name.

Natural-language examples for SNS intents (router must distinguish these from Mercari watch intents — any `@handle` or X/Twitter/推特 keyword forces SNS):

- "追蹤 @elonmusk" → `sns_add_account`, sns_handle="elonmusk"
- "刪除追蹤 @elonmusk" / "取消追蹤 @elonmusk" / "unfollow @elonmusk" → `sns_delete`, sns_handle="elonmusk"
- "我的 X 追蹤清單" / "推主追蹤" → `sns_list`
- "整理一下 amd 最近熱門討論" → `sns_buzz`, sns_buzz_query="amd"
- "remove target 2 from opportunity list" → `opportunity_remove`, opportunity_target="2"
- "I am not interested in Umbreon ex SAR" → `opportunity_remove`, opportunity_target="Umbreon ex SAR"

Routing rules:

- Return `help` for capability or usage questions.
- Return `status` for runtime/model/service-state questions.
- Return `tools` when the user explicitly wants a tool catalog.
- Return `scan_help` when the user asks how to use image/photo scan or wants image lookup instructions before sending a photo.
- Return `lookup_card` for one-card valuation requests.
- Return `trend_board` ONLY for explicit leaderboard / top-N / ranking requests (e.g. "pokemon 熱門前 5", "遊戲王熱門排行"). Price-direction questions go to `web_research`, not `trend_board`.
- Return `reputation_snapshot` when a Mercari URL is provided for seller/item trust checking.
- Return `web_research` when the user asks about price direction, market sentiment, recent news, or any why/how/explanatory question that needs web sources and summarization.
- Return `opportunity_remove` when the user wants to remove/dismiss a target from the opportunity/hunt list.
- Return watch intents for tracking requests.
- Return `unknown` when the request is unrelated or too ambiguous.
